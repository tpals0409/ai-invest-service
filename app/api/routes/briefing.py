"""데일리 브리핑. API 명세 §8.

랭킹은 규칙 엔진이 하고(`app.engines.briefing`, 산식 §5.2) LLM은 상위 4건의 두
문장만 쓴다. 후보는 세 곳에서 나온다 — 보유 종목 등락과 섹터 비중 이동은
Portfolio Engine이 만들고, 실적·공시·거시는 `events` 테이블에서 읽는다.

담당 트랙: feat/event-ranking
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession
from app.api.routes.portfolio import _ledger
from app.core.adapters import Ledger
from app.core.enums import BriefingCategory, BriefingStatus, MetricSource
from app.core.errors import InsufficientData
from app.core.models import AIResponse, Event
from app.core.response_log import record
from app.core.schemas import DataAsOf, Envelope, Section
from app.engines.briefing import (
    DEFAULT_EVENT_IMPORTANCE,
    EVENT_CATEGORY,
    EVENT_IMPORTANCE,
    HORIZON_DAYS,
    MOVE_FLOOR,
    NOVELTY_DAYS,
    PORTFOLIO_SHIFT_IMPORTANCE,
    SECTOR_SHIFT_FLOOR,
    Candidate,
    RankedItem,
    item_key,
    move_importance,
    rank,
)
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.engines.risk import _rate_exposure
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import generate_section, ratio_segment
from app.llm.guard import Feature

log = logging.getLogger("app.api.briefing")

router = APIRouter(prefix="/briefing", tags=["briefing"])

_ENDPOINT = "briefing"
_SOURCE = MetricSource.PORTFOLIO_ENGINE

#: 섹터 비중 이동을 재는 기준 시점. 하루 전과 비교하면 매매 한 번에 5%p가 넘어
#: 매일 걸리고, 한 달 전과 비교하면 "오늘의 브리핑"이 아니게 된다.
_SHIFT_LOOKBACK_DAYS = 5

#: novelty가 훑을 최근 응답 행 수. 하루 한 번 보는 화면이라 7일이면 7행이지만,
#: 새로고침을 세는 사용자가 있으므로 여유를 둔다.
#: ponytail: 하루 수십 번 호출하는 사용자는 7일치를 다 못 본다. 배치가 생기면
#: 하루 한 행만 남으므로 이 상수는 그때 사라진다.
_NOVELTY_ROWS = 50


# ── 라우터 ────────────────────────────────────────────────────────────────────
# ponytail: 배치가 없어 요청 시점에 계산한다. 스케줄러가 생기면 여기서 조회만 한다.
@router.get("")
async def get_briefing(
    user_id: CurrentUser, db: DbSession, date: Date | None = None
) -> Envelope[dict]:
    """오늘 알릴 항목 최대 4건을 골라 각각 두 문장으로 설명한다(§8).

    `empty`는 에러가 아니다. 원장을 못 읽거나 보유 종목이 없거나 알릴 만한 일이
    없으면 200에 `status: "empty"`로 답한다 — 화면은 이 값을 보고 섹션을 숨긴다.
    409는 항목을 골라 놓고도 문장을 만들 수 없을 때(LLM 키 부재)뿐이다.

    `generating`은 돌려주지 않는다. 배치가 없어서 요청 시점에 다 계산하기 때문에
    "생성 중"인 상태 자체가 존재하지 않는다.
    """
    ledger = _ledger(user_id)
    if ledger is None:
        return _empty(date or Date.today())

    day = _trading_day(ledger, date)
    if day is None:
        return _empty(date or Date.today())

    engine = PortfolioEngine(ledger)
    snapshot = engine.snapshot(day)
    if not snapshot.holdings:
        return _empty(day)

    candidates = [
        *_holding_moves(engine, snapshot, day),
        *_sector_shifts(engine, ledger, snapshot, day),
        *await _event_candidates(db, snapshot, day),
    ]
    top = rank(candidates, today=day, seen_keys=await _recent_keys(db, user_id, day))
    if not top:
        return _empty(day)

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 네 항목이 같은 이유로 실패한다. 항목마다 null로 흩뿌리지 않는다.
        raise InsufficientData("ANTHROPIC_API_KEY가 없어 브리핑을 생성할 수 없습니다.")

    outcomes = await asyncio.gather(
        *(
            generate_section(
                item.candidate.key,
                title=item.candidate.title,
                feature=Feature.DAILY_BRIEFING_ITEM,
                prompt="daily_briefing",
                client=client,
                engine_values=item.candidate.values,
                request=item.candidate.request,
            )
            for item in top
        )
    )

    items: list[dict[str, Any]] = []
    for item, outcome in zip(top, outcomes, strict=True):
        if outcome.section is None:
            log.warning("브리핑 항목 %s 생성 실패 · %s", outcome.key, "; ".join(outcome.reasons))
            continue
        items.append(_item_payload(item, outcome.section))

    envelope = Envelope[dict](
        content={
            # 문장이 하나도 안 나오면 보여줄 것이 없다. 빈 목록에 ready를 붙이면
            # 화면은 제목만 있는 빈 섹션을 그린다.
            "date": day.isoformat(),
            "status": (BriefingStatus.READY if items else BriefingStatus.EMPTY).value,
            "items": items,
        },
        data_as_of=DataAsOf(price=_as_datetime(day), portfolio=_as_datetime(day)),
    )
    # 내일 novelty가 읽을 행이고, 피드백이 참조할 행이 된다.
    await record(db, envelope, user_id=user_id, endpoint=_ENDPOINT)
    return envelope


def _empty(day: Date) -> Envelope[dict]:
    """보여줄 것이 없는 브리핑. 응답 로그에는 남기지 않는다 — novelty는 항목만
    읽고, 피드백을 달 대상도 없다."""
    return Envelope[dict](
        content={"date": day.isoformat(), "status": BriefingStatus.EMPTY.value, "items": []}
    )


def _trading_day(ledger: Ledger, requested: Date | None) -> Date | None:
    """요청일 이하의 마지막 거래일. 휴장일을 물어도 직전 장으로 답한다."""
    if requested is None:
        return ledger.trading_days[-1]
    return next((day for day in reversed(ledger.trading_days) if day <= requested), None)


# ── 후보 ──────────────────────────────────────────────────────────────────────
def _holding_moves(
    engine: PortfolioEngine, snapshot: PortfolioSnapshot, day: Date
) -> list[Candidate]:
    """그날 움직인 보유 종목. 이벤트 테이블이 비어 있어도 브리핑이 나오는 경로다."""
    row = next((r for r in engine.daily_returns() if r.trade_date == day), None)
    if row is None:
        return []

    holdings = {h.symbol: h for h in snapshot.holdings}
    candidates = []
    for contribution in row.contributions:
        holding = holdings.get(contribution.symbol)
        if holding is None or abs(contribution.return_rate) < MOVE_FLOOR:
            continue
        up = contribution.return_rate > 0
        candidates.append(
            Candidate(
                key=item_key(BriefingCategory.HOLDING_MOVE, (holding.symbol,)),
                category=BriefingCategory.HOLDING_MOVE,
                title=f"{holding.name} {'강세' if up else '약세'}",
                stock_weight=holding.stock_weight,
                importance=move_importance(contribution.return_rate),
                event_date=day,
                deeplink=f"/stocks/{holding.symbol}?tab=ai",
                tickers=(holding.symbol,),
                values={
                    "weight": ratio_segment(holding.stock_weight, _SOURCE),
                    "return_rate": ratio_segment(
                        contribution.return_rate, _SOURCE, signed=True, digits=2
                    ),
                },
                request=(
                    f"{holding.name}({holding.sector}) 종목의 당일 등락 브리핑을 "
                    f"작성하십시오. 엔진 판정: {'상승' if up else '하락'}. "
                    "등락의 원인은 확인된 바가 없습니다."
                ),
            )
        )
    return candidates


def _sector_shifts(
    engine: PortfolioEngine, ledger: Ledger, snapshot: PortfolioSnapshot, day: Date
) -> list[Candidate]:
    """섹터 비중이 5%p 이상 움직인 건. 기준은 5거래일 전 스냅샷이다."""
    days = ledger.trading_days
    index = days.index(day)
    if index == 0:
        return []
    before = engine.snapshot(days[max(0, index - _SHIFT_LOOKBACK_DAYS)])

    now = _sector_weights(snapshot)
    then = _sector_weights(before)
    candidates = []
    for sector in sorted(now.keys() | then.keys()):
        delta = now.get(sector, 0.0) - then.get(sector, 0.0)
        if abs(delta) < SECTOR_SHIFT_FLOOR:
            continue
        # 빠져나간 업종은 지금 스냅샷에 종목이 없다. 그때는 예전 스냅샷에서 이름을 딴다.
        source = snapshot if sector in now else before
        tickers = tuple(sorted(h.symbol for h in source.holdings if h.sector == sector))
        weight = max(now.get(sector, 0.0), then.get(sector, 0.0))
        candidates.append(
            Candidate(
                key=item_key(BriefingCategory.PORTFOLIO_SHIFT, tickers),
                category=BriefingCategory.PORTFOLIO_SHIFT,
                title=f"{sector} 비중 {'확대' if delta > 0 else '축소'}",
                stock_weight=weight,
                importance=PORTFOLIO_SHIFT_IMPORTANCE,
                event_date=day,
                deeplink="/portfolio",
                tickers=tickers,
                values={
                    "sector_weight": ratio_segment(now.get(sector, 0.0), _SOURCE),
                    "weight_change": ratio_segment(delta, _SOURCE, signed=True),
                },
                request=(
                    f"{sector} 업종 비중 변화 브리핑을 작성하십시오. "
                    f"엔진 판정: 최근 {_SHIFT_LOOKBACK_DAYS}거래일 사이 "
                    f"{'확대' if delta > 0 else '축소'}."
                ),
            )
        )
    return candidates


def _sector_weights(snapshot: PortfolioSnapshot) -> dict[str, float]:
    """현금 제외 후 재정규화한 업종별 비중(ŵ 합)."""
    weights: dict[str, float] = {}
    for holding in snapshot.holdings:
        weights[holding.sector] = weights.get(holding.sector, 0.0) + holding.stock_weight
    return weights


async def _event_candidates(
    db: DbSession, snapshot: PortfolioSnapshot, day: Date
) -> list[Candidate]:
    """`events`에서 온 실적·공시·거시. 테이블이 비어 있으면 빈 목록이다.

    거시 이벤트는 종목이 없으므로 포트폴리오 전체(ŵ=1)로 보되, 산식 §5.2대로
    금리민감도 계수를 곱한다 — 금리와 무관한 구성이면 거시 일정도 남 얘기다.
    """
    holdings = {h.symbol: h for h in snapshot.holdings}
    rows = (
        await db.execute(
            select(
                Event.event_type,
                Event.ticker,
                Event.title,
                Event.event_date,
                Event.importance,
            ).where(
                Event.event_date <= day,
                Event.event_date >= day - timedelta(days=HORIZON_DAYS),
                or_(Event.ticker.in_(tuple(holdings)), Event.ticker.is_(None)),
            )
        )
    ).all()
    if not rows:
        return []

    rate_coefficient = min(abs(_rate_exposure(snapshot).score), 1.0)
    candidates = []
    for event_type, ticker, title, event_date, weight in rows:
        category = EVENT_CATEGORY.get(event_type)
        if category is None:
            continue
        holding = holdings.get(ticker) if ticker else None
        if ticker and holding is None:
            continue

        # 적재기가 매긴 `events.importance`는 표를 깎기만 한다. 기본값(0.5)이면 표
        # 그대로다 — 적재기 조정이 순위를 뒤집으면 랭킹 재현성이 사라진다.
        importance = EVENT_IMPORTANCE[event_type] * min(
            float(weight) / DEFAULT_EVENT_IMPORTANCE, 1.0
        )
        if category is BriefingCategory.MACRO_EVENT:
            importance *= rate_coefficient

        tickers = (ticker,) if ticker else ()
        candidates.append(
            Candidate(
                key=item_key(category, tickers),
                category=category,
                title=title,
                stock_weight=holding.stock_weight if holding else 1.0,
                importance=importance,
                event_date=event_date,
                deeplink=f"/stocks/{ticker}?tab=ai" if ticker else "/portfolio",
                tickers=tickers,
                values=(
                    {"weight": ratio_segment(holding.stock_weight, _SOURCE)} if holding else {}
                ),
                request=(
                    f"{event_date.isoformat()}자 이벤트 브리핑을 작성하십시오. "
                    f"엔진 판정 분류: {category.value}. 이벤트: {title}."
                ),
            )
        )
    return candidates


# ── novelty ───────────────────────────────────────────────────────────────────
async def _recent_keys(db: DbSession, user_id: str, day: Date) -> set[str]:
    """최근 7일 브리핑에 나갔던 항목 키. §5.2 novelty.

    스키마를 넓히지 않고 `ai_responses.payload`를 그대로 다시 읽는다. 같은 날 앞선
    호출은 세지 않는다 — 세면 하루에 두 번 열었을 때 순서가 달라져서, 규칙 엔진으로
    랭킹하는 이유인 재현성이 사라진다.
    """
    payloads = (
        await db.execute(
            select(AIResponse.payload)
            .where(AIResponse.user_id == user_id, AIResponse.endpoint == _ENDPOINT)
            .order_by(AIResponse.created_at.desc())
            .limit(_NOVELTY_ROWS)
        )
    ).scalars()

    floor = (day - timedelta(days=NOVELTY_DAYS)).isoformat()
    today = day.isoformat()
    keys: set[str] = set()
    for payload in payloads:
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict):
            continue
        stamp = content.get("date")
        # ISO 날짜 문자열은 사전순 비교가 곧 날짜 비교다.
        if not isinstance(stamp, str) or not floor <= stamp < today:
            continue
        for item in content.get("items") or []:
            if isinstance(item, dict):
                keys.add(item_key(item.get("category", ""), item.get("related_tickers") or []))
    return keys


# ── 응답 조립 ─────────────────────────────────────────────────────────────────
def _item_payload(item: RankedItem, section: Section) -> dict[str, Any]:
    """§8 `items` 한 줄. `citations`는 이 기능이 문서를 조회하지 않아 늘 빈 배열이다.

    ponytail: 이벤트에 `document_id`가 달려 오면 근거를 붙일 수 있다. documents
    조인이 필요해서 지금은 비워 둔다 — §8 예시의 `cit_1`이 그 자리다.
    """
    candidate = item.candidate
    return {
        "rank": item.rank,
        "category": candidate.category.value,
        "relevance_score": round(item.relevance_score, 2),
        "title": section.title or candidate.title,
        "text": section.text,
        "segments": [segment.model_dump(mode="json") for segment in section.segments],
        "related_tickers": list(candidate.tickers),
        "deeplink": candidate.deeplink,
        "citations": [],
    }


def _as_datetime(day: Date) -> datetime:
    """기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    return datetime.combine(day, time(15, 30))

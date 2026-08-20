"""데일리 브리핑 랭킹. 엔진 산식 §5.2.

    relevance = ŵ_i^0.5 × importance × recency × novelty

전부 코드다. LLM은 상위 4건이 확정된 뒤 각 항목의 두 문장을 쓰는 데만 불린다.
랭킹까지 모델에 맡기면 비용이 사용자 수 × 이벤트 수로 늘고, 같은 입력이 같은
순서를 준다는 보장이 사라진다.

비중에 제곱근을 씌우는 것은 의도된 것이다. 선형으로 두면 최대 보유 종목이 네
칸을 독식해서, 소액 보유 종목의 실적 발표가 영원히 올라오지 못한다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date as Date

from app.core.enums import BriefingCategory, EventType
from app.core.schemas import Segment

__all__ = [
    "EVENT_CATEGORY",
    "EVENT_IMPORTANCE",
    "HORIZON_DAYS",
    "MOVE_FLOOR",
    "NOVELTY_DAYS",
    "PORTFOLIO_SHIFT_IMPORTANCE",
    "SECTOR_SHIFT_FLOOR",
    "TOP_N",
    "Candidate",
    "RankedItem",
    "item_key",
    "move_importance",
    "rank",
]

#: §8 "최대 4건".
TOP_N = 4

#: 후보로 볼 최대 경과일. exp(−7/2)≈0.03이라 그 밖은 어차피 순위에 못 든다.
HORIZON_DAYS = 7

#: novelty가 되짚는 기간. §5.2 "최근 7일 내 동일 항목 노출 이력".
NOVELTY_DAYS = 7

_REPEAT_NOVELTY = 0.3

#: §5.2 importance 표. key는 `events.event_type`(`EventType`)이다.
EVENT_IMPORTANCE: Mapping[str, float] = {
    EventType.EARNINGS: 1.0,  # 실적 발표 · 실적 정정
    EventType.FILING: 0.8,  # 주요 공시
    EventType.DIVIDEND: 0.8,  # 배당도 주요 공시로 본다
    EventType.MACRO: 0.6,  # rate_sensitivity 계수는 호출부가 곱한다
    EventType.PRODUCT: 0.4,  # 일반 뉴스
}

#: `events.importance` 기본값. 적재기가 손대지 않은 행은 표 그대로 쓰인다.
DEFAULT_EVENT_IMPORTANCE = 0.5

#: 이벤트 종류 → 브리핑 분류. `BriefingCategory`에 뉴스 칸이 없어서 `product`는
#: 공시 쪽으로 넣는다 — 신제품·수주 발표는 국내 시장에서 대개 공시로 나온다.
EVENT_CATEGORY: Mapping[str, BriefingCategory] = {
    EventType.EARNINGS: BriefingCategory.EARNINGS,
    EventType.FILING: BriefingCategory.FILING,
    EventType.DIVIDEND: BriefingCategory.FILING,
    EventType.MACRO: BriefingCategory.MACRO_EVENT,
    EventType.PRODUCT: BriefingCategory.FILING,
}

#: §5.2 "포트폴리오 구조 변화".
PORTFOLIO_SHIFT_IMPORTANCE = 0.7

#: 섹터 비중이 이만큼 움직여야 항목이 된다. §5.2 "5%p 이상 이동".
SECTOR_SHIFT_FLOOR = 0.05

#: 이만큼 못 움직인 종목은 후보가 아니다. 대형주 하루 ±1% 안쪽은 장중 잡음이지
#: 알릴 거리가 아니고, 문턱이 없으면 아무 일 없던 날에도 보유 종목이 그대로 네
#: 칸을 채운다. §8의 `empty`("유의미한 이벤트 없음")를 실제로 만드는 값이다.
MOVE_FLOOR = 0.01

#: 등락 → importance 기울기. ±5%에서 주요 공시(0.8)에 닿는다.
_MOVE_SLOPE = 8.0


def move_importance(return_rate: float) -> float:
    """주가 등락의 importance.

    §5.2 표에 주가 변동 행이 없다. 일반 뉴스(0.4)를 바닥으로 두고 등락폭에 비례해
    올린다 — 등락 자체가 곧 그 항목의 중요도라서, 같은 종목의 +0.5%와 +8%를 같은
    무게로 두면 랭킹이 비중 순서로 굳어버린다.

    ponytail: 표에 행이 생기면 이 함수를 지우고 상수로 바꾼다.
    """
    return min(1.0, 0.4 + abs(return_rate) * _MOVE_SLOPE)


def item_key(category: str, tickers: Sequence[str]) -> str:
    """novelty가 "동일 항목"을 판정하는 식별자.

    응답에 이미 실려 나가는 `category`·`related_tickers`만으로 만든다. 어제 응답을
    `ai_responses.payload`에서 그대로 다시 읽어 만들 수 있어야 해서다(스키마를
    넓히지 않으려는 것이다).

    ponytail: 같은 종목의 서로 다른 공시 두 건이 같은 키가 된다. novelty가 막으려는
    것이 "같은 종목 같은 이야기의 반복"이라 지금은 이 정도로 충분하다. 이벤트 id까지
    구분해야 하면 응답에 항목 id를 추가하고 여기서 함께 읽는다.
    """
    return f"{category}|{'.'.join(sorted(tickers))}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """브리핑 항목 후보 하나. 랭킹 입력과 문장 생성 입력을 함께 들고 다닌다."""

    key: str
    category: BriefingCategory
    title: str
    #: ŵ_i — 현금 제외 후 재정규화한 비중. 포트폴리오 전체 사안은 1.0이다.
    stock_weight: float
    importance: float
    event_date: Date
    deeplink: str
    tickers: tuple[str, ...] = ()
    #: 문장 생성이 쓸 수 있는 자리표시자. 랭킹은 건드리지 않는다.
    values: Mapping[str, Segment] = field(default_factory=dict)
    #: 모델에게 넘길 엔진 판정 문구.
    request: str = ""


@dataclass(frozen=True, slots=True)
class RankedItem:
    rank: int
    relevance_score: float
    candidate: Candidate


def rank(
    candidates: Iterable[Candidate],
    *,
    today: Date,
    seen_keys: Iterable[str] = (),
    limit: int = TOP_N,
) -> list[RankedItem]:
    """§5.2 relevance로 정렬해 상위 `limit`건을 매긴다.

    같은 입력은 항상 같은 순서를 준다. 점수가 같으면 `key` 사전순으로 가른다 —
    후보 목록이 만들어진 순서에 기대면 조회 순서가 바뀔 때 조용히 순위가 바뀐다.
    """
    seen = set(seen_keys)
    scored: list[RankedItem] = []
    for candidate in candidates:
        days = (today - candidate.event_date).days
        if not 0 <= days <= HORIZON_DAYS:
            continue
        score = (
            math.sqrt(max(candidate.stock_weight, 0.0))
            * candidate.importance
            * math.exp(-days / 2)
            * (_REPEAT_NOVELTY if candidate.key in seen else 1.0)
        )
        if score <= 0.0:
            continue
        scored.append(RankedItem(rank=0, relevance_score=score, candidate=candidate))

    scored.sort(key=lambda item: (-item.relevance_score, item.candidate.key))
    return [replace(item, rank=n) for n, item in enumerate(scored[:limit], start=1)]

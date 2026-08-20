"""데일리 브리핑 엔드포인트와 랭킹 엔진 테스트.

두 가지가 요점이다.

    경계        순위는 코드가 정하고 LLM은 두 문장만 쓴다. 그래서 같은 입력이 같은
                순서를 준다는 것을 순위 쪽에서 직접 고정한다.
    빈 이벤트   `events`는 아직 0행이다. 여기 있는 테스트는 **전부** 이벤트 0건에서
                통과한다 — 남의 적재기가 돌아야 통과하는 테스트는 CI에서 깨진다.

시드 픽스처는 10거래일이고 마지막 날 1%를 넘긴 종목이 하나뿐이라, *기본* 경로가
이미 "한 건짜리 브리핑"이다. 네 칸이 다 차는 날은 2025-09-10이다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.db import get_session
from app.core.enums import BriefingCategory
from app.engines.briefing import (
    HORIZON_DAYS,
    TOP_N,
    Candidate,
    item_key,
    move_importance,
    rank,
)
from app.llm.client import LlmResult, NullLlmClient
from app.llm.guard import split_sentences

URL = "/api/ai/v1/briefing"
#: 8종목·현금 4.2%. 마지막 거래일에 SK하이닉스만 1%를 넘긴다.
HOLDER = "mixed_with_cash"
#: 1종목. 마지막 거래일 등락이 -0.52%라 문턱에 걸려 브리핑이 비는 경로다.
LONELY = "golden_1_single"
STRANGER = "no_such_user"

#: 네 칸이 다 차는 날.
BUSY_DAY = "2025-09-10"

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


class FakeClient:
    """사용자 턴에 실린 자리표시자만 골라 쓰는, 검사를 통과하는 응답.

    항목마다 넘어가는 자리표시자가 달라서 하드코딩하면 못 쓰는 key를 써서 차단된다.
    턴에서 첫 key를 뽑아 쓰는 편이 실제 모델이 하는 일에 더 가깝다.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        turn: str = kwargs["user"]
        keys = _PLACEHOLDER_RE.findall(turn.split("[사용 가능한 수치 자리표시자]")[-1])
        first = keys[0] if keys else None
        fact = (
            f"확인된 지표는 {{{{{first}}}}}입니다."
            if first
            else "수치로 확인된 지표가 없습니다."
        )
        # 브리핑 항목은 정확히 2문장이다(응답 정책 §2).
        return LlmResult(
            payload={
                "narrative": f"{fact} 포트폴리오에서 차지하는 몫만큼 영향을 받습니다.",
                "used_placeholders": [first] if first else [],
                "used_citations": [],
            }
        )


class StubResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> list[Any]:
        return self._rows


class StubSession:
    """`events` 조회와 응답 로그에 답하는 최소 세션.

    쿼리 문자열로 갈라 낸다 — 호출 순서에 기대면 인자 평가 순서가 바뀌는 순간
    조용히 엉뚱한 값을 넘긴다.
    """

    def __init__(self, *, past: list[dict] | None = None) -> None:
        #: 지난 브리핑 봉투들. novelty가 읽는다.
        self.past = past or []
        self.seen: list[str] = []
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        self.seen.append(sql)
        if "ai_responses" in sql:
            return StubResult(list(self.past))
        # events는 아직 0행이다.
        return StubResult([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


def _make_client(monkeypatch: pytest.MonkeyPatch, session: Any, llm: Any) -> TestClient:
    monkeypatch.setattr("app.api.routes.briefing.get_llm_client", lambda: llm)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    test_client = TestClient(app)
    test_client.llm = llm  # type: ignore[attr-defined]
    test_client.db = session  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def client(monkeypatch):
    with _make_client(monkeypatch, StubSession(), FakeClient()) as test_client:
        yield test_client


def _get(client: TestClient, user: str, **params: str) -> Any:
    return client.get(URL, headers={"Authorization": f"Bearer {user}"}, params=params)


# ── 랭킹 엔진 ─────────────────────────────────────────────────────────────────
TODAY = date(2025, 9, 12)


def _candidate(key: str, *, weight: float, importance: float, day: date = TODAY) -> Candidate:
    return Candidate(
        key=key,
        category=BriefingCategory.HOLDING_MOVE,
        title=key,
        stock_weight=weight,
        importance=importance,
        event_date=day,
        deeplink="/portfolio",
    )


def test_비중에_제곱근을_씌워_소액_보유의_큰_이벤트가_올라온다() -> None:
    """선형 가중이면 큰 보유가 이기는 조합을 골라, 제곱근에서 뒤집히는지 본다."""
    small = _candidate("small", weight=0.09, importance=1.0)  # √: 0.300 · 선형: 0.090
    big = _candidate("big", weight=0.49, importance=0.4)  # √: 0.280 · 선형: 0.196

    assert big.stock_weight * big.importance > small.stock_weight * small.importance
    assert [item.candidate.key for item in rank([big, small], today=TODAY)] == ["small", "big"]


def test_최근일수록_앞선다() -> None:
    fresh = _candidate("a", weight=0.2, importance=0.8)
    stale = _candidate("b", weight=0.2, importance=0.8, day=TODAY - timedelta(days=3))
    assert [item.candidate.key for item in rank([stale, fresh], today=TODAY)] == ["a", "b"]


def test_최근_노출된_항목은_novelty로_밀린다() -> None:
    seen = _candidate("seen", weight=0.5, importance=1.0)
    fresh = _candidate("fresh", weight=0.5, importance=0.5)

    assert [i.candidate.key for i in rank([seen, fresh], today=TODAY)] == ["seen", "fresh"]
    ranked = rank([seen, fresh], today=TODAY, seen_keys=["seen"])
    assert [i.candidate.key for i in ranked] == ["fresh", "seen"]


def test_기간_밖의_후보는_아예_빠진다() -> None:
    old = _candidate("old", weight=1.0, importance=1.0, day=TODAY - timedelta(days=HORIZON_DAYS + 1))
    future = _candidate("future", weight=1.0, importance=1.0, day=TODAY + timedelta(days=1))
    edge = _candidate("edge", weight=1.0, importance=1.0, day=TODAY - timedelta(days=HORIZON_DAYS))
    assert [i.candidate.key for i in rank([old, future, edge], today=TODAY)] == ["edge"]


def test_상위_네_건만_남는다() -> None:
    many = [_candidate(f"c{n}", weight=0.5, importance=0.9 - n / 100) for n in range(9)]
    ranked = rank(many, today=TODAY)
    assert len(ranked) == TOP_N
    assert [item.rank for item in ranked] == [1, 2, 3, 4]


def test_같은_입력은_입력_순서와_무관하게_같은_순위를_준다() -> None:
    """동점은 key 사전순으로 가른다. 후보를 만든 순서에 기대면 조회 순서가 바뀔 때
    조용히 순위가 바뀐다."""
    tied = [_candidate(k, weight=0.3, importance=0.7) for k in ("delta", "alpha", "charlie")]
    forward = [item.candidate.key for item in rank(tied, today=TODAY)]
    backward = [item.candidate.key for item in rank(list(reversed(tied)), today=TODAY)]
    assert forward == backward == ["alpha", "charlie", "delta"]


def test_등락폭이_클수록_importance가_커지고_1을_넘지_않는다() -> None:
    assert move_importance(0.0) == pytest.approx(0.4)
    assert move_importance(-0.05) == pytest.approx(0.8)
    assert move_importance(0.05) == move_importance(-0.05)
    assert move_importance(0.5) == 1.0


def test_항목_키는_분류와_종목으로만_만들어진다() -> None:
    """응답에 실려 나가는 필드만 쓴다 — 어제 응답을 그대로 다시 읽어야 하기 때문이다."""
    assert item_key("holding_move", ["000660"]) == item_key("holding_move", ("000660",))
    assert item_key("holding_move", ["b", "a"]) == item_key("holding_move", ["a", "b"])
    assert item_key("holding_move", ["000660"]) != item_key("earnings", ["000660"])


# ── 통과 경로 ─────────────────────────────────────────────────────────────────
def test_브리핑이_순위와_생성된_문장을_함께_돌려준다(client: TestClient) -> None:
    response = _get(client, HOLDER)
    assert response.status_code == 200
    content = response.json()["content"]

    assert content["status"] == "ready"
    assert content["date"] == "2025-09-12"
    assert 1 <= len(content["items"]) <= TOP_N

    item = content["items"][0]
    assert item["rank"] == 1
    assert item["category"] == BriefingCategory.HOLDING_MOVE.value
    assert item["related_tickers"] == ["000660"]
    assert item["deeplink"] == "/stocks/000660?tab=ai"
    assert 0.0 < item["relevance_score"] <= 1.0


def test_항목은_정확히_두_문장이고_조각을_이어_붙이면_본문이_된다(client: TestClient) -> None:
    items = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]
    assert items
    for item in items:
        assert len(split_sentences(item["text"])) == 2
        assert "".join(segment["value"] for segment in item["segments"]) == item["text"]
        assert any(segment["type"] == "metric" for segment in item["segments"])


def test_네_건을_넘기지_않고_순위가_1부터_이어진다(client: TestClient) -> None:
    content = _get(client, HOLDER, date=BUSY_DAY).json()["content"]
    assert [item["rank"] for item in content["items"]] == [1, 2, 3, 4]
    scores = [item["relevance_score"] for item in content["items"]]
    assert scores == sorted(scores, reverse=True)


def test_같은_요청은_같은_순서를_돌려준다(client: TestClient) -> None:
    """랭킹이 코드에 있다는 것의 관측 가능한 결과다."""
    first = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]
    second = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]
    assert [i["title"] for i in first] == [i["title"] for i in second]
    assert [i["relevance_score"] for i in first] == [i["relevance_score"] for i in second]


def test_휴장일을_물으면_직전_거래일로_답한다(client: TestClient) -> None:
    # 2025-09-13은 토요일이다.
    assert _get(client, HOLDER, date="2025-09-13").json()["content"]["date"] == "2025-09-12"


def test_응답을_로그에_남긴다(client: TestClient) -> None:
    """내일 novelty가 읽을 행이고, 피드백이 참조할 행이다."""
    response = _get(client, HOLDER)
    row = client.db.added[-1]  # type: ignore[attr-defined]
    assert row.endpoint == "briefing"
    assert row.request_id == response.json()["request_id"]
    assert client.db.commits == 1  # type: ignore[attr-defined]


# ── novelty ───────────────────────────────────────────────────────────────────
def _past_briefing(day: str, category: str, tickers: list[str]) -> dict:
    return {
        "content": {
            "date": day,
            "status": "ready",
            "items": [{"category": category, "related_tickers": tickers}],
        }
    }


def test_어제_나갔던_항목은_뒤로_밀린다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = StubSession(past=[_past_briefing("2025-09-09", "holding_move", ["000660"])])
    with _make_client(monkeypatch, session, FakeClient()) as client:
        items = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]

    # 노출 이력이 없었다면 1위인 종목이다. 0.3이 곱해지면서 네 칸 밖으로 밀린다.
    assert items[0]["related_tickers"] != ["000660"]
    assert all(item["related_tickers"] != ["000660"] for item in items)


def test_같은_날_앞선_호출은_novelty로_세지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """하루에 두 번 열었다고 순서가 달라지면 규칙 엔진으로 랭킹하는 뜻이 없다."""
    session = StubSession(past=[_past_briefing(BUSY_DAY, "holding_move", ["000660"])])
    with _make_client(monkeypatch, session, FakeClient()) as client:
        items = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]
    assert items[0]["related_tickers"] == ["000660"]


def test_칠일보다_오래된_노출은_세지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = StubSession(past=[_past_briefing("2025-08-01", "holding_move", ["000660"])])
    with _make_client(monkeypatch, session, FakeClient()) as client:
        items = _get(client, HOLDER, date=BUSY_DAY).json()["content"]["items"]
    assert items[0]["related_tickers"] == ["000660"]


# ── empty 경로 ────────────────────────────────────────────────────────────────
def test_원장을_못_읽으면_에러가_아니라_빈_브리핑이다(client: TestClient) -> None:
    """§8 — 화면은 `empty`를 보고 섹션을 숨긴다. 409가 아니다."""
    response = _get(client, STRANGER)
    assert response.status_code == 200
    assert response.json()["content"] == {
        "date": date.today().isoformat(),
        "status": "empty",
        "items": [],
    }
    assert client.llm.calls == []  # type: ignore[attr-defined]


def test_알릴_만한_일이_없으면_빈_브리핑이다(client: TestClient) -> None:
    """보유는 있지만 문턱을 넘긴 등락도, 섹터 이동도, 이벤트도 없는 날이다."""
    content = _get(client, LONELY).json()["content"]
    assert content == {"date": "2025-09-12", "status": "empty", "items": []}
    assert client.llm.calls == []  # type: ignore[attr-defined]


def test_거래일보다_이른_날짜를_물으면_빈_브리핑이다(client: TestClient) -> None:
    assert _get(client, HOLDER, date="2020-01-01").json()["content"]["status"] == "empty"


def test_빈_브리핑은_로그에_남기지_않는다(client: TestClient) -> None:
    """볼 것이 없으면 novelty가 읽을 항목도, 피드백을 달 대상도 없다."""
    _get(client, STRANGER)
    assert client.db.added == []  # type: ignore[attr-defined]


# ── 실패 경로 ─────────────────────────────────────────────────────────────────
def test_LLM_키가_없으면_409다(monkeypatch: pytest.MonkeyPatch) -> None:
    """항목은 골라 놓고 문장을 못 쓰는 경우다. 항목마다 null로 흩뿌리지 않는다."""
    with _make_client(monkeypatch, StubSession(), NullLlmClient()) as client:
        response = _get(client, HOLDER)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_DATA"


def test_보유가_없으면_LLM을_부르기_전에_끝난다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`empty`는 에러가 아니므로 키가 없어도 200이어야 한다."""
    with _make_client(monkeypatch, StubSession(), NullLlmClient()) as client:
        assert _get(client, STRANGER).status_code == 200


def test_문장이_전부_막히면_ready가_아니다(monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 목록에 ready를 붙이면 화면이 제목만 있는 섹션을 그린다."""

    class Blocked(FakeClient):
        async def generate(self, **kwargs: Any) -> LlmResult:
            self.calls.append(kwargs)
            # 세 문장이라 문장 수 검사에서 막힌다.
            return LlmResult(
                payload={
                    "narrative": "하나입니다. 둘입니다. 셋입니다.",
                    "used_placeholders": [],
                    "used_citations": [],
                }
            )

    with _make_client(monkeypatch, StubSession(), Blocked()) as client:
        content = _get(client, HOLDER).json()["content"]
    assert content == {"date": "2025-09-12", "status": "empty", "items": []}


# ── 이벤트 경로 ───────────────────────────────────────────────────────────────
# `events`는 아직 0행이라 라우터는 이 경로에서 아무것도 만들지 않는다. 적재기가
# 채우기 전에 배선이 맞는지 보려면 행을 직접 물려 보는 수밖에 없다.
class EventSession(StubSession):
    def __init__(self, rows: list[tuple], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows = rows

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        self.seen.append(sql)
        if "ai_responses" in sql:
            return StubResult(list(self.past))
        return StubResult(list(self.rows))


def _event(event_type: str, ticker: str | None, title: str, importance: float = 0.5) -> tuple:
    return (event_type, ticker, title, date(2025, 9, 12), importance)


def test_실적_공시가_같은_날_등락을_제친다(monkeypatch: pytest.MonkeyPatch) -> None:
    """importance 1.0에 SK하이닉스 비중이면 등락(최대 1.0)과 같은 ŵ에서 반드시 앞선다."""
    rows = [_event("earnings", "000660", "SK하이닉스 3분기 실적 발표")]
    with _make_client(monkeypatch, EventSession(rows), FakeClient()) as client:
        items = _get(client, HOLDER).json()["content"]["items"]

    assert items[0]["category"] == BriefingCategory.EARNINGS.value
    assert items[0]["title"] == "SK하이닉스 3분기 실적 발표"
    assert items[0]["deeplink"] == "/stocks/000660?tab=ai"
    assert len(split_sentences(items[0]["text"])) == 2


def test_이벤트_종류가_분류로_옮겨진다(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _event("filing", "005930", "삼성전자 자기주식 취득 결정"),
        _event("dividend", "105560", "KB금융 분기배당 결정"),
        _event("macro", None, "한국은행 기준금리 결정"),
        _event("product", "035420", "네이버 신규 서비스 공개"),
    ]
    with _make_client(monkeypatch, EventSession(rows), FakeClient()) as client:
        items = _get(client, HOLDER).json()["content"]["items"]

    by_title = {item["title"]: item["category"] for item in items}
    assert by_title["삼성전자 자기주식 취득 결정"] == BriefingCategory.FILING.value
    assert by_title["KB금융 분기배당 결정"] == BriefingCategory.FILING.value
    # 거시는 종목이 없다. 포트폴리오 전체 사안이라 딥링크도 포트폴리오로 간다.
    macro = next(item for item in items if item["title"] == "한국은행 기준금리 결정")
    assert macro["category"] == BriefingCategory.MACRO_EVENT.value
    assert macro["related_tickers"] == []
    assert macro["deeplink"] == "/portfolio"


def test_보유하지_않은_종목의_이벤트는_버린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """쿼리가 걸러 주지만, 원장과 DB가 어긋나면 비중을 못 구한다."""
    rows = [_event("earnings", "999999", "남의 회사 실적")]
    with _make_client(monkeypatch, EventSession(rows), FakeClient()) as client:
        items = _get(client, HOLDER).json()["content"]["items"]
    assert all(item["title"] != "남의 회사 실적" for item in items)


def test_적재기가_낮게_매긴_importance는_표를_깎는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """`events.importance` 기본값(0.5)이면 표 그대로다. 표를 넘기지는 못한다."""
    scores = {}
    for weight in (0.5, 0.1, 1.0):
        rows = [_event("earnings", "000660", "SK하이닉스 실적 발표", importance=weight)]
        with _make_client(monkeypatch, EventSession(rows), FakeClient()) as client:
            items = _get(client, HOLDER).json()["content"]["items"]
        scores[weight] = next(i["relevance_score"] for i in items if i["title"].endswith("발표"))

    assert scores[0.1] < scores[0.5]
    assert scores[1.0] == scores[0.5]

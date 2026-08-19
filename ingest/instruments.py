"""종목 마스터 적재기.

KOSPI · KOSDAQ 전 종목의 기본 정보를 pykrx로 모으고, DART corpCode.xml에서
고유번호를 붙여 instruments 테이블에 upsert 한다.

이 적재기가 Phase 1의 최상단에 있는 이유는 corp_code 때문이다. DART 공시 조회는
종목코드가 아니라 고유번호를 요구하므로, 공시 적재 트랙은 여기서 만들어진
매핑 없이는 시작할 수 없다.

실행:
    python -m ingest.instruments [--date YYYYMMDD]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree

import httpx
from pykrx import stock
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionFactory
from app.core.enums import InstrumentStatus, Market
from app.core.models import Instrument

log = logging.getLogger("ingest.instruments")

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
CORP_CODE_CACHE = Path("data/corpCode.xml")
MARKETS: tuple[Market, ...] = (Market.KOSPI, Market.KOSDAQ)
UPSERT_CHUNK = 500
HTTP_TIMEOUT = 30.0

# NULL이 들어와도 기존 값을 지우지 않는 컬럼. 어느 단계든 건너뛸 수 있으므로
# (DART 키 없음, KRX 업종분류 실패) 부분 실패가 적재된 값을 덮어쓰면 안 된다.
PRESERVING_COLUMNS = ("sector", "sector_code", "corp_code", "market_cap", "listed_shares")


class CorpCodeError(RuntimeError):
    """DART corpCode.xml을 받지 못했거나 해석할 수 없을 때."""


@dataclass(slots=True)
class InstrumentRow:
    """instruments 한 행. 컬럼 이름을 그대로 쓴다."""

    ticker: str
    name: str
    market: str
    sector: str | None = None
    sector_code: str | None = None
    corp_code: str | None = None
    market_cap: int | None = None
    listed_shares: int | None = None
    status: str = InstrumentStatus.LISTED


# ── 정규화 ───────────────────────────────────────────────
def normalize_ticker(value: object) -> str:
    """종목코드를 6자리 문자열로 만든다.

    pandas는 숫자로만 이뤄진 인덱스를 int로 읽어버린다. "005930"이 5930이 되면
    모델의 char_length(ticker) = 6 제약에 걸려 적재 전체가 실패한다.
    """
    raw = str(value).strip() if value is not None else ""
    ticker = raw.zfill(6)
    # 빈 값을 zfill 하면 "000000"이 되어 조용히 통과한다. 먼저 걸러낸다.
    if not raw or len(ticker) != 6 or not ticker.isdigit():
        raise ValueError(f"종목코드가 6자리 숫자가 아니다: {value!r}")
    return ticker


def _to_int(value: object) -> int | None:
    """결측·빈 문자열을 None으로 돌린다. NaN은 int()에서 예외가 난다."""
    if value is None:
        return None
    try:
        number = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return number


def _clean(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


# ── KRX 수집 ─────────────────────────────────────────────
def fetch_sector_index_codes(date: str) -> dict[tuple[str, str], str]:
    """(시장, 업종명) → KRX 업종지수 코드.

    KRX 업종분류(12025)는 업종명만 주고 코드를 주지 않는다. 업종지수 목록의
    이름과 맞춰 코드를 얻는다. 이름 대조지만 양쪽 모두 KRX 자체 분류라
    출처가 하나이므로, 법인명으로 corp_code를 붙이는 것과는 성격이 다르다.
    """
    codes: dict[tuple[str, str], str] = {}
    for market in MARKETS:
        for index_ticker in stock.get_index_ticker_list(date, market):
            name = _clean(stock.get_index_ticker_name(index_ticker))
            if name:
                codes.setdefault((str(market), name), str(index_ticker))
    return codes


def collect_market(date: str, market: Market) -> list[InstrumentRow]:
    """한 시장의 종목을 모은다."""
    caps = stock.get_market_cap_by_ticker(date, str(market))
    if caps is None or caps.empty:
        log.warning("[%s] 시가총액 조회가 비었다. 휴장일이거나 KRX 응답이 없다.", market)
        return []

    universe = {normalize_ticker(t) for t in stock.get_market_ticker_list(date, str(market)) or []}
    sectors = stock.get_market_sector_classifications(date, str(market))
    sector_of: dict[str, str | None] = {}
    name_of: dict[str, str | None] = {}
    if sectors is not None and not sectors.empty:
        for raw_ticker, row in sectors.iterrows():
            ticker = normalize_ticker(raw_ticker)
            sector_of[ticker] = _clean(row.get("업종명"))
            name_of[ticker] = _clean(row.get("종목명"))
    else:
        log.warning("[%s] 업종분류 조회가 비었다. sector 없이 진행한다.", market)

    rows: list[InstrumentRow] = []
    for raw_ticker, cap_row in caps.iterrows():
        ticker = normalize_ticker(raw_ticker)
        if universe and ticker not in universe:
            continue  # ETF·ETN·리츠 등 보통주가 아닌 종목
        name = name_of.get(ticker) or _clean(stock.get_market_ticker_name(ticker))
        if not name:
            log.warning("[%s] %s 종목명을 찾지 못해 건너뛴다.", market, ticker)
            continue
        rows.append(
            InstrumentRow(
                ticker=ticker,
                name=name,
                market=str(market),
                sector=sector_of.get(ticker),
                market_cap=_to_int(cap_row.get("시가총액")),
                listed_shares=_to_int(cap_row.get("상장주식수")),
            )
        )
    log.info("[%s] %d 종목 수집", market, len(rows))
    return rows


def collect_instruments(date: str) -> list[InstrumentRow]:
    """전 시장 종목을 모으고 업종 코드까지 채운다."""
    rows: list[InstrumentRow] = []
    for market in MARKETS:
        rows.extend(collect_market(date, market))

    try:
        sector_codes = fetch_sector_index_codes(date)
    except Exception as exc:  # noqa: BLE001 - 업종 코드는 부가 정보다
        log.warning("업종지수 코드를 얻지 못했다. sector_code 없이 진행한다: %s", exc)
        return rows

    matched = 0
    for row in rows:
        if row.sector:
            row.sector_code = sector_codes.get((row.market, row.sector))
            matched += row.sector_code is not None
    log.info("업종 코드 %d/%d 매칭", matched, len(rows))
    return rows


# ── DART 고유번호 ────────────────────────────────────────
def extract_corp_code_xml(payload: bytes) -> bytes:
    """corpCode.xml 응답(zip)에서 XML 본문을 꺼낸다.

    DART는 zip으로 내려주고, 오류일 때만 XML을 그대로 준다. 압축을 풀지 않고
    파싱하면 원인을 알 수 없는 실패가 된다.
    """
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise CorpCodeError(f"zip이 아니다. DART 응답: {_dart_error(payload)}")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise CorpCodeError(f"zip 안에 XML이 없다: {archive.namelist()}")
        return archive.read(names[0])


def _dart_error(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload.decode("utf-8", "replace"))
    except ElementTree.ParseError:
        return payload[:120].decode("utf-8", "replace")
    status = root.findtext("status") or "?"
    message = root.findtext("message") or "?"
    return f"status={status} message={message}"


def parse_corp_codes(xml_bytes: bytes) -> dict[str, str]:
    """stock_code → corp_code 매핑.

    반드시 stock_code로만 잇는다. corp_name은 읽지도 않는다 — 같은 이름의
    법인이 실제로 존재해서 종목명으로 맞추면 엉뚱한 고유번호가 붙는다.
    """
    root = ElementTree.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for node in root.iter("list"):
        stock_code = (node.findtext("stock_code") or "").strip()
        corp_code = (node.findtext("corp_code") or "").strip()
        if not stock_code or not corp_code:
            continue  # 비상장 법인은 stock_code가 공백이다
        try:
            mapping[normalize_ticker(stock_code)] = corp_code.zfill(8)
        except ValueError:
            log.debug("종목코드 형식이 아니라 건너뛴다: %r", stock_code)
    return mapping


def fetch_corp_codes(api_key: str, client: httpx.Client | None = None) -> dict[str, str]:
    """DART에서 corpCode.xml을 받아 매핑을 만든다."""
    owned = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(CORP_CODE_URL, params={"crtfc_key": api_key})
        response.raise_for_status()
        payload = response.content
    finally:
        if owned:
            client.close()

    xml_bytes = extract_corp_code_xml(payload)
    try:
        CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CORP_CODE_CACHE.write_bytes(xml_bytes)  # data/ 는 .gitignore 대상이다
    except OSError as exc:
        log.debug("corpCode.xml 캐시를 남기지 못했다: %s", exc)
    return parse_corp_codes(xml_bytes)


def load_corp_codes(client: httpx.Client | None = None) -> dict[str, str]:
    """키가 없거나 수집에 실패해도 빈 매핑을 돌려주고 적재를 계속한다."""
    api_key = (settings.dart_api_key or "").strip()
    if not api_key:
        log.warning(
            "DART_API_KEY가 없어 corp_code 단계를 건너뛴다. "
            "종목 마스터는 적재되지만 공시 조회는 키를 넣고 재실행해야 한다."
        )
        return {}
    try:
        mapping = fetch_corp_codes(api_key, client)
    except (CorpCodeError, httpx.HTTPError) as exc:
        log.error("corpCode.xml 수집 실패. corp_code 없이 진행한다: %s", exc)
        return {}
    log.info("DART 고유번호 %d건 확보", len(mapping))
    return mapping


def attach_corp_codes(rows: Sequence[InstrumentRow], mapping: dict[str, str]) -> int:
    """종목코드가 정확히 일치하는 행에만 고유번호를 붙인다."""
    matched = 0
    for row in rows:
        corp_code = mapping.get(row.ticker)
        if corp_code:
            row.corp_code = corp_code
            matched += 1
    if mapping:
        log.info("corp_code %d/%d 매칭", matched, len(rows))
    return matched


# ── 적재 ─────────────────────────────────────────────────
def _chunks(rows: Sequence[InstrumentRow], size: int) -> Iterator[Sequence[InstrumentRow]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def build_upsert_statement(rows: Sequence[InstrumentRow]) -> Insert:
    """ticker 충돌 시 갱신하는 INSERT를 만든다. 재실행해도 중복이 생기지 않는다."""
    statement = pg_insert(Instrument).values([asdict(row) for row in rows])
    updates = {
        "name": statement.excluded.name,
        "market": statement.excluded.market,
        "status": statement.excluded.status,
        "updated_at": func.now(),
    }
    for column in PRESERVING_COLUMNS:
        updates[column] = func.coalesce(
            getattr(statement.excluded, column), getattr(Instrument, column)
        )
    return statement.on_conflict_do_update(index_elements=["ticker"], set_=updates)


async def upsert(rows: Sequence[InstrumentRow]) -> int:
    if not rows:
        return 0
    async with SessionFactory() as session:
        for chunk in _chunks(rows, UPSERT_CHUNK):
            await session.execute(build_upsert_statement(chunk))
        await session.commit()
    return len(rows)


# ── 진입점 ───────────────────────────────────────────────
async def run(date: str | None = None) -> int:
    """수집 → 고유번호 → upsert. 적재한 종목 수를 돌려준다."""
    target = date or stock.get_nearest_business_day_in_a_week()
    log.info("기준일 %s 로 종목 마스터를 적재한다.", target)

    rows = collect_instruments(target)
    if not rows:
        log.error("수집된 종목이 없다. 기준일(%s)과 KRX 응답을 확인할 것.", target)
        return 0

    attach_corp_codes(rows, load_corp_codes())
    count = await upsert(rows)
    log.info("종목 마스터 %d건 적재 완료", count)
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="종목 마스터 적재기")
    parser.add_argument("--date", help="기준일 YYYYMMDD. 생략하면 최근 영업일")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    return 0 if asyncio.run(run(args.date)) else 1


if __name__ == "__main__":
    raise SystemExit(main())

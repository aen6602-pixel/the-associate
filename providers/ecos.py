"""한국은행 ECOS provider — 국고채 수익률(무위험수익률) 등. authoritative."""
from __future__ import annotations

from datetime import date, timedelta

from core.schema import Provenance, Value, DataError, SourceType
from core.http import session
from core import config

_STAT = "817Y002"  # 시장금리(일별)
# 만기 → ECOS 항목코드 (국고채)
_ITEM = {
    "1Y": "010190000", "2Y": "010195000", "3Y": "010200000", "5Y": "010200001",
    "10Y": "010210000", "20Y": "010220000", "30Y": "010230000", "50Y": "010240000",
}

# 신용등급 → 회사채(3년) 수익률 항목코드. 실측 2026-08-27 StatisticItemList/817Y002 로
# 확인한 코드다. 한국은행이 고시하는 등급은 AA- 와 BBB- 두 구간뿐이므로 그 사이 등급은
# 보간하지 않고 **가까운 구간을 쓰고 그 사실을 밝힌다**(없는 정밀도를 만들지 않는다).
_CORP_ITEM = {
    "AA-": "010300000",
    "AA-(민평)": "010310000",
    "BBB-": "010320000",
}
CORP_RATINGS = tuple(_CORP_ITEM)


def _latest_daily(item: str, what: str) -> tuple[float, str]:
    """817Y002(시장금리 일별)에서 해당 항목의 최근 관측치 → (값, YYYYMMDD)."""
    key = config.require(config.Keys.ECOS, "ECOS_API_KEY")
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/"
           f"{_STAT}/D/{start}/{end}/{item}")
    r = session().get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "RESULT" in j:  # 에러 응답
        raise DataError(f"ECOS 오류: {j['RESULT'].get('MESSAGE', '')}")
    rows = [x for x in j.get("StatisticSearch", {}).get("row", [])
            if x.get("DATA_VALUE") not in (None, "", ".")]
    if not rows:
        raise DataError(f"ECOS {what} 관측치를 찾지 못함")
    latest = max(rows, key=lambda x: x["TIME"])
    return round(float(latest["DATA_VALUE"]), 4), latest["TIME"]


def risk_free_rate(tenor: str = "10Y") -> Value:
    """국고채 수익률(무위험수익률). 단위 %."""
    t = tenor.upper()
    item = _ITEM.get(t)
    if item is None:
        raise DataError(f"지원하지 않는 만기: {tenor}. 지원: {list(_ITEM)}")
    val, d = _latest_daily(item, f"국고채({t})")
    return Value(
        value=val, unit="%",
        label=f"국고채({t}) 수익률",
        provenance=Provenance(
            source="한국은행 ECOS",
            source_type=SourceType.AUTHORITATIVE,
            source_url="https://ecos.bok.or.kr/",
            original_field=f"{_STAT}/{item}",
            as_of=f"{d[:4]}-{d[4:6]}-{d[6:]}",
        ),
    )


def corporate_bond_yield(rating: str = "AA-") -> Value:
    """등급별 회사채(3년) 유통수익률. **신규 조달금리의 시장 관측치**로, 과거 조달금리의
    가중평균인 실효 Kd(이자비용÷차입금)와 구분해서 쓴다. 단위 %."""
    r = (rating or "AA-").strip().upper().replace(" ", "")
    item = _CORP_ITEM.get(r) or _CORP_ITEM.get(rating)
    if item is None:
        raise DataError(f"지원하지 않는 등급: {rating}. 한국은행 고시 등급: {list(_CORP_ITEM)}")
    val, d = _latest_daily(item, f"회사채({rating})")
    return Value(
        value=val, unit="%", label=f"회사채(3년, {rating}) 유통수익률",
        provenance=Provenance(
            source="한국은행 ECOS", source_type=SourceType.AUTHORITATIVE,
            source_url="https://ecos.bok.or.kr/", original_field=f"{_STAT}/{item}",
            as_of=f"{d[:4]}-{d[4:6]}-{d[6:]}",
            note="한국은행이 고시하는 등급은 AA- / BBB- 두 구간뿐 — 그 사이 등급은 보간하지 "
                 "않고 가까운 구간을 쓰며 그 사실을 밝힌다."),
    )


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    key = config.require(config.Keys.ECOS, "ECOS_API_KEY")
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=14)).strftime("%Y%m%d")
    j = probe("GET", f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1/"
                     f"{_STAT}/D/{start}/{end}/{_ITEM['10Y']}").json()
    if "RESULT" in j:
        raise DataError(f"ECOS 오류: {j['RESULT'].get('MESSAGE', '')}")
    return "시장금리(국고채) 조회 OK"

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


def risk_free_rate(tenor: str = "10Y") -> Value:
    """국고채 수익률(무위험수익률). 단위 %."""
    t = tenor.upper()
    item = _ITEM.get(t)
    if item is None:
        raise DataError(f"지원하지 않는 만기: {tenor}. 지원: {list(_ITEM)}")
    key = config.require(config.Keys.ECOS, "ECOS_API_KEY")

    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/"
           f"{_STAT}/D/{start}/{end}/{item}")
    r = session().get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "RESULT" in j:  # 에러 응답
        msg = j["RESULT"].get("MESSAGE", "")
        raise DataError(f"ECOS 오류: {msg}")
    rows = j.get("StatisticSearch", {}).get("row", [])
    rows = [x for x in rows if x.get("DATA_VALUE") not in (None, "", ".")]
    if not rows:
        raise DataError("ECOS 국고채 관측치를 찾지 못함")
    latest = max(rows, key=lambda x: x["TIME"])
    d = latest["TIME"]
    return Value(
        value=round(float(latest["DATA_VALUE"]), 4), unit="%",
        label=f"국고채({t}) 수익률",
        provenance=Provenance(
            source="한국은행 ECOS",
            source_type=SourceType.AUTHORITATIVE,
            source_url="https://ecos.bok.or.kr/",
            original_field=f"{_STAT}/{item}",
            as_of=f"{d[:4]}-{d[4:6]}-{d[6:]}",
        ),
    )

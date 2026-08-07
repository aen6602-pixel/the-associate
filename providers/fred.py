"""FRED provider (미국 연준) — 무위험수익률 등. authoritative."""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json
from core import config

# 만기 → FRED 시리즈 ID (미국 국채 상수만기 수익률)
_SERIES = {
    "3M": "DGS3MO", "1Y": "DGS1", "2Y": "DGS2", "3Y": "DGS3", "5Y": "DGS5",
    "7Y": "DGS7", "10Y": "DGS10", "20Y": "DGS20", "30Y": "DGS30",
}


def risk_free_rate(tenor: str = "10Y") -> Value:
    """미국 국채 수익률(무위험수익률 대용). 단위 %."""
    t = tenor.upper()
    series = _SERIES.get(t)
    if series is None:
        raise DataError(f"지원하지 않는 만기: {tenor}. 지원: {list(_SERIES)}")
    key = config.require(config.Keys.FRED, "FRED_API_KEY")
    data = get_json(
        "https://api.stlouisfed.org/fred/series/observations",
        ttl_hours=12,
        params={"series_id": series, "api_key": key, "file_type": "json",
                "sort_order": "desc", "limit": 10},
    )
    for o in data.get("observations", []):
        if o["value"] not in (".", "", None):
            return Value(
                value=round(float(o["value"]), 4), unit="%",
                label=f"US Treasury {t} yield",
                provenance=Provenance(
                    source="FRED (US Federal Reserve)",
                    source_type=SourceType.AUTHORITATIVE,
                    source_url=f"https://fred.stlouisfed.org/series/{series}",
                    original_field=series, as_of=o["date"],
                ),
            )
    raise DataError(f"FRED {series} 유효 관측치를 찾지 못함")

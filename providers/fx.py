"""환율 provider — frankfurter.app (ECB 기준, 키 불필요)."""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json


def fx_rate(base: str, quote: str, date: str | None = None) -> Value:
    """1 단위 base 통화당 quote 통화 금액.

    date: "YYYY-MM-DD" (미지정 시 최신). ECB 는 영업일만 제공 → 가장 가까운 이전 영업일로 반환됨.
    """
    base, quote = base.upper(), quote.upper()
    endpoint = date if date else "latest"
    url = f"https://api.frankfurter.app/{endpoint}"
    data = get_json(url, ttl_hours=12, params={"from": base, "to": quote})
    rates = data.get("rates", {})
    if quote not in rates:
        raise DataError(f"환율을 찾지 못함: {base}->{quote} ({endpoint})")
    return Value(
        value=rates[quote],
        unit=f"{quote}/{base}",
        label=f"FX {base}->{quote}",
        provenance=Provenance(
            source="ECB (via frankfurter.app)",
            source_type=SourceType.AUTHORITATIVE,  # 유럽중앙은행 기준환율
            source_url=url + f"?from={base}&to={quote}",
            as_of=data.get("date"),
            original_field=f"rates.{quote}",
        ),
    )

"""환율 provider — frankfurter.app (ECB 기준, 키 불필요) + Yahoo 보완.

ECB 기준환율은 TWD 를 고시하지 않는다(실측 2026-08: USD->TWD 가 404). 대만 기업이 낀
크로스보더 comps 에서 통화를 통일하려면 이 통화가 반드시 필요하므로, ECB 에 없는 쌍만
Yahoo FX(`USDTWD=X`)로 보완하고 등급을 reference 로 낮춘다.
"""
from __future__ import annotations

import requests

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json

# ECB 기준환율 고시 통화가 아니라 Yahoo 로 우회해야 하는 통화(실측으로 확인된 것만 등재).
_NOT_IN_ECB = {"TWD"}


def fx_rate(base: str, quote: str, date: str | None = None) -> Value:
    """1 단위 base 통화당 quote 통화 금액.

    date: "YYYY-MM-DD" (미지정 시 최신). ECB 는 영업일만 제공 → 가장 가까운 이전 영업일로 반환됨.
    ECB 미고시 통화는 Yahoo 로 우회한다(등급 reference, note 에 그 사실을 남긴다).
    """
    base, quote = base.upper(), quote.upper()
    if base in _NOT_IN_ECB or quote in _NOT_IN_ECB:
        return _via_yahoo(base, quote, date)
    endpoint = date if date else "latest"
    url = f"https://api.frankfurter.app/{endpoint}"
    try:
        data = get_json(url, ttl_hours=12, params={"from": base, "to": quote})
    except requests.HTTPError as e:
        # 404 = ECB 가 그 통화를 고시하지 않음. 조용히 실패하지 말고 대체 경로를 탄다.
        if getattr(e.response, "status_code", None) in (404, 422):
            return _via_yahoo(base, quote, date)
        raise
    rates = data.get("rates", {})
    if quote not in rates:
        return _via_yahoo(base, quote, date)
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


def _via_yahoo(base: str, quote: str, date: str | None) -> Value:
    """ECB 미고시 통화쌍. 과거 특정일 환율은 Yahoo 일별 종가에서 뽑는다."""
    from providers import yahoo

    if date:
        symbol = f"{base}{quote}=X" if base != "USD" else f"{quote}=X"
        v = yahoo.close_on_or_before(symbol, str(date).replace("-", ""))
        return Value(
            v.value, f"{quote}/{base}", label=f"FX {base}->{quote} ({v.provenance.as_of})",
            provenance=Provenance(
                source="Yahoo Finance (FX)", source_type=SourceType.REFERENCE,
                source_url=v.provenance.source_url, original_field=f"{symbol} close",
                as_of=v.provenance.as_of,
                note=(f"ECB 기준환율에 {quote if quote in _NOT_IN_ECB else base} 고시가 없어 "
                      "Yahoo 로 우회. 중앙은행 고시가 아니라 집계 호가(reference)."),
            ),
        )
    return yahoo.fx_rate(base, quote)

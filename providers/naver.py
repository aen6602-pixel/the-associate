"""네이버 금융 시세 provider (KRX 시세 우회 — 사내 프록시에서 data.krx 직접 접근 불가).

시가총액·종가를 m.stock.naver.com 통합 API 에서 가져온다. comps 의 peer 시가총액에 사용.
값의 원천은 거래소(KRX) 시세(네이버가 집계) → authoritative.
"""
from __future__ import annotations

import re

from core.schema import Provenance, Value, DataError, SourceType
from core.http import session

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}


def _fetch(stock_code: str) -> dict:
    r = session().get(f"https://m.stock.naver.com/api/stock/{stock_code}/integration",
                      headers=_UA, timeout=15)
    if r.status_code == 409:
        # 실측 확인(2026-08): 상장폐지된 종목(예: 신성통상 005390, 공개매수 후 상장폐지)이
        # 이 코드로 응답한다({"code":"StockConflict"}) — 일시적 서버 오류가 아니라 더 이상
        # 시세가 존재하지 않는 상태. 재시도로 해결되지 않으므로 원인을 명시해 바로 알려준다.
        try:
            code = r.json().get("code", "")
        except ValueError:
            code = ""
        raise DataError(
            f"종목코드 {stock_code} 시세 조회 실패(HTTP 409{f', code={code}' if code else ''}). "
            "재시도로 해결되는 일시적 오류가 아니라, 상장폐지·거래정지 등으로 더 이상 실시간 "
            "시세가 없는 종목일 가능성이 높습니다. DART 공시 이력(상장폐지결정 등)으로 확인하세요."
        )
    r.raise_for_status()
    return r.json()


def _parse_kr_amount(s: str) -> int | None:
    """'1,350조 4,904억' → 정수(원)."""
    s = (s or "").replace(",", "").replace(" ", "")
    total, found = 0, False
    for unit, mul in (("조", 10 ** 12), ("억", 10 ** 8), ("만", 10 ** 4)):
        m = re.search(rf"(\d+){unit}", s)
        if m:
            total += int(m.group(1)) * mul
            found = True
    return total if found else None


def _info_map(j: dict) -> dict:
    return {it.get("code"): it.get("value") for it in j.get("totalInfos", []) if it.get("code")}


def snapshot(stock_code: str, name: str | None = None) -> dict:
    """{'market_cap': Value, 'price': Value}. stock_code=6자리."""
    if not (stock_code and stock_code.isdigit() and len(stock_code) == 6):
        raise DataError(f"유효한 6자리 종목코드가 필요합니다: {stock_code!r} (비상장은 시가총액 없음)")
    j = _fetch(stock_code)
    nm = name or j.get("stockName") or stock_code
    info = _info_map(j)
    url = f"https://m.stock.naver.com/domestic/stock/{stock_code}/total"

    mv = _parse_kr_amount(info.get("marketValue", ""))
    if not mv:
        raise DataError(f"{nm}({stock_code}) 시가총액을 네이버에서 못 구함")
    price_s = (j.get("closePrice") or info.get("closePrice") or "").replace(",", "")
    price = int(price_s) if price_s.isdigit() else None

    prov = Provenance(
        source="네이버 금융(KRX 시세)", source_type=SourceType.AUTHORITATIVE,
        source_url=url, original_field="시가총액(marketValue)", note="종가 기준 시가총액",
    )
    out = {"market_cap": Value(mv, "KRW", label=f"{nm} 시가총액", provenance=prov)}
    if price is not None:
        out["price"] = Value(price, "KRW", label=f"{nm} 종가",
                             provenance=Provenance(source="네이버 금융(KRX 시세)",
                                                   source_type=SourceType.AUTHORITATIVE,
                                                   source_url=url, original_field="종가(closePrice)"))
    return out


def market_cap(stock_code: str, name: str | None = None) -> Value:
    return snapshot(stock_code, name)["market_cap"]

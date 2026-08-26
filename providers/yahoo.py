"""Yahoo Finance 시세 provider — 해외(미국·일본·대만) 주가/지수 시계열.

베타 회귀에 필요한 건 종가 시계열뿐이고, Yahoo 의 **chart 엔드포인트는 키·쿠키 없이** 열려
있다(실측 2026-08: AAPL/^GSPC/7203.T/^N225/2330.TW/^TWII 전부 5년 주봉 263개 관측치 수신).
`quoteSummary`·`quote` 계열은 crumb 인증을 요구해 401 이 나므로 쓰지 않는다.

주의:
- 공식 API 가 아니다(yfinance 라이브러리도 같은 엔드포인트를 감싼다). 스펙이 바뀌거나
  레이트리밋이 걸릴 수 있으므로 실패 시 원인을 분명히 알리고, 응답은 core.http 가 캐시한다.
- 국내(한국) 주가는 네이버 금융 provider 를 쓴다 — KRX 시세를 더 안정적으로 준다.
"""
from __future__ import annotations

from core.http import session
from core.schema import DataError, Provenance, SourceType, Value

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 주기 → Yahoo interval
_INTERVAL = {"day": "1d", "week": "1wk", "month": "1mo"}

# 시장 → (기본 지수 심볼, 지수 표시명, 종목 심볼 접미사)
MARKETS = {
    "US": ("^GSPC", "S&P 500", ""),
    "JP": ("^N225", "Nikkei 225", ".T"),
    "TW": ("^TWII", "TAIEX", ".TW"),
    "HK": ("^HSI", "Hang Seng", ".HK"),
    "KR": ("^KS11", "KOSPI", ".KS"),   # 국내는 보통 네이버를 쓰지만 대체 경로로 열어둔다
}


def market_index(market: str) -> tuple[str, str]:
    m = (market or "US").strip().upper()
    if m not in MARKETS:
        raise DataError(f"지원하지 않는 시장: {market} (지원: {', '.join(MARKETS)})")
    sym, name, _ = MARKETS[m]
    return sym, name


def _fetch(symbol: str, period: str, years: int) -> list[dict]:
    interval = _INTERVAL.get(period)
    if interval is None:
        raise DataError(f"period 는 {tuple(_INTERVAL)} 중 하나여야 합니다: {period!r}")
    url = f"{_CHART}{symbol}?range={int(years)}y&interval={interval}"
    r = session().get(url, headers=_UA, timeout=30)
    if r.status_code == 404:
        raise DataError(f"Yahoo 에서 심볼 '{symbol}' 을 찾지 못했습니다. "
                        f"거래소 접미사를 확인하세요(일본 .T, 대만 .TW, 홍콩 .HK).")
    if r.status_code == 429:
        raise DataError("Yahoo Finance 레이트리밋(429). 잠시 후 다시 시도하세요.")
    r.raise_for_status()

    try:
        j = r.json()
        result = (j.get("chart", {}).get("result") or [])[0]
        stamps = result.get("timestamp") or []
        closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    except (ValueError, IndexError, KeyError, TypeError) as e:
        raise DataError(f"Yahoo 응답을 해석하지 못했습니다({symbol}): {e}") from e

    from datetime import datetime, timezone

    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue  # 거래정지·결측 구간
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        out.append({"date": d, "close": float(close)})
    if len(out) < 2:
        raise DataError(f"Yahoo 시계열 관측치가 부족합니다({len(out)}개): {symbol}")
    return out


def price_series(symbol: str, period: str = "week", years: int = 5) -> list[dict]:
    """개별종목 종가 시계열 [{'date','close'}, ...] (과거→최신). symbol 예: AAPL, 7203.T, 2330.TW."""
    if not (symbol or "").strip():
        raise DataError("Yahoo 심볼이 필요합니다 (예: AAPL, 7203.T, 2330.TW).")
    return _fetch(symbol.strip(), period, years)


def index_series(market: str = "US", period: str = "week", years: int = 5) -> list[dict]:
    """시장지수 종가 시계열."""
    sym, _ = market_index(market)
    return _fetch(sym, period, years)


def last_close(symbol: str) -> Value:
    """최근 종가 (통화 포함). 시가총액은 crumb 인증이 필요해 제공하지 않는다."""
    url = f"{_CHART}{symbol}?range=1mo&interval=1d"
    r = session().get(url, headers=_UA, timeout=30)
    r.raise_for_status()
    try:
        result = (r.json().get("chart", {}).get("result") or [])[0]
        meta = result.get("meta", {})
        closes = [c for c in ((result.get("indicators", {}).get("quote") or [{}])[0]
                              .get("close") or []) if c is not None]
    except (ValueError, IndexError, KeyError, TypeError) as e:
        raise DataError(f"Yahoo 응답 해석 실패({symbol}): {e}") from e
    if not closes:
        raise DataError(f"{symbol} 의 종가를 찾지 못했습니다.")
    return Value(
        round(float(closes[-1]), 4), meta.get("currency") or "", label=f"{symbol} 종가",
        provenance=Provenance(
            source="Yahoo Finance", source_type=SourceType.REFERENCE,
            source_url=f"https://finance.yahoo.com/quote/{symbol}",
            original_field="chart/meta+close",
            note=("비공식 공개 엔드포인트(키 불필요). 거래소 공식 시세가 아니라 집계 시세이므로 "
                  "참조(reference) 등급으로 표기한다.")),
    )

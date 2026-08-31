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


def close_on_or_before(symbol: str, target: str | None = None) -> Value:
    """target(YYYYMMDD) **이하**의 가장 가까운 거래일 종가. 미지정 시 최신 거래일.

    크로스보더 comps 에서 4개 시장의 시가총액을 **같은 거래일**로 맞추려면 "최신 종가" 로는
    안 된다 — 시장별 휴장일·시차 때문에 최신 거래일이 서로 다르다(실측 2026-08-27:
    KRX 는 08-27, 나스닥·TWSE 는 08-26 이 마지막). 공통 기준일을 정한 뒤 각 종목에서
    그 날짜 이하의 종가를 뽑는 방식으로만 분자(시총)의 기준일을 통일할 수 있다.
    """
    if not (symbol or "").strip():
        raise DataError("Yahoo 심볼이 필요합니다 (예: AAPL, 7203.T, 2330.TW).")
    years = 1
    if target:
        from datetime import date

        gap = date.today().year - int(str(target)[:4])
        years = max(1, min(10, gap + 1))
    series = _fetch(symbol.strip(), "day", years)
    rows = [x for x in series if not target or x["date"] <= str(target)]
    if not rows:
        raise DataError(f"{symbol}: {target} 이전의 거래일 종가가 시계열에 없습니다 "
                        f"(가용 최초 {series[0]['date']}).")
    row = rows[-1]
    return Value(
        round(float(row["close"]), 4), _currency(symbol),
        label=f"{symbol} 종가 ({row['date']})",
        provenance=Provenance(
            source="Yahoo Finance", source_type=SourceType.REFERENCE,
            source_url=f"https://finance.yahoo.com/quote/{symbol}",
            original_field="chart/indicators.quote.close", as_of=row["date"],
            note=("비공식 공개 엔드포인트(키 불필요). 거래소 공식 시세가 아니라 집계 시세이므로 "
                  "참조(reference) 등급."
                  + (f" 요청 기준일 {target} 이하의 최근 거래일." if target else "")),
        ),
    )


def _currency(symbol: str) -> str:
    """심볼 접미사 → 표시통화. 시가총액 단위를 잘못 붙이면 배수가 조용히 틀린다."""
    s = symbol.upper()
    for suffix, cur in ((".TW", "TWD"), (".T", "JPY"), (".HK", "HKD"),
                        (".KS", "KRW"), (".KQ", "KRW")):
        if s.endswith(suffix):
            return cur
    return "USD"


def fx_rate(base: str, quote: str) -> Value:
    """환율(1 base → quote). ECB 기준환율에 없는 통화(TWD 등)의 대체 경로.

    실측 2026-08: frankfurter(ECB)는 TWD 를 제공하지 않아 USD/TWD 조회가 404 다.
    Yahoo 의 `USDTWD=X` 심볼은 열려 있어(31.806) 이 경로로 보완한다 — 다만 중앙은행
    기준환율이 아니라 집계 호가이므로 reference 등급으로 낮춰 표기한다.
    """
    base, quote = base.strip().upper(), quote.strip().upper()
    if base == quote:
        raise DataError(f"같은 통화의 환율은 의미가 없습니다: {base}->{quote}")
    symbol = f"{base}{quote}=X" if base != "USD" else f"{quote}=X"
    v = close_on_or_before(symbol)
    return Value(
        v.value, f"{quote}/{base}", label=f"FX {base}->{quote}",
        provenance=Provenance(
            source="Yahoo Finance (FX)", source_type=SourceType.REFERENCE,
            source_url=f"https://finance.yahoo.com/quote/{symbol}",
            original_field=f"{symbol} close", as_of=v.provenance.as_of,
            note="ECB 기준환율에 없는 통화의 대체 경로. 중앙은행 고시가 아니라 집계 호가.",
        ),
    )


def last_close(symbol: str) -> Value:
    """최근 종가 (통화 포함).

    시가총액 자체를 주는 엔드포인트(quoteSummary)는 crumb 인증이 막혀 쓸 수 없지만,
    **시가총액을 못 구한다는 뜻이 아니다** — 종가 × 발행주식수(SEC/EDINET/FinMind 공시)로
    조립할 수 있고 그 경로가 engines.market_data.market_cap (도구: get_market_cap) 이다.
    """
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


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    j = probe("GET", f"{_CHART}AAPL", headers=_UA,
              params={"range": "5d", "interval": "1d"}).json()
    if not ((j.get("chart") or {}).get("result")):
        raise DataError("Yahoo 차트 응답이 비어 있습니다")
    return "해외 시세 조회 OK"

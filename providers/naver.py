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


# ── 주가/지수 시계열 (베타 회귀용) ─────────────────────────────────
# 실측 확인(2026-08): 개별종목·지수 모두 일/주/월봉이 2019년부터 조회된다.
_PERIODS = ("day", "week", "month")


def _chart(path: str, period: str, years: int) -> list[dict]:
    if period not in _PERIODS:
        raise DataError(f"period 는 {_PERIODS} 중 하나여야 합니다: {period!r}")
    from datetime import date

    today = date.today()
    start = today.replace(year=today.year - int(years))
    url = (f"https://api.stock.naver.com/chart/domestic/{path}/{period}"
           f"?startDateTime={start:%Y%m%d}0000&endDateTime={today:%Y%m%d}0000")
    r = session().get(url, headers=_UA, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise DataError(f"시계열을 못 받았습니다: {url}")
    out = []
    for row in rows:
        close = row.get("closePrice")
        d = row.get("localDate")
        if close is None or not d:
            continue
        try:
            out.append({"date": str(d), "close": float(close)})
        except (TypeError, ValueError):
            continue
    if len(out) < 2:
        raise DataError(f"시계열 관측치가 부족합니다({len(out)}개): {url}")
    return out


def price_series(stock_code: str, period: str = "week", years: int = 5) -> list[dict]:
    """개별종목 종가 시계열 [{'date','close'}, ...] (과거→최신)."""
    if not (stock_code and stock_code.isdigit() and len(stock_code) == 6):
        raise DataError(f"유효한 6자리 종목코드가 필요합니다: {stock_code!r}")
    return _chart(f"item/{stock_code}", period, years)


def close_on_or_before(stock_code: str, target: str | None = None,
                       name: str | None = None) -> Value:
    """target(YYYYMMDD) 이하의 가장 가까운 거래일 종가. 미지정 시 최신 거래일.

    통합 API 의 closePrice 는 응답에 없을 때가 있어(실측 2026-08: 삼성전자·SK하이닉스
    모두 키 부재) 일별 차트 시계열에서 뽑는다.
    """
    years = 1
    if target:
        from datetime import date

        years = max(1, min(10, date.today().year - int(str(target)[:4]) + 1))
    series = price_series(stock_code, "day", years)
    rows = [x for x in series if not target or x["date"] <= str(target)]
    if not rows:
        raise DataError(f"{stock_code}: {target} 이전 거래일 종가가 없습니다 "
                        f"(가용 최초 {series[0]['date']}).")
    row = rows[-1]
    return Value(
        row["close"], "KRW", label=f"{name or stock_code} 종가 ({row['date']})",
        provenance=Provenance(
            source="네이버 금융(KRX 시세)", source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://m.stock.naver.com/domestic/stock/{stock_code}/total",
            original_field="chart/domestic/item closePrice", as_of=row["date"],
            note=f"일별 종가 시계열에서 {target or '최신'} 이하 최근 거래일 종가",
        ),
    )


def implied_common_shares(stock_code: str, name: str | None = None) -> Value:
    """KRX 시가총액 ÷ 최근 종가 = **유통 보통주식수**(내재).

    DART 의 발행주식총수를 시가총액 계산에 쓰면 안 된다(실측 2026-08-27):
      삼성전자  DART 8,975,138,200주(보통 7,780,466,850 + 우선 1,194,671,350)
                vs KRX 시총÷종가 5,835,247,925주  → +53.8% 과대
      SK하이닉스 DART 5,721,980,209주 vs KRX 내재 730,492,379주 → +683% 과대
    DART '주식총수현황' 은 **발행한 주식의 누적 총수**(우선주·소각분 포함)라서 유통
    보통주식수와 다르다. 시가총액은 거래소가 이미 보통주 기준으로 계산해 주므로,
    그 시총을 종가로 되나눠 주식수를 역산하는 쪽이 항상 자기일관적이다.
    이 주식수는 과거 기준일 시총을 `내재주식수 × 그 날 종가` 로 환산할 때 쓴다.
    """
    mv = market_cap(stock_code, name)
    px = close_on_or_before(stock_code, None, name)
    shares = mv.value / px.value
    return Value(
        round(shares), "주", label=f"{name or stock_code} 내재 유통보통주식수",
        provenance=Provenance(
            source="계산(KRX 시가총액 ÷ 종가)", source_type=SourceType.COMPUTED,
            source_url=mv.provenance.source_url,
            original_field="marketValue / closePrice", as_of=px.provenance.as_of,
            note=(f"KRX 시가총액 {mv.value:,} ÷ 종가({px.provenance.as_of}) "
                  f"{px.value:,.0f} = {shares:,.0f}주. DART 발행주식총수(우선주·누적발행 "
                  f"포함)와 다르며, 시가총액 계산에는 이 값을 쓴다."),
        ),
        extras={"market_cap": mv, "price": px},
    )


def index_series(index: str = "KOSPI", period: str = "week", years: int = 5) -> list[dict]:
    """시장지수 종가 시계열. index: KOSPI | KOSDAQ."""
    idx = (index or "KOSPI").strip().upper()
    if idx not in ("KOSPI", "KOSDAQ"):
        raise DataError(f"지원하지 않는 지수: {index} (KOSPI, KOSDAQ)")
    return _chart(f"index/{idx}", period, years)

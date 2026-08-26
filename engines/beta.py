"""베타 엔진 — 베타를 '가정' 이 아니라 데이터로 만든다.

두 경로:
  1) **회귀베타(상장사)**: 네이버 금융(KRX 시세)의 주가/지수 시계열로 OLS 회귀.
     β = Cov(주식수익률, 시장수익률) / Var(시장수익률)
     기본은 5년 주봉(관측치 ≈ 260) — Bloomberg 2년 주봉, Damodaran 5년 월봉의 중간 지점으로,
     표본이 충분하면서도 사업구조 변화를 과하게 반영하지 않는 구간.
  2) **산업베타(비상장사·신설법인)**: Damodaran 산업 무차입베타를 회사의 자본구조로
     재레버리지(Hamada).  βL = βU × [1 + (1 − t) × D/E]

회귀베타는 R²·관측치수를 함께 돌려준다 — R² 가 낮으면(예: <0.1) 그 베타는 신뢰하기 어렵다는
사실을 숨기지 않는다.
"""
from __future__ import annotations

from core.schema import DataError, Provenance, SourceType, Value
from providers import damodaran, dart, naver, yahoo


def _returns(series: list[dict]) -> dict[str, float]:
    """{날짜: 수익률}. 연속된 두 관측치의 단순수익률."""
    out = {}
    for prev, cur in zip(series, series[1:]):
        if prev["close"]:
            out[cur["date"]] = cur["close"] / prev["close"] - 1
    return out


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """(기울기, 절편, R²). 외부 의존성 없이 계산."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        raise DataError("시장수익률의 분산이 0 이라 베타를 계산할 수 없습니다.")
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy ** 2) / (sxx * syy) if syy else 0.0
    return slope, intercept, r2


def regression_beta(company: str, period: str = "week", years: int = 5,
                    index: str = "KOSPI", market: str = "KR",
                    symbol: str | None = None) -> Value:
    """레버드베타 — 주가·시장지수 시계열 OLS 회귀.

    market='KR' 이면 회사명을 DART 로 해석해 종목코드를 얻고 네이버(KRX) 시세를 쓴다.
    그 외 시장(US/JP/TW/HK)은 Yahoo 를 쓰며, `symbol` 로 티커를 직접 준다
    (예: AAPL, 7203.T, 2330.TW). symbol 을 생략하면 company 를 티커로 간주한다.
    """
    mkt = (market or "KR").strip().upper()

    if mkt == "KR" and not symbol:
        ent = dart.resolve(company)
        code = ent.get("stock_code")
        if not code:
            raise DataError(
                f"{ent['corp_name']} 는 상장 종목코드가 없어(비상장) 회귀베타를 낼 수 없습니다. "
                f"산업 무차입베타를 재레버리지하는 industry_beta 를 쓰세요.")
        name, ticker = ent["corp_name"], code
        stock = naver.price_series(code, period, years)
        market_series = naver.index_series(index, period, years)
        index_name = index
        src_name = "네이버 금융(KRX 시세)"
        src_url = f"https://m.stock.naver.com/domestic/stock/{code}/total"
    else:
        ticker = (symbol or company or "").strip()
        if not ticker:
            raise DataError("해외 종목은 Yahoo 심볼(symbol)이 필요합니다. 예: AAPL, 7203.T")
        name = ticker
        stock = yahoo.price_series(ticker, period, years)
        market_series = yahoo.index_series(mkt, period, years)
        _, index_name = yahoo.market_index(mkt)
        src_name = "Yahoo Finance"
        src_url = f"https://finance.yahoo.com/quote/{ticker}"

    sr, mr = _returns(stock), _returns(market_series)
    dates = sorted(set(sr) & set(mr))
    if len(dates) < 30:
        raise DataError(f"공통 관측치가 {len(dates)}개뿐이라 회귀가 불안정합니다 "
                        f"(period={period}, years={years}).")

    ys = [sr[d] for d in dates]
    xs = [mr[d] for d in dates]
    beta, _, r2 = _ols(xs, ys)

    warn = ""
    if r2 < 0.1:
        warn = f" ⚠️ R² {r2:.3f} 가 낮아 시장과의 설명력이 약함 — 산업베타 병행 검토 권장."
    return Value(
        round(beta, 4), "배", label=f"{name} 레버드베타(회귀)",
        provenance=Provenance(
            source=f"계산엔진(engines.beta) · {src_name}",
            source_type=SourceType.COMPUTED,
            source_url=src_url,
            as_of=dates[-1],
            note=(f"{index_name} 대비 {period}봉 {years}년 OLS 회귀, 관측치 {len(dates)}개, "
                  f"R² {r2:.3f}. β = Cov(주식,시장)/Var(시장). "
                  f"기간 {dates[0]}~{dates[-1]}.{warn}"),
        ),
        extras={"r_squared": Value(
            round(r2, 4), "", label="회귀 결정계수 R²",
            provenance=Provenance(source="계산엔진(engines.beta)",
                                  source_type=SourceType.COMPUTED,
                                  source_url="(computed)",
                                  note="1에 가까울수록 시장이 주가변동을 잘 설명"))},
    )


def relever(unlevered_beta: float, de_ratio: float, tax_rate_pct: float) -> float:
    """Hamada: βL = βU × [1 + (1 − t) × D/E]"""
    return float(unlevered_beta) * (1 + (1 - float(tax_rate_pct) / 100) * float(de_ratio))


def unlever(levered_beta: float, de_ratio: float, tax_rate_pct: float) -> float:
    return float(levered_beta) / (1 + (1 - float(tax_rate_pct) / 100) * float(de_ratio))


def industry_beta(industry: str, country: str = "KR", de_ratio: float | None = None,
                  tax_rate_pct: float | None = None) -> Value:
    """산업 무차입베타를 목표 자본구조로 재레버리지한 베타.

    de_ratio 를 안 주면 그 산업의 평균 D/E 를, tax_rate_pct 를 안 주면 산업 실효세율을 쓴다
    (즉 아무것도 안 주면 산업 평균 레버드베타에 수렴한다)."""
    region = damodaran.region_for(country)
    m = damodaran.industry_metrics(industry, region)
    if "unlevered_beta" not in m:
        raise DataError(f"'{industry}' 산업의 무차입베타가 데이터셋에 없습니다.")

    bu = m["unlevered_beta"].value
    de = de_ratio if de_ratio is not None else (
        m["de_ratio"].value if "de_ratio" in m else None)
    if de is None:
        raise DataError(f"'{industry}' 산업의 D/E 가 없어 재레버리지할 수 없습니다. "
                        f"de_ratio 를 직접 지정하세요.")
    tax = tax_rate_pct if tax_rate_pct is not None else (
        m["effective_tax_rate"].value if "effective_tax_rate" in m else 0.0)

    bl = relever(bu, de, tax)
    name = m["industry_name"].label
    src = m["unlevered_beta"].provenance
    return Value(
        round(bl, 4), "배", label=f"{name} 재레버리지 베타 ({region})",
        provenance=Provenance(
            source="계산엔진(engines.beta) · Damodaran 산업베타",
            source_type=SourceType.COMPUTED, source_url=src.source_url, as_of=src.as_of,
            note=(f"βL = βU {bu:.4f} × [1 + (1 − {tax:.2f}%) × D/E {de:.4f}] = {bl:.4f} "
                  f"(Hamada). 산업 '{name}', {region} 데이터셋."),
        ),
        extras={"unlevered_beta": m["unlevered_beta"]},
    )


def beta_for(company: str, industry: str | None = None, country: str = "KR",
             period: str = "week", years: int = 5, index: str = "KOSPI",
             market: str | None = None, symbol: str | None = None) -> Value:
    """최선의 경로로 베타를 구한다 — 상장사는 회귀, 실패하거나 비상장이면 산업베타.

    market 를 안 주면 country 를 시장으로 본다(KR→네이버/KOSPI, 그 외→Yahoo)."""
    mkt = (market or country or "KR").strip().upper()
    try:
        return regression_beta(company, period, years, index, mkt, symbol)
    except DataError as e:
        if industry is None:
            raise DataError(f"{e} (industry 를 함께 주면 산업베타로 대체 계산할 수 있습니다.)")
        v = industry_beta(industry, country)
        v.provenance.note = f"회귀베타 불가({e}) → 산업베타 사용. " + (v.provenance.note or "")
        return v

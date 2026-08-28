"""베타 엔진 — 베타를 '가정' 이 아니라 데이터로 만든다.

두 경로:
  1) **회귀베타(상장사)**: 네이버 금융(KRX 시세)의 주가/지수 시계열로 OLS 회귀.
     β = Cov(주식수익률, 시장수익률) / Var(시장수익률)
     기본은 **5년 월봉**(Damodaran 관례). 예전 기본값이던 5년 주봉보다 설명력이 일관되게
     높다 — 실측(2026-08, R² 월봉5년 vs 주봉5년):
       삼성전자 0.813 vs 0.659 · 현대차 0.595 vs 0.378 · 기아 0.385 vs 0.222
     짧은 간격일수록 비동기거래·호가스프레드 잡음이 섞여 공분산이 희석되기 때문이다.
     보조로 2년 주봉(Bloomberg 관례)을 함께 계산해 안정성을 보여준다.

     ⚠️ **R² 가 가장 높은 창을 고르지 않는다.** 그건 데이터 마이닝이고, 실측에서 1년 일봉이
     대개 R² 가 제일 높게 나오는데(관측치 250개) 베타 자체는 가장 불안정하다
     (현대차 일봉 0.798 vs 월봉 1.258). 관례를 고정하고 대안을 병기하는 쪽이 맞다.
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


# 회귀베타를 자본비용에 쓸 수 있는 최소 설명력. 실측 관측치: SK하이닉스 R²=0.561(사용 가능),
# 현대자동차 R²=0.3822(경계). 0.3 미만이면 "시장과 같이 움직인다" 는 전제가 성립하지 않아
# 그 베타로 만든 CAPM 은 근거가 없다 → 산업베타로 강제 전환한다.
R2_MIN = 0.3

# 표준 관측창 — 관례를 고정한다(R² 최대화로 고르지 않는다).
PRIMARY_WINDOW = ("month", 5)     # Damodaran
SECONDARY_WINDOW = ("week", 2)    # Bloomberg

# Blume 조정: 베타는 시간이 지나면 1 로 회귀하는 경향이 있다. 과거 회귀값을 그대로 미래
# 자본비용에 쓰면 극단값이 그대로 남는다. β_adj = 0.67 × β_raw + 0.33 × 1.0
BLUME_W_RAW, BLUME_W_MARKET = 2.0 / 3.0, 1.0 / 3.0


def blume_adjust(raw: float) -> float:
    return BLUME_W_RAW * float(raw) + BLUME_W_MARKET * 1.0


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float, float]:
    """(기울기, 절편, R², 기울기 t값). 외부 의존성 없이 계산.

    t값을 함께 내는 이유 — R² 는 "시장이 얼마나 설명하는가" 이고, t값은 "베타가 0 과
    구분되는가" 다. 관측치가 적으면 R² 가 그럴듯해도 베타 자체가 통계적으로 의미 없을 수 있다.
    """
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
    # 잔차 표준오차 → 기울기 표준오차 → t = slope / se
    # 잔차가 0(완전적합)이면 표준오차도 0 이라 t 는 무한대다. 예전에 이 경우를 0 으로
    # 돌려줬는데, 그러면 완전적합에 "|t|<2 — 베타가 0 과 구분되지 않음" 경고가 붙는다.
    sse = syy - slope * sxy
    if n <= 2:
        return slope, intercept, r2, 0.0
    if sse <= 0:
        return slope, intercept, r2, (float("inf") if slope else 0.0)
    se_slope = ((sse / (n - 2)) / sxx) ** 0.5
    tstat = (slope / se_slope) if se_slope else (float("inf") if slope else 0.0)
    return slope, intercept, r2, tstat


def regression_beta(company: str, period: str | None = None, years: int | None = None,
                    index: str = "KOSPI", market: str = "KR",
                    symbol: str | None = None, adjust: bool = True) -> Value:
    """레버드베타 — 주가·시장지수 시계열 OLS 회귀.

    market='KR' 이면 회사명을 DART 로 해석해 종목코드를 얻고 네이버(KRX) 시세를 쓴다.
    그 외 시장(US/JP/TW/HK)은 Yahoo 를 쓰며, `symbol` 로 티커를 직접 준다
    (예: AAPL, 7203.T, 2330.TW). symbol 을 생략하면 company 를 티커로 간주한다.
    """
    mkt = (market or "KR").strip().upper()
    period = period or PRIMARY_WINDOW[0]
    years = int(years or PRIMARY_WINDOW[1])

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
    raw, _, r2, tstat = _ols(xs, ys)
    beta = blume_adjust(raw) if adjust else raw

    warn = ""
    if r2 < R2_MIN:
        # "미확보" 가 아니다 — 회귀는 됐고 설명력이 기준 미달인 것이다. 예전 문구가
        # "회귀베타 미확보" 로 읽혀 '기능이 없다' 는 오해를 샀다.
        warn = (f" ⚠️ 회귀는 성공했으나 R² {r2:.3f} < {R2_MIN} 로 설명력이 기준 미달입니다 "
                f"(베타 산출 실패가 아님). 이 베타로 만든 CAPM 은 근거가 약하므로 "
                f"industry(Damodaran 산업명)를 함께 넘겨 산업베타로 전환하는 것을 권합니다.")
    if abs(tstat) < 2.0:
        warn += (f" ⚠️ 기울기 t값 {tstat:.2f} (|t|<2) — 베타가 0 과 통계적으로 구분되지 "
                 f"않습니다.")

    # 보조 관측창으로 안정성 확인. 실패해도 본 결과를 막지 않는다.
    alt = None
    if (period, years) == PRIMARY_WINDOW:
        try:
            a_stock = (naver.price_series(ticker, *SECONDARY_WINDOW) if mkt == "KR" and not symbol
                       else yahoo.price_series(ticker, *SECONDARY_WINDOW))
            a_mkt = (naver.index_series(index, *SECONDARY_WINDOW) if mkt == "KR" and not symbol
                     else yahoo.index_series(mkt, *SECONDARY_WINDOW))
            a_sr, a_mr = _returns(a_stock), _returns(a_mkt)
            a_dates = sorted(set(a_sr) & set(a_mr))
            if len(a_dates) >= 30:
                a_raw, _, a_r2, _ = _ols([a_mr[d] for d in a_dates], [a_sr[d] for d in a_dates])
                alt = {"beta": blume_adjust(a_raw) if adjust else a_raw, "raw": a_raw,
                       "r2": a_r2, "n": len(a_dates),
                       "window": f"{SECONDARY_WINDOW[0]}봉 {SECONDARY_WINDOW[1]}년"}
        except Exception:  # noqa: BLE001 — 보조 확인 실패가 본 결과를 막지는 않는다
            alt = None
    alt_note = ""
    if alt:
        alt_note = (f" 보조창({alt['window']}, n={alt['n']}): β {alt['beta']:.3f}, "
                    f"R² {alt['r2']:.3f}"
                    + (" — 두 창의 베타 차이가 커 안정성이 낮습니다."
                       if abs(alt["beta"] - beta) > 0.3 else " — 두 창이 정합적입니다."))

    adj_note = (f"raw β {raw:.4f} → Blume 조정 {beta:.4f} "
                f"(0.67×raw + 0.33×1.0; 베타는 장기적으로 1 로 회귀한다). " if adjust else
                f"raw β {raw:.4f} (Blume 조정 미적용). ")
    return Value(
        round(beta, 4), "배", label=f"{name} 레버드베타(회귀{'·Blume조정' if adjust else ''})",
        provenance=Provenance(
            source=f"계산엔진(engines.beta) · {src_name}",
            source_type=SourceType.COMPUTED,
            source_url=src_url,
            as_of=dates[-1],
            note=(f"{index_name} 대비 {period}봉 {years}년 OLS 회귀, 관측치 {len(dates)}개, "
                  f"R² {r2:.3f}, t {tstat:.2f}. {adj_note}"
                  f"β = Cov(주식,시장)/Var(시장). 기간 {dates[0]}~{dates[-1]}."
                  f"{alt_note}{warn}"),
        ),
        extras={
            "raw_beta": Value(
                round(raw, 4), "배", label=f"{name} raw 베타(조정 전)",
                provenance=Provenance(source="계산엔진(engines.beta)",
                                      source_type=SourceType.COMPUTED,
                                      source_url="(computed: OLS)",
                                      note="Blume 조정 전 회귀 기울기")),
            "r_squared": Value(
                round(r2, 4), "", label="회귀 결정계수 R²",
                provenance=Provenance(source="계산엔진(engines.beta)",
                                      source_type=SourceType.COMPUTED,
                                      source_url="(computed)",
                                      note="1에 가까울수록 시장이 주가변동을 잘 설명")),
            "t_stat": Value(
                round(tstat, 2), "", label="기울기 t값",
                provenance=Provenance(source="계산엔진(engines.beta)",
                                      source_type=SourceType.COMPUTED,
                                      source_url="(computed)",
                                      note="|t|<2 면 베타가 0 과 구분되지 않는다")),
            "observations": Value(
                len(dates), "개", label="관측치 수",
                provenance=Provenance(source="계산엔진(engines.beta)",
                                      source_type=SourceType.COMPUTED,
                                      source_url="(computed)")),
        },
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
             period: str | None = None, years: int | None = None, index: str = "KOSPI",
             market: str | None = None, symbol: str | None = None) -> Value:
    """최선의 경로로 베타를 구한다 — 상장사는 회귀, 실패하거나 비상장이면 산업베타.

    period·years 를 생략하면 PRIMARY_WINDOW(5년 월봉, Damodaran 관례)를 쓴다. 예전에는
    여기서 주봉 5년을 하드코딩해 넘겨 regression_beta 의 기본값이 무시되고 있었다.
    market 를 안 주면 country 를 시장으로 본다(KR→네이버/KOSPI, 그 외→Yahoo)."""
    mkt = (market or country or "KR").strip().upper()
    try:
        reg = regression_beta(company, period, years, index, mkt, symbol)
    except DataError as e:
        if industry is None:
            raise DataError(f"{e} (industry 를 함께 주면 산업베타로 대체 계산할 수 있습니다.)")
        v = industry_beta(industry, country)
        v.provenance.note = f"회귀베타 불가({e}) → 산업베타 사용. " + (v.provenance.note or "")
        return v

    # ── R² 게이팅 ────────────────────────────────────────────────────────
    # 설명력이 부족한 회귀베타는 "값이 나왔다" 는 것 말고는 근거가 없다. industry 가 있으면
    # 산업베타로 갈아타고, 없으면 값은 주되 경고를 최상위 note 로 올린다(조용히 쓰이지 않게).
    r2v = (reg.extras or {}).get("r_squared")
    r2 = r2v.value if r2v else None
    if r2 is not None and r2 < R2_MIN:
        if industry:
            v = industry_beta(industry, country)
            v.provenance.note = (
                f"⚠️ 회귀베타 R² {r2:.3f} < {R2_MIN} 로 설명력이 부족해 **산업베타로 전환**했습니다"
                f"(회귀베타 {reg.value} 는 참고용으로 extras 에 남깁니다). "
                + (v.provenance.note or ""))
            v.extras = dict(v.extras or {})
            v.extras["regression_beta_rejected"] = reg
            return v
        reg.provenance.note = (
            f"⚠️ [저신뢰] 회귀는 성공했으나 R² {r2:.3f} < {R2_MIN} 로 설명력이 기준 미달입니다 "
            f"(베타 '미확보'가 아니라 '신뢰도 미달'). 자본비용에 쓰려면 근거가 약하므로 "
            f"industry(Damodaran 산업명)를 함께 넘겨 산업베타로 전환하세요. "
            + (reg.provenance.note or ""))
    return reg

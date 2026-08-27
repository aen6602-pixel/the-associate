"""WACC 엔진 — provider Value 들을 조합해 결정론적으로 계산 (computed 등급).

Ke = Rf + β × ERP  (CAPM)
Kd(세후) = Kd × (1 − 세율)
WACC = (E/V) × Ke + (D/V) × Kd(세후)

Rf: ECOS(KR)/FRED(US) [authoritative], ERP·세율: Damodaran [reference].
β·Kd·자본구조는 사용자가 제시하는 가정(assumption) — 엔진이 지어내지 않는다.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from providers import damodaran, dart, ecos, fred, naver


def _rf(country: str, tenor: str) -> Value:
    c = country.strip().upper()
    if c == "KR":
        return ecos.risk_free_rate(tenor)
    if c == "US":
        return fred.risk_free_rate(tenor)
    raise DataError(f"무위험수익률 미지원 국가: {country} (현재 KR, US). 다른 국가는 추가 예정.")


def compute_wacc(country: str, beta: float, cost_of_debt_pct: float,
                 debt_to_value: float, tenor: str = "10Y",
                 risk_free_pct: float | None = None) -> Value:
    """WACC 계산. beta/cost_of_debt_pct/debt_to_value 는 사용자가 제시하는 가정.

    risk_free_pct 를 주면 국채 조회를 건너뛴다 — Rf provider 가 없는 국가(일본·대만 등)에서
    해당 통화 국채수익률을 직접 넣기 위한 통로다."""
    beta = float(beta)
    kd = float(cost_of_debt_pct)
    dv = float(debt_to_value)
    if not 0 <= dv <= 1:
        raise DataError("debt_to_value 는 0~1 사이여야 함 (예: 부채비중 30% → 0.3)")

    if risk_free_pct is not None:
        rf = Value(float(risk_free_pct), "%", label=f"무위험수익률 ({country.upper()}, 직접 지정)",
                   provenance=Provenance(source="사용자 가정(입력)",
                                         source_type=SourceType.ASSUMPTION,
                                         source_url="(assumption)",
                                         note="Rf provider 가 없는 국가라 직접 지정된 값"))
    else:
        rf = _rf(country, tenor)
    erp = damodaran.equity_risk_premium(country)
    tax = damodaran.corporate_tax_rate(country)

    ke = rf.value + beta * erp.value
    kd_after = kd * (1 - tax.value / 100)
    wacc = (1 - dv) * ke + dv * kd_after

    rf_src = rf.provenance.source + (f", {rf.provenance.as_of}" if rf.provenance.as_of else "")
    note = (
        f"Ke {ke:.2f}% = Rf {rf.value}%({rf_src}) "
        f"+ β {beta} × ERP {erp.value}%(Damodaran); "
        f"Kd(세후) {kd_after:.2f}% = {kd}% × (1 − 세율 {tax.value}%, Damodaran); "
        f"WACC = {(1-dv)*100:.0f}%×Ke + {dv*100:.0f}%×Kd(세후)."
    )
    return Value(
        value=round(wacc, 2), unit="%", label=f"WACC ({country.upper()})",
        provenance=Provenance(
            source="계산엔진(engines.wacc)",
            source_type=SourceType.COMPUTED,
            source_url="(computed from Rf+ERP+β+tax+구조)",
            note=note,
        ),
    )


# ── 자동 WACC ─────────────────────────────────────────────────────
def market_debt_to_value(company: str, year: int | None = None,
                         include_lease: bool = True) -> Value:
    """시장가치 기준 목표 부채비중 D/(D+E) = IBD / (IBD + 시가총액).

    자본은 장부가가 아니라 **시가총액**을 쓴다(WACC 의 자본구조는 시장가치 기준).
    비상장사는 시가총액이 없으므로 산업평균(damodaran.industry_metrics)을 써야 한다."""
    ent = dart.resolve(company)
    code = ent.get("stock_code")
    if not code:
        raise DataError(
            f"{ent['corp_name']} 는 비상장이라 시가총액이 없습니다 → 목표 부채비중은 "
            f"산업평균(get_industry_benchmarks 의 debt_to_value)을 쓰세요.")

    db = dart.debt_balances(company, year)
    lease = db["lease"].value if include_lease else 0
    ibd = db["short_term"].value + db["long_term"].value + lease
    mcap = naver.market_cap(code, ent["corp_name"])
    v = ibd + mcap.value
    if v <= 0:
        raise DataError(f"{ent['corp_name']} 의 IBD+시가총액이 0 이하입니다.")

    dv = ibd / v
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    return Value(
        round(dv, 4), "비율",
        label=f"{ent['corp_name']} 현재 레버리지 D/(D+E) (spot, 시장가치)",
        provenance=Provenance(
            source="계산엔진(engines.wacc)", source_type=SourceType.COMPUTED,
            source_url="(computed: DART 차입금 + 네이버 시가총액)",
            as_of=mcap.provenance.as_of,
            note=(f"IBD {f(ibd)} ÷ (IBD + 시가총액 {f(mcap.value)}) = {dv * 100:.2f}%. "
                  f"자본은 장부가가 아닌 시가총액 기준"
                  + (" (리스부채 포함)" if include_lease and lease else "")
                  + ". ⚠️ 이것은 **평가시점의 순간(spot) 레버리지**이지 목표자본구조가 아니다 — "
                    "주가가 급등한 시점에는 자기자본 비중이 과대해져(SK하이닉스 실측 D/V "
                    "1.88%) WACC 이 구조적으로 높게 나온다. WACC 의 target 으로는 산업 "
                    "median(get_industry_benchmarks) 을 기본으로 쓰고, spot 은 교차검증에 쓴다."),
        ),
        extras={"market_cap": mcap},
    )


def compute_wacc_auto(company: str, country: str = "KR", industry: str | None = None,
                      tenor: str = "10Y", beta_override: float | None = None,
                      cost_of_debt_pct: float | None = None,
                      debt_to_value: float | None = None,
                      debt_ratio_source: str = "auto",
                      market: str | None = None, symbol: str | None = None,
                      risk_free_pct: float | None = None) -> Value:
    """WACC 를 공시·시세에서 **자동으로** 구성한다.

    한국(market='KR')과 해외는 쓸 수 있는 소스가 다르다:
      · β  — KR: 네이버(KRX) 5년 주봉 회귀 / 해외: Yahoo + 해당시장 지수(symbol 필요)
             비상장이거나 회귀 불가면 industry 로 Damodaran 산업베타 재레버리지
      · Kd — KR: 현금흐름표 이자비용 ÷ 이자발생부채(DART)
             해외: DART 가 없으므로 **Damodaran 산업평균 Kd**(industry 필요)
      · D/(D+E) — KR: IBD ÷ (IBD + 시가총액) / 해외: 산업평균(시가총액 소스 없음)
      · Rf·ERP·세율 — ECOS(KR)/FRED(US) + Damodaran. Rf provider 가 없는 국가(JP·TW 등)는
        risk_free_pct 로 직접 넣는다.

    각 인자를 직접 주면 그 값이 우선한다. 어느 경로를 썼는지는 note 에 전부 남는다."""
    from engines import beta as beta_engine, dcf_inputs

    c = (country or "KR").strip().upper()
    mkt = (market or c).strip().upper()
    domestic = mkt == "KR"
    region = damodaran.region_for(c)
    steps: list[str] = []

    def _industry_or_fail(what: str) -> dict:
        if not industry:
            raise DataError(
                f"{company}({mkt}) 의 {what} 는 공시에서 자동으로 뽑을 수 없습니다 — DART 공시는 "
                f"한국 기업 전용입니다. Damodaran 산업평균을 쓰려면 industry 를 지정하세요"
                f"(예: Apparel, Semiconductor). 또는 값을 직접 넘기세요.")
        return damodaran.industry_wacc(industry, region)

    # 1) 베타
    if beta_override is not None:
        beta_v, beta = None, float(beta_override)
        steps.append(f"β {beta} (직접 지정)")
    else:
        beta_v = beta_engine.beta_for(company, industry=industry, country=c,
                                      market=mkt, symbol=symbol)
        beta = beta_v.value
        steps.append(f"β {beta} ({beta_v.provenance.source})")

    # 2) 세전 타인자본비용
    if cost_of_debt_pct is not None:
        kd_v, kd = None, float(cost_of_debt_pct)
        steps.append(f"Kd {kd}% (직접 지정)")
    elif domestic:
        # P1-2: 기본을 **시장 Kd**(ECOS 등급별 회사채 유통수익률)로 바꿨다. 실효 Kd 는 과거
        # 조달금리의 가중평균이라 Rf 보다 낮아지는 역전이 나온다(SK하이닉스 실측 3.79% <
        # Rf 4.288% → 신용스프레드가 음수). 시장 Kd 가 실패하면 실효 Kd 로 폴백한다.
        try:
            kd_v = dcf_inputs.market_cost_of_debt(company, country=c)
            kd = kd_v.value
            steps.append(f"Kd {kd}% (ECOS 등급별 회사채 = 시장 신규조달금리)")
        except DataError:
            kd_v = dcf_inputs.cost_of_debt(company)
            kd = kd_v.value
            steps.append(f"Kd {kd}% (실효 이자비용÷IBD — 시장 Kd 조회 실패)")
        if not 0.3 <= kd <= 15:
            raise DataError(
                f"Kd {kd}% 가 통상 범위(0.3~15%)를 벗어납니다 "
                f"({kd_v.provenance.note}). cost_of_debt_pct 를 직접 지정하거나 산업평균을 쓰세요.")
    else:
        kd_v = _industry_or_fail("타인자본비용")["cost_of_debt"]
        kd = kd_v.value
        steps.append(f"Kd {kd}% (Damodaran {region} 산업평균)")

    # 3) 목표 부채비중
    if debt_to_value is not None:
        dv_v, dv = None, float(debt_to_value)
        steps.append(f"D/(D+E) {dv:.4f} (직접 지정)")
    elif debt_ratio_source in ("industry", "auto") or not domestic:
        # P1-1: 기본값을 산업 median 으로 바꿨다. 예전 기본(auto=국내는 시장가치 우선)은
        # 순간 레버리지를 'target' 이라 부르는 것이어서 IB 리뷰에서 반드시 지적된다.
        # spot 을 쓰려면 debt_ratio_source='spot' 을 명시해야 한다.
        try:
            dv_v = _industry_or_fail("목표 부채비중")["debt_to_value"]
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} ({region} 산업 median, target)")
        except DataError:
            if not domestic:
                raise
            dv_v = market_debt_to_value(company)
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} (industry 미지정 → spot 시장가치로 대체)")
    else:   # debt_ratio_source == "spot"
        try:
            dv_v = market_debt_to_value(company)
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} (spot 시장가치 — target 아님)")
        except DataError as e:
            if not industry:
                raise DataError(f"{e}") from e
            dv_v = damodaran.industry_wacc(industry, region)["debt_to_value"]
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} (시가총액 없음 → 산업평균)")

    base = compute_wacc(c, beta, kd, dv, tenor, risk_free_pct)

    # 금융부문 보유사는 D/V·Kd 자체가 오염된다 → 경고를 WACC note 에 실어 보낸다.
    mix_note = ""
    if domestic:
        try:
            from engines import business_mix

            mix = business_mix.classify(company, None, "CFS", deep=False)
            if not mix["single_dcf_ok"]:
                mix_note = (f" ⚠️ [금융부문 오염] {mix['reason']} 연결 IBD 로 만든 부채비중과 "
                            f"이자비용으로 만든 Kd 는 제조부문 자본비용이 아닙니다.")
        except Exception:  # noqa: BLE001
            pass

    extras = {"beta": beta_v, "cost_of_debt": kd_v, "debt_to_value": dv_v}
    return Value(
        base.value, "%", label=f"{company} WACC (자동)",
        provenance=Provenance(
            source="계산엔진(engines.wacc · 자동 도출)", source_type=SourceType.COMPUTED,
            source_url="(computed: 공시·시세에서 β·Kd·자본구조 자동 도출)",
            as_of=base.provenance.as_of,
            note=("입력: " + " / ".join(steps) + ". " + (base.provenance.note or "")
                  + mix_note
                  + " [정합성] WACC 의 D/(D+E) 와 DCF 의 순부채 차감은 같은 부채 정의"
                    "(이자발생부채 + 리스부채)를 써야 합니다 — get_net_debt 결과와 위 IBD 가 "
                    "다르면 그 차이를 먼저 설명해야 합니다."),
        ),
        extras={k: v for k, v in extras.items() if v is not None},
    )

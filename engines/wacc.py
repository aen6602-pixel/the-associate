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
                 debt_to_value: float, tenor: str = "10Y") -> Value:
    """WACC 계산. beta/cost_of_debt_pct/debt_to_value 는 사용자가 제시하는 가정."""
    beta = float(beta)
    kd = float(cost_of_debt_pct)
    dv = float(debt_to_value)
    if not 0 <= dv <= 1:
        raise DataError("debt_to_value 는 0~1 사이여야 함 (예: 부채비중 30% → 0.3)")

    rf = _rf(country, tenor)
    erp = damodaran.equity_risk_premium(country)
    tax = damodaran.corporate_tax_rate(country)

    ke = rf.value + beta * erp.value
    kd_after = kd * (1 - tax.value / 100)
    wacc = (1 - dv) * ke + dv * kd_after

    note = (
        f"Ke {ke:.2f}% = Rf {rf.value}%({rf.provenance.source}, {rf.provenance.as_of}) "
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
        round(dv, 4), "비율", label=f"{ent['corp_name']} 목표부채비중 D/(D+E) (시장가치)",
        provenance=Provenance(
            source="계산엔진(engines.wacc)", source_type=SourceType.COMPUTED,
            source_url="(computed: DART 차입금 + 네이버 시가총액)",
            as_of=mcap.provenance.as_of,
            note=(f"IBD {f(ibd)} ÷ (IBD + 시가총액 {f(mcap.value)}) = {dv * 100:.2f}%. "
                  f"자본은 장부가가 아닌 시가총액 기준"
                  + (" (리스부채 포함)" if include_lease and lease else "") + "."),
        ),
        extras={"market_cap": mcap},
    )


def compute_wacc_auto(company: str, country: str = "KR", industry: str | None = None,
                      tenor: str = "10Y", beta_override: float | None = None,
                      cost_of_debt_pct: float | None = None,
                      debt_to_value: float | None = None,
                      debt_ratio_source: str = "auto") -> Value:
    """WACC 를 공시·시세에서 **자동으로** 구성한다.

    · β  : 상장사는 5년 주봉 회귀베타(네이버 시세), 비상장이면 industry 인자로 산업베타 재레버리지
    · Kd : 현금흐름표 이자비용 ÷ 이자발생부채 (손익 '금융비용' 은 환차손 포함이라 쓰지 않음)
    · D/(D+E) : IBD ÷ (IBD + 시가총액). 비상장이거나 debt_ratio_source='industry' 면 산업평균
    · Rf·ERP·세율 : ECOS/FRED + Damodaran

    각 인자를 직접 주면 그 값이 우선한다(자동 도출을 건너뛴다). 어느 경로를 썼는지는 note 에
    전부 남는다 — 자동이라도 추적 가능해야 하므로."""
    from engines import beta as beta_engine, dcf_inputs

    c = (country or "KR").strip().upper()
    steps: list[str] = []

    # 1) 베타
    if beta_override is not None:
        beta_v, beta = None, float(beta_override)
        steps.append(f"β {beta} (직접 지정)")
    else:
        beta_v = beta_engine.beta_for(company, industry=industry, country=c)
        beta = beta_v.value
        steps.append(f"β {beta} ({beta_v.provenance.source})")

    # 2) 세전 타인자본비용
    if cost_of_debt_pct is not None:
        kd_v, kd = None, float(cost_of_debt_pct)
        steps.append(f"Kd {kd}% (직접 지정)")
    else:
        kd_v = dcf_inputs.cost_of_debt(company)
        kd = kd_v.value
        if not 0.3 <= kd <= 15:
            raise DataError(
                f"공시에서 계산한 Kd {kd}% 가 통상 범위(0.3~15%)를 벗어납니다 "
                f"({kd_v.provenance.note}). cost_of_debt_pct 를 직접 지정하거나 산업평균을 쓰세요.")
        steps.append(f"Kd {kd}% (공시 이자비용÷IBD)")

    # 3) 목표 부채비중
    if debt_to_value is not None:
        dv_v, dv = None, float(debt_to_value)
        steps.append(f"D/(D+E) {dv:.4f} (직접 지정)")
    elif debt_ratio_source == "industry":
        if not industry:
            raise DataError("debt_ratio_source='industry' 면 industry 를 지정해야 합니다.")
        dv_v = damodaran.industry_metrics(industry, damodaran.region_for(c))["debt_to_value"]
        dv = dv_v.value
        steps.append(f"D/(D+E) {dv:.4f} (산업평균)")
    else:
        try:
            dv_v = market_debt_to_value(company)
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} (시장가치)")
        except DataError as e:
            if not industry:
                raise DataError(f"{e}") from e
            dv_v = damodaran.industry_metrics(industry, damodaran.region_for(c))["debt_to_value"]
            dv = dv_v.value
            steps.append(f"D/(D+E) {dv:.4f} (시가총액 없음 → 산업평균)")

    base = compute_wacc(c, beta, kd, dv, tenor)
    extras = {"beta": beta_v, "cost_of_debt": kd_v, "debt_to_value": dv_v}
    return Value(
        base.value, "%", label=f"{company} WACC (자동)",
        provenance=Provenance(
            source="계산엔진(engines.wacc · 자동 도출)", source_type=SourceType.COMPUTED,
            source_url="(computed: 공시·시세에서 β·Kd·자본구조 자동 도출)",
            as_of=base.provenance.as_of,
            note="입력: " + " / ".join(steps) + ". " + (base.provenance.note or ""),
        ),
        extras={k: v for k, v in extras.items() if v is not None},
    )

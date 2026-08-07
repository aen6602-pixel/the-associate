"""WACC 엔진 — provider Value 들을 조합해 결정론적으로 계산 (computed 등급).

Ke = Rf + β × ERP  (CAPM)
Kd(세후) = Kd × (1 − 세율)
WACC = (E/V) × Ke + (D/V) × Kd(세후)

Rf: ECOS(KR)/FRED(US) [authoritative], ERP·세율: Damodaran [reference].
β·Kd·자본구조는 사용자가 제시하는 가정(assumption) — 엔진이 지어내지 않는다.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from providers import damodaran, ecos, fred


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

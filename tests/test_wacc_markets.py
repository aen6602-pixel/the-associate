"""WACC 자동 산출의 시장 분기 — 해외 기업이 한국(DART) 경로를 타지 않도록 고정한다.

실측 사고: compute_wacc_auto 가 시장 구분 없이 무조건 DART 를 타서, 미국 의류업체
(Hanesbrands·V.F.·Gildan)와 일본 기업 조회가 전부 "DART 에서 기업을 못 찾음" 으로 실패했다.
"""
from __future__ import annotations

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import wacc as wacc_engine


def _val(v, unit="%", label="x", src="테스트"):
    return Value(v, unit, label=label,
                 provenance=Provenance(source=src, source_type=SourceType.REFERENCE,
                                       source_url="(test)"))


@pytest.fixture
def market_inputs(monkeypatch):
    """Rf·ERP·세율만 고정하고, 나머지 경로는 실제 분기 로직이 돌게 둔다."""
    from providers import damodaran, ecos, fred

    monkeypatch.setattr(ecos, "risk_free_rate", lambda tenor="10Y": _val(4.0, src="ECOS"))
    monkeypatch.setattr(fred, "risk_free_rate", lambda tenor="10Y": _val(4.7, src="FRED"))
    monkeypatch.setattr(damodaran, "equity_risk_premium", lambda c="KR": _val(5.0))
    monkeypatch.setattr(damodaran, "corporate_tax_rate", lambda c="KR": _val(25.0))
    monkeypatch.setattr(damodaran, "industry_wacc", lambda ind, region: {
        "cost_of_debt": _val(5.29),
        "debt_to_value": _val(0.2383, "비율"),
        "industry_name": _val(0, "", label=ind),
    })


def test_overseas_does_not_touch_dart(monkeypatch, market_inputs):
    """해외 기업이면 Kd·부채비중을 DART 가 아니라 Damodaran 산업평균에서 가져와야 한다."""
    from engines import beta as beta_engine, dcf_inputs

    monkeypatch.setattr(beta_engine, "beta_for",
                        lambda *a, **k: _val(1.2, "배", src="Yahoo Finance"))
    monkeypatch.setattr(dcf_inputs, "cost_of_debt", lambda *a, **k: pytest.fail(
        "해외 기업인데 DART 기반 cost_of_debt 를 호출했다"))
    monkeypatch.setattr(wacc_engine, "market_debt_to_value", lambda *a, **k: pytest.fail(
        "해외 기업인데 네이버 시가총액 경로를 호출했다"))

    v = wacc_engine.compute_wacc_auto("Hanesbrands", country="US", industry="Apparel",
                                      market="US", symbol="HBI")
    assert v.value > 0
    assert "산업평균" in v.provenance.note


def test_overseas_without_industry_gives_actionable_error(monkeypatch, market_inputs):
    from engines import beta as beta_engine

    monkeypatch.setattr(beta_engine, "beta_for", lambda *a, **k: _val(1.2, "배"))
    with pytest.raises(DataError) as e:
        wacc_engine.compute_wacc_auto("Hanesbrands", country="US", market="US", symbol="HBI")
    msg = str(e.value)
    assert "한국 기업 전용" in msg and "industry" in msg


def test_domestic_uses_market_kd_not_effective_kd(monkeypatch, market_inputs):
    """국내 기본 Kd 는 **시장**(ECOS 등급별 회사채)이다.

    실효 Kd(이자비용/차입금)는 과거 조달금리의 가중평균이라 저금리 조달분이 남아 있으면
    무위험수익률보다 낮아진다(SK하이닉스 실측 3.79% < Rf 4.288% -> 신용스프레드 음수).
    """
    from engines import beta as beta_engine, dcf_inputs

    used = []
    monkeypatch.setattr(beta_engine, "beta_for",
                        lambda *a, **k: _val(1.18, "배", src="네이버 금융(KRX 시세)"))
    monkeypatch.setattr(dcf_inputs, "market_cost_of_debt",
                        lambda *a, **k: (used.append("market_kd"), _val(4.5))[1])
    monkeypatch.setattr(dcf_inputs, "cost_of_debt", lambda *a, **k: pytest.fail(
        "시장 Kd 가 되는데 실효 Kd 를 기본으로 쓰면 안 된다"))
    monkeypatch.setattr(wacc_engine, "market_debt_to_value", lambda *a, **k: pytest.fail(
        "기본 경로에서 spot 레버리지를 target 으로 쓰면 안 된다"))

    v = wacc_engine.compute_wacc_auto("삼성전자", industry="Semiconductor")
    assert used == ["market_kd"]
    assert "ECOS 등급별 회사채" in v.provenance.note
    assert "산업 median" in v.provenance.note, "target 자본구조는 산업 median 이 기본"


def test_domestic_falls_back_to_effective_kd_when_market_kd_fails(
        monkeypatch, market_inputs):
    """시장 Kd 조회가 실패하면 실효 Kd 로 폴백하되 그 사실을 note 에 남긴다."""
    from engines import beta as beta_engine, dcf_inputs

    def _boom(*a, **k):
        raise DataError("ECOS 장애")

    monkeypatch.setattr(beta_engine, "beta_for", lambda *a, **k: _val(1.18, "배"))
    monkeypatch.setattr(dcf_inputs, "market_cost_of_debt", _boom)
    monkeypatch.setattr(dcf_inputs, "cost_of_debt", lambda *a, **k: _val(1.95))

    v = wacc_engine.compute_wacc_auto("삼성전자", industry="Semiconductor")
    assert "실효" in v.provenance.note and "시장 Kd 조회 실패" in v.provenance.note


def test_spot_leverage_is_opt_in_and_labeled_as_such(monkeypatch, market_inputs):
    """spot 을 쓰려면 명시해야 하고, target 이 아니라고 표기돼야 한다."""
    from engines import beta as beta_engine, dcf_inputs

    monkeypatch.setattr(beta_engine, "beta_for", lambda *a, **k: _val(1.18, "배"))
    monkeypatch.setattr(dcf_inputs, "market_cost_of_debt", lambda *a, **k: _val(4.5))
    monkeypatch.setattr(wacc_engine, "market_debt_to_value",
                        lambda *a, **k: _val(0.0188, "비율"))

    v = wacc_engine.compute_wacc_auto("삼성전자", debt_ratio_source="spot")
    assert "spot" in v.provenance.note and "target 아님" in v.provenance.note


def test_market_defaults_to_country(monkeypatch, market_inputs):
    """market 을 생략하면 country 를 따라간다 — country=US 면 해외 경로."""
    from engines import beta as beta_engine, dcf_inputs

    monkeypatch.setattr(beta_engine, "beta_for", lambda *a, **k: _val(1.0, "배"))
    monkeypatch.setattr(dcf_inputs, "cost_of_debt", lambda *a, **k: pytest.fail(
        "country=US 인데 국내 경로를 탔다"))
    wacc_engine.compute_wacc_auto("Nike", country="US", industry="Apparel", symbol="NKE")


def test_risk_free_override_for_countries_without_provider(market_inputs):
    """일본·대만은 Rf provider 가 없다 — 직접 지정하면 국채 조회를 건너뛴다."""
    v = wacc_engine.compute_wacc("JP", beta=0.88, cost_of_debt_pct=6.0,
                                 debt_to_value=0.32, risk_free_pct=1.9)
    assert "Rf 1.9%" in v.provenance.note
    assert "None" not in v.provenance.note, "as_of 가 없을 때 'None' 이 노출되면 안 된다"


def test_unsupported_country_without_override_is_rejected(market_inputs):
    with pytest.raises(DataError, match="무위험수익률 미지원"):
        wacc_engine.compute_wacc("JP", beta=0.88, cost_of_debt_pct=6.0, debt_to_value=0.32)

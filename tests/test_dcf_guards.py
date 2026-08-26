"""DCF 결과 검증 가드 + LLM 이 넘기는 선택 인자 정규화.

두 사고를 막는다:
  1. 전 연도 UFCF 가 음수인데(EV 음수) 아무 경고 없이 '주당가치' 로 제시되는 것
     — 실측: SK하이닉스에 5개년 평균 CAPEX/매출 31%를 넣으면 EV −66조가 나온다.
  2. LLM 이 선택 인자를 생략하지 않고 0/""을 넘겨 β=0 인 WACC(=Rf)가 조용히 계산되는 것
     — 실측: gpt-5.6-terra 가 beta_override=0, year=0, industry="" 를 넘겼다.
"""
from __future__ import annotations

import pytest

from agent import registry
from core.schema import DataError, Provenance, SourceType, Value
from engines import dcf as dcf_engine


@pytest.fixture
def stub_dcf_base(monkeypatch):
    """DART/Damodaran 을 타지 않고 build_model 을 돌리기 위한 최소 스텁."""
    from providers import dart, damodaran

    monkeypatch.setattr(dart, "financial_item", lambda *a, **k: Value(
        1000.0, "KRW", label="테스트 매출액",
        provenance=Provenance(source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                              source_url="https://dart.fss.or.kr", as_of="FY2025")))
    monkeypatch.setattr(dart, "shares_outstanding", lambda *a, **k: Value(
        100, "주", label="테스트 발행주식수",
        provenance=Provenance(source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                              source_url="https://dart.fss.or.kr", as_of="FY2025")))
    monkeypatch.setattr(damodaran, "corporate_tax_rate", lambda *a, **k: Value(
        25.0, "%", label="세율",
        provenance=Provenance(source="Damodaran", source_type=SourceType.REFERENCE,
                              source_url="https://pages.stern.nyu.edu")))


def _model(**over):
    args = dict(company="테스트", wacc_pct=10.0, net_debt=0.0, revenue_growth=5.0,
                ebit_margin_pct=15.0, da_pct=5.0, capex_pct=5.0, nwc_pct=10.0,
                terminal_growth_pct=2.0, forecast_years=5)
    args.update(over)
    return dcf_engine.build_model(**args)


def test_healthy_model_has_no_warnings(stub_dcf_base):
    m = _model()
    assert m["warnings"] == []
    assert m["valuation_reliable"] is True
    assert m["ev"] > 0 and m["per_share"] > 0


def test_all_negative_ufcf_is_flagged(stub_dcf_base):
    """CAPEX 가 세후영업이익+D&A 를 크게 넘으면 전 연도 UFCF 음수 → 경고."""
    m = _model(capex_pct=40.0, ebit_margin_pct=10.0, da_pct=5.0)
    assert m["valuation_reliable"] is False
    assert any("전부 UFCF 가 음수" in w for w in m["warnings"])
    assert any("EV 가 음수" in w for w in m["warnings"])


def test_negative_equity_is_flagged(stub_dcf_base):
    """EV 는 양수인데 순부채가 커서 지분가치가 음수인 경우."""
    m = _model(net_debt=10_000_000.0)
    assert any("지분가치가 음수" in w for w in m["warnings"])


def test_terminal_value_dominance_is_flagged(stub_dcf_base):
    """WACC 가 g 에 근접하면 TV 가 EV 를 지배 → 결과가 g 에 좌우된다는 사실을 알려야 한다."""
    m = _model(wacc_pct=6.0, terminal_growth_pct=5.5)
    assert any("Terminal Value" in w for w in m["warnings"])


def test_warnings_surface_in_the_value_note(stub_dcf_base):
    v = dcf_engine.evaluate("테스트", 10.0, 0.0, 5.0, 10.0, 5.0, 40.0, 10.0, 2.0)
    assert "검증 경고" in v.provenance.note


def test_wacc_still_guards_against_g_above_wacc(stub_dcf_base):
    with pytest.raises(DataError, match="terminal growth"):
        _model(wacc_pct=3.0, terminal_growth_pct=5.0)


# ── 선택 인자 정규화 ──────────────────────────────────────────────
@pytest.mark.parametrize("raw", [None, "", "   ", 0, 0.0, -1])
def test_pos_treats_blank_and_nonpositive_as_missing(raw):
    assert registry._pos(raw) is None


@pytest.mark.parametrize("raw, expected", [(5, 5), (2024, 2024), (1.65, 1.65), ("3", "3")])
def test_pos_keeps_real_values(raw, expected):
    assert registry._pos(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_blank_normalises_empty_strings(raw):
    assert registry._blank(raw) is None


def test_blank_keeps_real_strings():
    assert registry._blank("Semiconductor") == "Semiconductor"


def test_wacc_auto_ignores_zero_beta_override(monkeypatch):
    """β=0 을 그대로 쓰면 WACC 가 Rf 와 같아져 조용히 틀린다 → 자동 도출로 넘어가야 한다."""
    seen = {}

    def fake(company, country, industry, tenor, beta_override, kd, dv, src,
             market=None, symbol=None, risk_free_pct=None):
        seen.update(beta_override=beta_override, industry=industry, kd=kd, dv=dv, src=src,
                    market=market, symbol=symbol, risk_free_pct=risk_free_pct)
        return Value(9.0, "%", label="wacc",
                     provenance=Provenance(source="계산엔진", source_type=SourceType.COMPUTED,
                                           source_url="(computed)"))

    from engines import wacc as wacc_engine

    monkeypatch.setattr(wacc_engine, "compute_wacc_auto", fake)
    registry._wacc_auto("삼성전자", "KR", industry="", beta_override=0,
                        cost_of_debt_pct=0, debt_to_value=0, debt_ratio_source="",
                        market="", symbol="", risk_free_pct=0)
    assert seen["beta_override"] is None
    assert seen["industry"] is None
    assert seen["kd"] is None and seen["dv"] is None
    assert seen["src"] == "auto"
    # 시장·티커·Rf 도 같은 정규화를 받아야 한다 — 빈 문자열이 그대로 가면 라우팅이 깨진다.
    assert seen["market"] is None and seen["symbol"] is None
    assert seen["risk_free_pct"] is None


def test_year_zero_is_dropped_before_hitting_dart(monkeypatch):
    seen = {}

    from engines import dcf_inputs

    def fake(company, year, include_lease):
        seen["year"] = year
        return Value(1, "KRW", label="nd",
                     provenance=Provenance(source="계산엔진", source_type=SourceType.COMPUTED,
                                           source_url="(computed)"))

    monkeypatch.setattr(dcf_inputs, "net_debt", fake)
    registry._net_debt("삼성전자", year=0)
    assert seen["year"] is None

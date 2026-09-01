"""베타 경로 선택(회귀/산업/Peer)과 WACC 산출 내역 공개.

WACC 은 DCF 를 가장 크게 흔드는 숫자인데, 예전에는 계산에 쓴 Rf·ERP·세율·Ke 가 note 문장
안에만 있었다. 화면에 근거표를 그리려면 두뇌가 문장을 다시 타이핑해야 했고, 그 과정이
숫자가 어긋나는 통로가 된다. 이제 항목마다 **자기 출처를 단 Value** 로 나온다.

Peer 베타는 레버드베타를 그냥 평균내지 않는다 — 자본구조가 제각각인 βL 의 평균은 어느
회사의 자본구조도 아니다. 각자 무차입화(Hamada) → 중앙값 → 대상 자본구조로 재레버리지.
"""
from __future__ import annotations

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import beta as beta_engine, wacc as wacc_engine


def _v(x, unit="배", src="테스트"):
    return Value(x, unit, label=src,
                 provenance=Provenance(source=src, source_type=SourceType.COMPUTED,
                                       source_url="(test)"))


def _reg(b: float, r2: float) -> Value:
    v = _v(b)
    v.extras = {"r_squared": _v(r2, "")}
    return v


@pytest.fixture
def peers(monkeypatch):
    """3개 Peer: βL 과 D/E 가 서로 다르다 → 무차입화 없이는 섞을 수 없다."""
    table = {"가전자": (1.60, 0.60, 0.50), "나반도체": (1.20, 0.55, 0.20),
             "다소재": (0.90, 0.45, 0.10), "저신뢰사": (2.50, 0.10, 0.30),
             # 대상회사 — Peer 들보다 레버리지가 훨씬 높다(재레버리지 대상 확인용)
             "대상사": (1.00, 0.90, 0.80)}

    def fake_reg(company, period=None, years=None, index="KOSPI", market="KR",
                 symbol=None, adjust=True):
        if company not in table:
            raise DataError(f"{company}: 시세 없음")
        b, r2, _de = table[company]
        return _reg(b, r2)

    def fake_dv(company, year=None, include_lease=True):
        if company not in table:
            raise DataError(f"{company}: 시가총액 없음")
        de = table[company][2]
        return _v(de / (1 + de), "")        # D/E → D/(D+E)

    monkeypatch.setattr(beta_engine, "regression_beta", fake_reg)
    monkeypatch.setattr(wacc_engine, "market_debt_to_value", fake_dv)
    monkeypatch.setattr(beta_engine.damodaran, "corporate_tax_rate",
                        lambda c: _v(25.0, "%", "Damodaran"))
    return table


# ── Peer bottom-up 베타 ───────────────────────────────────────────
def test_peer_beta_unlevers_each_peer_before_averaging(peers):
    v = beta_engine.peer_beta(["가전자", "나반도체", "다소재"], target_de_ratio=0.30)

    bus = sorted(beta_engine.unlever(b, de, 25.0) for b, _r2, de in
                 (peers[n] for n in ("가전자", "나반도체", "다소재")))
    expected = beta_engine.relever(bus[1], 0.30, 25.0)      # 중앙값 → 재레버리지
    assert v.value == pytest.approx(round(expected, 4))
    assert v.value != pytest.approx(sum(peers[n][0] for n in
                                        ("가전자", "나반도체", "다소재")) / 3), \
        "레버드베타 단순평균과 같으면 무차입화를 안 한 것이다"


def test_low_r2_peers_are_dropped_and_said_so(peers):
    v = beta_engine.peer_beta(["가전자", "나반도체", "다소재", "저신뢰사"],
                              target_de_ratio=0.30)
    assert v.extras["peers_used"].value == 3
    assert "저신뢰사" in v.provenance.note and "R²" in v.provenance.note


def test_unknown_peer_is_dropped_with_a_reason(peers):
    v = beta_engine.peer_beta(["가전자", "나반도체", "다소재", "없는회사"],
                              target_de_ratio=0.30)
    assert v.extras["peers_used"].value == 3
    assert "없는회사" in v.provenance.note


def test_too_few_usable_peers_refuses_rather_than_guessing(peers):
    with pytest.raises(DataError) as e:
        beta_engine.peer_beta(["가전자", "없는회사1", "없는회사2"], target_de_ratio=0.30)
    assert "없는회사1" in str(e.value), "왜 버렸는지 말해야 고칠 수 있다"


def test_two_peers_is_not_an_average():
    with pytest.raises(DataError):
        beta_engine.peer_beta(["가전자", "나반도체"])


def test_thin_sample_is_flagged(peers):
    v = beta_engine.peer_beta(["가전자", "나반도체", "다소재"], target_de_ratio=0.30)
    assert "⚠️" in v.provenance.note and "얇" in v.provenance.note


def test_target_company_leverage_is_used_when_given(peers):
    """대상회사 자본구조로 재레버리지해야 한다 — Peer 중앙값 D/E 가 아니라."""
    v = beta_engine.peer_beta(["가전자", "나반도체", "다소재"], target_company="대상사")
    assert v.extras["target_de_ratio"].value == pytest.approx(0.80, abs=1e-3)


# ── WACC 산출 내역 ────────────────────────────────────────────────
@pytest.fixture
def market(monkeypatch):
    monkeypatch.setattr(wacc_engine, "_rf", lambda c, t: _v(3.50, "%", "ECOS 국고채 10년"))
    monkeypatch.setattr(wacc_engine.damodaran, "equity_risk_premium",
                        lambda c: _v(5.00, "%", "Damodaran ERP"))
    monkeypatch.setattr(wacc_engine.damodaran, "corporate_tax_rate",
                        lambda c: _v(24.00, "%", "Damodaran 법인세"))


def test_wacc_exposes_every_input_as_its_own_value(market):
    v = wacc_engine.compute_wacc("KR", beta=1.20, cost_of_debt_pct=5.00, debt_to_value=0.25)

    ex = v.extras
    assert ex["risk_free"].value == 3.50
    assert ex["equity_risk_premium"].value == 5.00
    assert ex["tax_rate"].value == 24.00
    assert ex["beta_used"].value == 1.20
    assert ex["cost_of_equity"].value == pytest.approx(9.50)        # 3.5 + 1.2×5
    assert ex["cost_of_debt_pretax"].value == 5.00
    assert ex["cost_of_debt_after_tax"].value == pytest.approx(3.80)  # 5 × (1−0.24)
    assert ex["debt_to_value"].value == 0.25
    assert v.value == pytest.approx(0.75 * 9.50 + 0.25 * 3.80, abs=0.01)


def test_wacc_inputs_keep_their_own_sources(market):
    """표의 각 행에 출처를 붙이려면 항목마다 출처가 살아 있어야 한다."""
    v = wacc_engine.compute_wacc("KR", 1.2, 5.0, 0.25)
    assert "ECOS" in v.extras["risk_free"].provenance.source
    assert "Damodaran" in v.extras["equity_risk_premium"].provenance.source


# ── beta_source 분기 ──────────────────────────────────────────────
def test_beta_source_peer_requires_peers():
    with pytest.raises(DataError) as e:
        wacc_engine.compute_wacc_auto("삼성전자", beta_source="peer")
    assert "peers" in str(e.value)


def test_beta_source_industry_requires_industry():
    with pytest.raises(DataError) as e:
        wacc_engine.compute_wacc_auto("삼성전자", beta_source="industry")
    assert "industry" in str(e.value)


def test_unknown_beta_source_is_rejected():
    with pytest.raises(DataError) as e:
        wacc_engine.compute_wacc_auto("삼성전자", beta_source="whatever")
    assert "auto/regression/industry/peer" in str(e.value)


def test_peer_path_is_wired_through_to_the_engine(peers, market, monkeypatch):
    monkeypatch.setattr(wacc_engine.damodaran, "industry_wacc",
                        lambda i, r: {"debt_to_value": _v(0.30, ""), "cost_of_debt": _v(5.0, "%")})
    v = wacc_engine.compute_wacc_auto(
        "대상사", industry="Semiconductor", beta_source="peer",
        peers=["가전자", "나반도체", "다소재"], cost_of_debt_pct=5.0, debt_to_value=0.25)
    assert "Peer 3개 bottom-up" in v.provenance.note
    assert v.extras["beta"].label.startswith("Peer 평균 베타")

"""IB 피드백 묶음 3 — 회귀베타(#9) · mid-year convention(#12 일부).

운전자본(#8) 회귀 테스트는 의미론이 바뀐 자리에 그대로 두는 게 맞아서
test_industry_gates.py / test_dcf_inputs.py 안에서 갱신했다.
"""
from __future__ import annotations

import pytest

from engines import beta as beta_engine


# ── #9 회귀베타 ───────────────────────────────────────────────────────
def test_default_window_is_five_year_monthly():
    """실측: 월봉 5년이 주봉 5년보다 설명력이 일관되게 높다.

    R² (2026-08) — 삼성전자 0.813 vs 0.659, 현대차 0.595 vs 0.378, 기아 0.385 vs 0.222.
    짧은 간격일수록 비동기거래·호가스프레드 잡음이 공분산을 희석한다.
    """
    assert beta_engine.PRIMARY_WINDOW == ("month", 5)


def test_beta_for_does_not_hardcode_the_window():
    """예전엔 beta_for 가 week/5 를 명시적으로 넘겨 regression_beta 기본값이 무시됐다."""
    import inspect

    sig = inspect.signature(beta_engine.beta_for)
    assert sig.parameters["period"].default is None
    assert sig.parameters["years"].default is None


def test_blume_adjustment_pulls_toward_one():
    assert beta_engine.blume_adjust(1.0) == pytest.approx(1.0)
    assert beta_engine.blume_adjust(0.4) == pytest.approx(0.6, abs=1e-9)
    assert beta_engine.blume_adjust(1.6) == pytest.approx(1.4, abs=1e-9)


def test_blume_weights_sum_to_one():
    assert (beta_engine.BLUME_W_RAW + beta_engine.BLUME_W_MARKET) == pytest.approx(1.0)


def test_ols_returns_a_t_statistic():
    """R² 는 '시장이 얼마나 설명하나', t 는 '베타가 0 과 구분되나' — 다른 질문이다."""
    xs = [0.01, -0.02, 0.03, -0.01, 0.005, 0.02, -0.015]
    ys = [2 * x for x in xs]
    _slope, _i, _r2, t = beta_engine._ols(xs, ys)
    # 완전적합은 잔차가 0 -> t = 무한대. 예전엔 0 을 돌려줘 "|t|<2" 경고가 잘못 붙었다.
    assert t == float("inf")


def test_t_stat_is_small_when_the_market_explains_nothing():
    """시장과 무관한 계열이면 베타가 0 과 구분되지 않아야 한다.

    작은 표본을 손으로 만들면 우연히 상관이 생긴다(직접 겪었다 — R²가 0.56 나왔다).
    고정 시드로 독립 난수를 만들어 재현 가능하게 한다.
    """
    import random

    rnd = random.Random(42)
    xs = [rnd.gauss(0, 0.02) for _ in range(120)]
    ys = [rnd.gauss(0, 0.02) for _ in range(120)]     # xs 와 독립
    _s, _i, r2, t = beta_engine._ols(xs, ys)
    assert r2 < 0.1
    assert abs(t) < 2.0


def test_flat_series_gives_zero_t_not_infinity():
    """수익률이 완전히 일정하면 기울기 0 — 무한대가 아니라 0 이어야 한다."""
    xs = [0.01, -0.01, 0.02, -0.02, 0.015]
    ys = [0.01] * 5
    slope, _i, _r2, t = beta_engine._ols(xs, ys)
    assert slope == pytest.approx(0.0, abs=1e-12)
    assert t == 0.0


def test_low_r2_message_says_it_is_low_confidence_not_missing(monkeypatch):
    """'회귀베타 미확보' 라는 문구가 '기능이 없다' 는 오해를 만들었다."""
    import inspect

    src = inspect.getsource(beta_engine)
    assert "신뢰도 미달" in src
    assert "베타 산출 실패가 아님" in src or "'미확보'가 아니라" in src


def test_regression_gate_threshold_is_documented():
    assert beta_engine.R2_MIN == 0.3


def test_secondary_window_is_a_cross_check_not_a_selection():
    """R² 가 가장 높은 창을 고르면 데이터 마이닝이다 — 실측에서 1년 일봉이 대개 R² 가
    제일 높은데 베타는 가장 불안정하다(현대차 일봉 0.798 vs 월봉 1.258)."""
    import inspect

    src = inspect.getsource(beta_engine)
    assert "데이터 마이닝" in src
    assert beta_engine.SECONDARY_WINDOW == ("week", 2)


# ── mid-year convention ───────────────────────────────────────────────
def _dcf_stubs(monkeypatch):
    from core.schema import Provenance, SourceType, Value
    from engines import business_mix, dcf as dcf_engine
    from providers import damodaran

    def _v(value, unit="KRW", label="x", as_of=None):
        return Value(value, unit, label=label,
                     provenance=Provenance(source="테스트",
                                           source_type=SourceType.AUTHORITATIVE,
                                           source_url="", as_of=as_of))

    monkeypatch.setattr(dcf_engine.dart, "financial_item",
                        lambda *a, **k: _v(1_000_000, label="테스트 매출액", as_of="FY2025"))
    monkeypatch.setattr(dcf_engine.dart, "shares_outstanding",
                        lambda *a, **k: _v(1_000, "주", as_of="FY2025"))
    monkeypatch.setattr(damodaran, "corporate_tax_rate",
                        lambda c: _v(25.0, "%", as_of="2026-01-01"))
    monkeypatch.setattr(business_mix, "classify",
                        lambda *a, **k: {"company": "테스트", "kind": "industrial",
                                         "single_dcf_ok": True, "reason": "", "evidence": []})
    monkeypatch.setattr(dcf_engine.reality_check, "market_reference",
                        lambda *a, **k: {"market_cap": None, "price": None,
                                         "as_of": None, "error": "테스트"})


def test_mid_year_is_the_default(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    assert m["mid_year"] is True


def test_mid_year_discounts_at_t_minus_half(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    assert m["rows"][0]["df"] == pytest.approx(1 / (1.10 ** 0.5), abs=1e-9)
    assert m["rows"][1]["df"] == pytest.approx(1 / (1.10 ** 1.5), abs=1e-9)


def test_year_end_convention_still_available(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0,
                               mid_year=False)
    assert m["rows"][0]["df"] == pytest.approx(1 / 1.10, abs=1e-9)


def test_mid_year_raises_value_by_roughly_half_a_year(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    mid = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    end = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0,
                                 mid_year=False)
    # 예측기간 PV 만 (1+WACC)^0.5 배 = 약 +4.9%
    ratio = mid["pv_ufcf_sum"] / end["pv_ufcf_sum"]
    assert ratio == pytest.approx(1.10 ** 0.5, abs=1e-6)
    assert mid["ev"] > end["ev"]


def test_terminal_value_is_not_mid_year_discounted(monkeypatch):
    """TV 는 n년차 말 시점의 잔존가치다 — 여기에 기중할인을 쓰면 반년치 과대평가된다."""
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    mid = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    end = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0,
                                 mid_year=False)
    assert mid["pv_tv"] == pytest.approx(end["pv_tv"], rel=1e-12)


def test_note_states_which_convention_was_used(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    v = dcf_engine.evaluate("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    assert "기중할인(mid-year)" in (v.provenance.note or "")

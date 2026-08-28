"""IB 피드백 묶음 2 — 시장 대조 강제(#2) · 역산 진단(#1).

이 둘이 서로를 강화한다. 목표가에 맞춘 숫자는 시장 대조 블록에서 스스로 이상해 보이고,
역산 진단은 그 요청에 정당한 출구를 준다.
"""
from __future__ import annotations

import pytest

from core.schema import DataError
from engines import reality_check, reverse_dcf


def _rows(n=5, rev=1000.0, margin=0.10, da=0.03, capex=0.05, nwc=0.02, tax=25.0, g=0.05):
    rows, prev = [], rev
    for t in range(1, n + 1):
        r = prev * (1 + g)
        ebit = r * margin
        rows.append({"t": t, "rev": r, "ebit": ebit, "nopat": ebit * (1 - tax / 100),
                     "da": r * da, "capex": r * capex, "dnwc": (r - prev) * nwc,
                     "ufcf": 0.0, "df": 1.0, "pv": 0.0})
        prev = r
    return rows


def _model(**over):
    rows = over.pop("rows", None) or _rows()
    m = {"rows": rows, "ev": 10_000.0, "equity_value": 9_000.0, "wacc_pct": 9.0,
         "pv_tv": 6_000.0, "tv": 15_000.0, "per_share": 900.0, "net_debt": 1_000.0,
         "tax_pct": 25.0}
    m.update(over)
    return m


# ── #2 시장 대조 ──────────────────────────────────────────────────────
def test_premium_beyond_the_limit_must_be_disclosed():
    """415,204원을 시가 126,800원과 비교도 안 하고 내놓던 사고."""
    c = reality_check.evaluate(_model(), market_value=3_000.0)
    assert c["verdict"] == "disclose"
    assert any("시가총액 대비" in f for f in c["flags"])
    assert c["metrics"]["premium_pct"] == pytest.approx(200.0)


def test_premium_within_the_limit_is_ok():
    c = reality_check.evaluate(_model(), market_value=8_000.0)
    assert not any("시가총액 대비" in f for f in c["flags"])


def test_market_gap_is_disclosed_not_blocked():
    """시가와 다르다는 이유로 산출을 막으면 역발상 분석이 불가능해진다."""
    c = reality_check.evaluate(_model(), market_value=1.0)
    assert c["verdict"] == "disclose"
    assert c["verdict"] != "nm"


def test_structure_checks_run_even_without_a_market_price():
    """비상장이라 시가가 없어도 검증을 통째로 건너뛰면 안 된다."""
    c = reality_check.evaluate(_model(), market_value=None)
    assert "tv_share_pct" in c["metrics"]
    assert "incremental_roic_pct" in c["metrics"]


def test_incremental_roic_below_wacc_is_flagged():
    """재투자 대비 이익 증가가 자본비용에 못 미치면 성장이 가치를 만들 수 없다."""
    rows = _rows(margin=0.03, capex=0.12, da=0.02, g=0.15)
    c = reality_check.evaluate(_model(rows=rows, wacc_pct=10.0))
    assert c["metrics"]["incremental_roic_pct"] < 10.0
    assert any("증분 ROIC" in f for f in c["flags"])


def test_high_roic_is_not_flagged():
    rows = _rows(margin=0.25, capex=0.04, da=0.035, g=0.10)
    c = reality_check.evaluate(_model(rows=rows, wacc_pct=8.0))
    assert not any("증분 ROIC" in f for f in c["flags"])


def test_incremental_roic_is_skipped_when_there_is_no_real_reinvestment():
    """유지보수 수준의 재투자에 증분 ROIC 를 논하면 잡음만 나온다."""
    rows = _rows(capex=0.030, da=0.030, nwc=0.0, g=0.02)
    c = reality_check.evaluate(_model(rows=rows))
    assert "incremental_roic_pct" not in c["metrics"]


def test_tv_share_above_the_limit_is_flagged():
    c = reality_check.evaluate(_model(ev=10_000.0, pv_tv=8_500.0))
    assert any("Terminal Value" in f for f in c["flags"])


def test_exit_multiple_far_above_entry_is_flagged():
    c = reality_check.evaluate(_model(tv=90_000.0))
    assert any("청산배수" in f for f in c["flags"])


def test_multiples_are_nm_when_ev_is_negative():
    """EV 가 음수면 -13.1x 같은 값이 나온다 — 숫자를 내보내면 안 된다."""
    c = reality_check.evaluate(_model(ev=-5_000.0, equity_value=-6_000.0))
    assert c["metrics"].get("multiples") == "NM (EV ≤ 0)"
    assert "implied_entry_ev_ebitda" not in c["metrics"]


def test_market_lookup_failure_does_not_raise(monkeypatch):
    """시가 조회 실패가 DCF 자체를 막으면 안 된다."""
    from engines import market_data

    monkeypatch.setattr(market_data, "resolve",
                        lambda *a, **k: (_ for _ in ()).throw(DataError("비상장")))
    ref = reality_check.market_reference("X", "KR")
    assert ref["market_cap"] is None and ref["error"]


def test_dcf_runs_the_check_without_being_asked():
    import inspect

    from engines import dcf as dcf_engine

    src = inspect.getsource(dcf_engine.build_model)
    assert "reality_check.market_reference" in src, "시장 대조가 선택적으로 남아 있다"


def test_dcf_note_leads_with_the_check():
    import inspect

    from engines import dcf as dcf_engine

    src = inspect.getsource(dcf_engine.evaluate)
    assert "reality_line +" in src


def test_check_metrics_are_exposed_as_extras():
    """note 문자열만 주면 LLM 이 다시 파싱하다 값을 바꾼다."""
    from engines import dcf as dcf_engine

    assert "premium_pct" in dcf_engine._REALITY_LABELS
    assert "incremental_roic_pct" in dcf_engine._REALITY_LABELS
    assert "tv_share_pct" in dcf_engine._REALITY_LABELS
    # extras 키는 check_ 접두사로 나가 다른 값과 섞이지 않는다
    import inspect

    assert 'f"check_{key}"' in inspect.getsource(dcf_engine._reality_extras)


# ── #1 역산 진단 ──────────────────────────────────────────────────────
def test_solver_finds_the_assumption_that_hits_the_target():
    f = lambda x: x * 100          # noqa: E731
    assert reverse_dcf._solve(f, 0.0, 10.0, 500.0) == pytest.approx(5.0, abs=1e-3)


def test_solver_reports_impossible_when_out_of_reach():
    f = lambda x: x * 100          # noqa: E731
    assert reverse_dcf._solve(f, 0.0, 1.0, 5_000.0) is None


def test_lower_than_history_is_conservative_not_a_warning():
    """목표가가 과거보다 *낮은* 성장률로 성립하면 그건 경고 대상이 아니다."""
    verdict, note = reverse_dcf._percentile_note("필요 매출성장률", 6.2,
                                                 [6.2, 12.0, 23.9, 11.0])
    assert verdict == "ok"
    assert "보수적" in note


def test_far_above_history_is_indefensible():
    verdict, note = reverse_dcf._percentile_note("필요 매출성장률", 43.9,
                                                 [6.2, 12.0, 23.9, 11.0])
    assert verdict == "indefensible"
    assert "방어할 수 없습니다" in note


def test_moderately_above_history_is_a_stretch():
    verdict, _ = reverse_dcf._percentile_note("필요 영업이익률", 13.3,
                                              [7.2, 9.0, 9.4, 10.1, 11.8])
    assert verdict == "stretch"


def test_wacc_direction_is_inverted():
    """WACC 은 **낮을수록** 공격적이다 — 방향을 안 뒤집으면 판정이 거꾸로 나온다."""
    low, _ = reverse_dcf._percentile_note("필요 WACC", 4.0, [8.0, 9.0, 10.0],
                                          higher_is_aggressive=False)
    high, _ = reverse_dcf._percentile_note("필요 WACC", 12.0, [8.0, 9.0, 10.0],
                                           higher_is_aggressive=False)
    assert low in ("stretch", "indefensible")
    assert high == "ok"


def test_unknown_history_is_not_silently_ok():
    verdict, _ = reverse_dcf._percentile_note("필요 매출성장률", 40.0, [])
    assert verdict == "unknown"


def test_reverse_dcf_never_returns_a_price():
    """주당가치를 내놓으면 그게 곧 '목표가에 맞춘 기본안' 이 된다."""
    import inspect

    src = inspect.getsource(reverse_dcf.evaluate)
    assert "value=None" in src


def test_watermark_is_on_the_result_and_every_extra():
    import inspect

    src = inspect.getsource(reverse_dcf.evaluate)
    # 결과 note 와 extras 양쪽에 낙인이 들어가야 한다 — extras 만 인용해 가는 경우가 있다.
    assert src.count("WATERMARK") >= 2, "extras 에도 낙인이 찍혀야 한다"
    assert "NOT A BASE CASE" in reverse_dcf.WATERMARK


def test_target_must_be_positive():
    with pytest.raises(DataError):
        reverse_dcf.diagnose("X", 0, wacc_pct=9, net_debt=0, revenue_growth_pct=5,
                             ebit_margin_pct=10, da_pct=3, capex_pct=4, nwc_pct=2,
                             terminal_growth_pct=2)


def test_compute_dcf_has_no_target_value_argument():
    """우회 경로를 아예 만들지 않는 것이 이 설계의 핵심이다."""
    from agent import registry

    props = next(s for s in registry.tool_schemas()
                 if s["name"] == "compute_dcf")["input_schema"]["properties"]
    for banned in ("target_value", "target_per_share", "target_price"):
        assert banned not in props


def test_reverse_tool_is_registered_and_routes_target_requests():
    from agent import registry

    s = next(s for s in registry.tool_schemas()
             if s["name"] == "diagnose_implied_assumptions")
    assert "목표주가" in s["description"]
    assert "TARGET-FITTED" in s["description"]
    req = s["input_schema"]["required"]
    assert "target_per_share" in req


def test_prompt_routes_target_seeking_to_the_diagnostic():
    from agent import brain

    p = brain.SYSTEM_PROMPT
    assert "diagnose_implied_assumptions" in p
    assert "190,000" in p, "실측 사고를 근거로 남긴다"
    assert "가정을 결론에 맞추는" in p


def test_prompt_requires_surfacing_the_check_block_first():
    from agent import brain

    p = brain.SYSTEM_PROMPT
    assert "[시장·구조 대조]" in p
    assert "증분 ROIC" in p
    assert "각주로 내리지 마라" in p

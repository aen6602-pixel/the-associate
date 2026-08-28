"""IB 피드백 묶음 4 — 사실 원장(#3) · 가정 정합성(#6) · 순현금(#10) · TV 병기(#12).
"""
from __future__ import annotations

import inspect

import pytest

from core import ledger
from core.schema import SourceType
from engines import assumption_check


def _call(name, inp, value, unit="KRW", as_of="FY2025",
          st=SourceType.AUTHORITATIVE, ok=True):
    return {"name": name, "input": inp,
            "result": {"ok": ok, "value": {
                "value": value, "unit": unit, "label": f"{inp.get('company','')} {name}",
                "provenance": {"source": "DART", "source_type": st, "as_of": as_of,
                               "source_url": "u"}}}}


def _session(*traces):
    return [{"role": "assistant", "content": "x", "trace": list(t)} for t in traces]


# ── #3 사실 원장 ──────────────────────────────────────────────────────
def test_ledger_is_rebuilt_from_the_trace_not_a_second_store():
    """별도 저장소를 두면 trace 와 어긋나는 순간 어느 쪽이 진실인지 알 수 없다."""
    src = inspect.getsource(ledger.build)
    assert 'msg.get("trace")' in src


def test_verified_values_survive_into_later_turns():
    msgs = _session([_call("get_financial_item", {"company": "기아", "item": "net_income"},
                           12_500_000_000_000)])
    block = ledger.block_for(msgs)
    assert "12,500,000,000,000" in block
    assert "재사용" in block


def test_failed_calls_never_enter_the_ledger():
    msgs = _session([_call("get_net_debt", {"company": "기아"}, None, ok=False)])
    assert ledger.build(msgs) == []


def test_null_values_never_enter_the_ledger():
    msgs = _session([_call("get_net_debt", {"company": "기아"}, None)])
    assert ledger.build(msgs) == []


def test_llm_estimates_are_excluded():
    """추정을 사실로 굳히면 원장이 오히려 오류를 고정한다."""
    msgs = _session([_call("get_financial_item", {"company": "X", "item": "revenue"},
                           100, st=SourceType.LLM_ESTIMATE)])
    assert ledger.build(msgs) == []


def test_values_read_from_filings_are_included():
    """parsed_authoritative 는 문서ID·인용이 있으므로 사실이다."""
    msgs = _session([_call("get_financial_item", {"company": "X", "item": "capex"},
                           100, st=SourceType.PARSED_AUTHORITATIVE)])
    assert len(ledger.build(msgs)) == 1


def test_calculated_results_are_not_facts():
    """compute_* 는 가정에 따라 달라진다 — 사실로 굳히면 옛 결론이 되살아난다."""
    msgs = _session([_call("compute_dcf", {"company": "기아"}, 190000, unit="KRW/주")])
    assert ledger.build(msgs) == []


def test_filing_text_reads_are_not_facts():
    msgs = _session([_call("read_dart_filing", {"rcept_no": "1"}, 1, unit="filing_text")])
    assert ledger.build(msgs) == []


def test_higher_grade_wins_over_recency():
    """원문 파싱 → 나중에 XBRL 로 재확인되면 승격돼야 한다."""
    msgs = _session(
        [_call("get_financial_item", {"company": "X", "item": "capex"}, 111,
               st=SourceType.PARSED_AUTHORITATIVE)],
        [_call("get_financial_item", {"company": "X", "item": "capex"}, 222,
               st=SourceType.AUTHORITATIVE)])
    facts = ledger.build(msgs)
    assert [f["value"] for f in facts] == [222]


def test_same_key_and_grade_keeps_the_latest():
    msgs = _session(
        [_call("get_financial_item", {"company": "X", "item": "revenue"}, 100)],
        [_call("get_financial_item", {"company": "X", "item": "revenue"}, 200)])
    assert [f["value"] for f in ledger.build(msgs)] == [200]


def test_different_periods_are_separate_facts_and_flagged():
    """기간이 다른 것 자체는 정상이지만, 한 답변에서 섞이면 기준 혼용이다."""
    msgs = _session([
        _call("get_financial_item", {"company": "기아", "item": "net_income"}, 100,
              as_of="FY2025"),
        _call("get_financial_item", {"company": "기아", "item": "net_income"}, 90,
              as_of="FY2024")])
    facts = ledger.build(msgs)
    assert len(facts) == 2
    conf = ledger.conflicts(facts)
    assert conf and "FY2024" in conf[0] and "FY2025" in conf[0]
    assert "기준기간이 섞인" in ledger.render(facts, conf)


def test_empty_history_renders_nothing():
    assert ledger.block_for([]) == ""
    assert ledger.render([]) == ""


def test_ledger_is_capped():
    traces = [[_call("get_financial_item", {"company": f"C{i}", "item": "revenue"}, i)]
              for i in range(60)]
    assert len(ledger.build(_session(*traces), limit=10)) == 10


def test_prompt_receives_the_ledger(monkeypatch):
    from agent import brain

    seen = {}

    def fake(question, history, max_rounds, model=None, effort=None, ledger_block=""):
        seen["block"] = ledger_block
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr(brain, "_answer_openai", fake)
    msgs = _session([_call("get_financial_item", {"company": "기아", "item": "net_income"},
                           12_500_000_000_000)])
    list(brain.answer("q", history=msgs, provider="openai"))
    assert "12,500,000,000,000" in seen["block"]


def test_system_prompt_embeds_the_block():
    from agent import brain

    out = brain._system_prompt("\n## 이 세션에서 이미 검증된 값\n- X: 1\n")
    assert "이미 검증된 값" in out


# ── #6 가정 정합성 ────────────────────────────────────────────────────
_BASE = dict(revenue_growth_pct=3.0, ebit_margin_pct=7.0, da_pct=3.0, capex_pct=4.0,
             terminal_growth_pct=2.0, wacc_pct=8.5)


def test_coherent_assumptions_pass():
    assert assumption_check.check(**_BASE)["verdict"] == "ok"


def test_growth_far_above_gdp_is_flagged():
    """16.23% 를 5년 유지하면 명목 GDP 의 4.6배다."""
    r = assumption_check.check(**{**_BASE, "revenue_growth_pct": 16.23})
    assert any("GDP" in f for f in r["flags"])


def test_terminal_growth_above_the_long_run_range_is_flagged():
    r = assumption_check.check(**{**_BASE, "terminal_growth_pct": 6.0})
    assert any("영구성장률" in f for f in r["flags"])


def test_terminal_growth_above_risk_free_is_flagged():
    r = assumption_check.check(**{**_BASE, "terminal_growth_pct": 3.9},
                               risk_free_pct=3.0)
    assert any("무위험수익률" in f for f in r["flags"])


def test_capex_below_depreciation_is_flagged():
    """감가상각만큼도 재투자하지 않으면 자산이 소멸한다."""
    r = assumption_check.check(**{**_BASE, "da_pct": 6.0, "capex_pct": 3.0})
    assert any("소멸" in f for f in r["flags"])
    assert r["metrics"]["capex_to_da"] == pytest.approx(0.5)


def test_growth_without_reinvestment_is_flagged():
    r = assumption_check.check(**{**_BASE, "revenue_growth_pct": 10.0,
                                  "da_pct": 5.0, "capex_pct": 5.0})
    assert any("자본 투입 없이" in f for f in r["flags"])


def test_best_of_both_worlds_combination_is_flagged():
    """서로 다른 국면에서 뽑은 드라이버를 한 세트로 묶은 사고."""
    r = assumption_check.check(**{**_BASE, "revenue_growth_pct": 16.0,
                                  "ebit_margin_pct": 11.5},
                               growth_history=[2.0, 5.0, 16.5, 8.0],
                               margin_history=[7.2, 9.0, 11.8, 8.5])
    assert any("동시에 최선" in f for f in r["flags"])


def test_history_band_breaches_are_reported():
    r = assumption_check.check(**{**_BASE, "ebit_margin_pct": 20.0},
                               margin_history=[7.0, 8.0, 9.0])
    assert any("과거 최고" in f for f in r["flags"])
    assert r["metrics"]["margin_band"] == (7.0, 9.0, pytest.approx(8.0))


def test_wacc_below_g_is_flagged():
    r = assumption_check.check(**{**_BASE, "wacc_pct": 1.0})
    assert any("TV 가 성립하지" in f for f in r["flags"])


def test_check_does_not_block():
    """판단이 필요한 영역이라 산출을 막지 않는다 — 알리기만 한다."""
    src = inspect.getsource(assumption_check)
    assert "막지 않는다" in src
    r = assumption_check.check(**{**_BASE, "revenue_growth_pct": 50.0})
    assert r["verdict"] == "disclose"


def test_dcf_runs_the_assumption_check():
    from engines import dcf as dcf_engine

    assert "assumption_check.check" in inspect.getsource(dcf_engine.build_model)
    assert "assumption_line" in inspect.getsource(dcf_engine.evaluate)


# ── #10 순현금: 운영현금 vs 잉여현금 ─────────────────────────────────
def test_operating_cash_is_not_deducted_from_ev(monkeypatch):
    from engines import dcf_inputs
    from providers import dart

    def _v(x, label="x"):
        from core.schema import Provenance, Value

        return Value(x, "KRW", label=label,
                     provenance=Provenance(source="DART", source_url="",
                                           source_type=SourceType.AUTHORITATIVE,
                                           as_of="FY2025"))

    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(100, "X 단기차입금"), "long_term": _v(200), "lease": _v(0)})
    monkeypatch.setattr(dart, "financial_item",
                        lambda c, item, *a, **k: _v(1000 if item == "revenue" else 500))
    monkeypatch.setattr(dcf_inputs, "_finance_arm_note", lambda *a, **k: "")

    full = dcf_inputs.net_debt("X")
    assert full.value == 300 - 500                      # 현금 전액 차감
    partial = dcf_inputs.net_debt("X", operating_cash_pct=2.0)
    assert partial.value == 300 - (500 - 20)            # 매출 1000 의 2% 는 운영현금
    assert "운영현금" in partial.provenance.note


def test_full_net_cash_carries_a_warning(monkeypatch):
    from engines import dcf_inputs
    from providers import dart
    from core.schema import Provenance, Value

    def _v(x, label="x"):
        return Value(x, "KRW", label=label,
                     provenance=Provenance(source="DART", source_url="",
                                           source_type=SourceType.AUTHORITATIVE,
                                           as_of="FY2025"))

    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(10, "X 단기차입금"), "long_term": _v(0), "lease": _v(0)})
    monkeypatch.setattr(dart, "financial_item", lambda c, item, *a, **k: _v(500))
    monkeypatch.setattr(dcf_inputs, "_finance_arm_note", lambda *a, **k: "")
    v = dcf_inputs.net_debt("X")
    assert v.value < 0
    assert "operating_cash_pct" in (v.provenance.note or "")


def test_operating_cash_cannot_exceed_actual_cash(monkeypatch):
    from engines import dcf_inputs
    from providers import dart
    from core.schema import Provenance, Value

    def _v(x, label="x"):
        return Value(x, "KRW", label=label,
                     provenance=Provenance(source="DART", source_url="",
                                           source_type=SourceType.AUTHORITATIVE,
                                           as_of="FY2025"))

    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(0, "X 단기차입금"), "long_term": _v(0), "lease": _v(0)})
    # 현금 10, 매출 1000 → 2% = 20 이지만 현금이 10 뿐이다
    monkeypatch.setattr(dart, "financial_item",
                        lambda c, item, *a, **k: _v(1000 if item == "revenue" else 10))
    monkeypatch.setattr(dcf_inputs, "_finance_arm_note", lambda *a, **k: "")
    v = dcf_inputs.net_debt("X", operating_cash_pct=2.0)
    assert v.value == 0, "잉여현금이 음수가 되면 안 된다"


# ── #12 TV 병기 ───────────────────────────────────────────────────────
def _dcf_stubs(monkeypatch):
    from core.schema import Provenance, Value
    from engines import business_mix, dcf as dcf_engine
    from providers import damodaran

    def _v(value, unit="KRW", as_of=None):
        return Value(value, unit, label="테스트 매출액",
                     provenance=Provenance(source="테스트", source_url="",
                                           source_type=SourceType.AUTHORITATIVE, as_of=as_of))

    monkeypatch.setattr(dcf_engine.dart, "financial_item",
                        lambda *a, **k: _v(1_000_000, as_of="FY2025"))
    monkeypatch.setattr(dcf_engine.dart, "shares_outstanding",
                        lambda *a, **k: _v(1_000, "주", as_of="FY2025"))
    monkeypatch.setattr(damodaran, "corporate_tax_rate", lambda c: _v(25.0, "%"))
    monkeypatch.setattr(business_mix, "classify",
                        lambda *a, **k: {"company": "테스트", "kind": "industrial",
                                         "single_dcf_ok": True, "reason": "", "evidence": []})
    monkeypatch.setattr(dcf_engine.reality_check, "market_reference",
                        lambda *a, **k: {"market_cap": None, "price": None,
                                         "as_of": None, "error": "테스트"})


def test_gordon_is_the_default_and_reports_its_implied_multiple(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    assert m["tv_method"] == "Gordon Growth"
    assert m["implied_exit_multiple"] > 0
    assert m["tv_exit"] is None


def test_exit_multiple_replaces_the_terminal_value(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0,
                               exit_multiple=8.0)
    assert m["tv_method"] == "exit multiple"
    last = m["rows"][-1]
    assert m["tv"] == pytest.approx(8.0 * (last["ebit"] + last["da"]))


def test_note_states_the_tv_method(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    v = dcf_engine.evaluate("테스트", 10.0, 0, 5.0, 20.0, 3.0, 4.0, 2.0, 2.0)
    assert "TV 방식 Gordon Growth" in (v.provenance.note or "")
    assert "내재 청산배수" in (v.provenance.note or "")

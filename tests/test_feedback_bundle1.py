"""IB 관점 테스트 피드백 묶음 1 — 주식수·출처등급·재현성·세율·전송실패.

각 테스트는 실측으로 보고된 증상을 그대로 고정한다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import runid
from core.schema import DataError, SourceType, is_sourced

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


# ── #5 주식수 정의 ────────────────────────────────────────────────────
def test_dcf_uses_outstanding_not_issued():
    """주당가치 분모는 자기주식을 뺀 유통주식수여야 한다.

    실측(기아): DCF 397,672,632 vs 시총역산 390,413,249 를 "기준 차이" 로 고지만 하고
    끝냈다. 주당가치가 최종 산출물인데 분모가 미정이면 안 된다.
    """
    import inspect

    from engines import dcf as dcf_engine

    src = inspect.getsource(dcf_engine.build_model)
    assert 'basis="outstanding"' in src, "DCF 가 발행주식총수(issued)를 쓰고 있다"


def test_share_value_exposes_common_and_preferred(monkeypatch):
    """우선주가 있으면 주당가치가 보통주 전용이 아니라는 걸 알 수 있어야 한다."""
    from providers import dart

    captured = {}

    def fake_get_json(url, **kw):
        captured["called"] = True
        return {"status": "000", "list": [
            {"se": "합계", "distb_stock_co": "6,630,180,138", "tesstk_co": "105,432,448",
             "now_to_isu_stock_totqy": "8,975,138,200", "rcept_no": "1"},
            {"se": "보통주", "distb_stock_co": "5,827,808,935", "tesstk_co": "0",
             "rcept_no": "1"},
            {"se": "우선주", "distb_stock_co": "802,371,203", "tesstk_co": "0",
             "rcept_no": "1"},
        ]}

    monkeypatch.setattr(dart, "get_json", fake_get_json)
    monkeypatch.setattr(dart, "resolve", lambda c: {
        "corp_code": "1", "corp_name": "삼성전자", "stock_code": "005930"})
    monkeypatch.setattr(dart, "_latest_year", lambda *a, **k: 2025)
    monkeypatch.setattr(dart.config, "require", lambda *a, **k: "k")

    v = dart.shares_outstanding("삼성전자", basis="outstanding")
    assert v.value == 6_630_180_138
    assert v.extras["common_outstanding"].value == 5_827_808_935
    assert v.extras["preferred_outstanding"].value == 802_371_203
    assert v.extras["treasury"].value == 105_432_448


def test_issued_equals_outstanding_plus_treasury(monkeypatch):
    from providers import dart

    monkeypatch.setattr(dart, "get_json", lambda url, **kw: {"status": "000", "list": [
        {"se": "합계", "distb_stock_co": "388,602,725", "tesstk_co": "1,810,273",
         "rcept_no": "1"}]})
    monkeypatch.setattr(dart, "resolve", lambda c: {
        "corp_code": "1", "corp_name": "기아", "stock_code": "000270"})
    monkeypatch.setattr(dart, "_latest_year", lambda *a, **k: 2025)
    monkeypatch.setattr(dart.config, "require", lambda *a, **k: "k")

    out = dart.shares_outstanding("기아", basis="outstanding").value
    iss = dart.shares_outstanding("기아").value
    assert iss - out == 1_810_273


def test_implied_shares_docstring_explains_the_treasury_gap():
    """시총 역산치와 유통주식수가 다른 건 오류가 아니라 정의 차이 — 그걸 밝혀야 한다."""
    from providers import naver

    doc = naver.implied_common_shares.__doc__ or ""
    assert "자기주식" in doc and "포함" in doc
    assert "정의 차이" in doc


# ── #13 출처 등급 ─────────────────────────────────────────────────────
def test_parsed_authoritative_grade_exists():
    assert SourceType.PARSED_AUTHORITATIVE in SourceType.ALL


def test_parsed_authoritative_outranks_llm_estimate():
    assert (SourceType.RANK[SourceType.PARSED_AUTHORITATIVE]
            > SourceType.RANK[SourceType.LLM_ESTIMATE])


def test_sourced_values_are_not_freely_retractable():
    """공시 원문에서 읽은 값이 '검증 안 됨' 으로 철회되던 연쇄를 막는다."""
    assert is_sourced(SourceType.AUTHORITATIVE)
    assert is_sourced(SourceType.PARSED_AUTHORITATIVE)
    assert not is_sourced(SourceType.LLM_ESTIMATE)


def test_provenance_accepts_the_new_grade():
    from core.schema import Provenance

    Provenance(source="DART 원문", source_url="http://x",
               source_type=SourceType.PARSED_AUTHORITATIVE)


def test_filing_text_tools_no_longer_demand_llm_estimate():
    """원문 읽기 도구가 '읽은 숫자는 llm_estimate 로' 라고 지시하면 안 된다."""
    from agent import registry

    for name in ("read_dart_filing", "read_edinet_filing", "read_sec_filing"):
        desc = next(s for s in registry.tool_schemas() if s["name"] == name)["description"]
        assert "parsed_authoritative" in desc, f"{name}: 새 등급을 안내하지 않는다"
        assert "llm_estimate 로 표시하고" not in desc, f"{name}: 여전히 강등을 지시한다"


@pytest.mark.parametrize("renderer", ["web", "html", "xlsx"])
def test_every_renderer_knows_the_new_grade(renderer):
    """등급을 추가하고 렌더러를 빼먹으면 화면에 빈칸이 뜬다."""
    if renderer == "web":
        assert "parsed_authoritative:" in JS
    elif renderer == "html":
        from excel import html_report

        assert "parsed_authoritative" in html_report._TIER
    else:
        from excel import workbook

        assert SourceType.PARSED_AUTHORITATIVE in workbook._TIER_KO


def test_prompt_distinguishes_parsed_from_estimated():
    from agent import brain

    assert "parsed_authoritative" in brain.SYSTEM_PROMPT
    assert "9,144" in brain.SYSTEM_PROMPT, "실측 사고를 근거로 남겨 회귀를 막는다"


# ── #3 세션 내 일관성(원장 이전의 최소 규칙) ──────────────────────────
def test_prompt_forbids_retracting_verified_values():
    from agent import brain

    p = brain.SYSTEM_PROMPT
    assert "명시적 반증" in p
    assert "철회" in p


# ── #14 재현성 ────────────────────────────────────────────────────────
def test_same_inputs_give_the_same_run_id():
    a = runid.stamp("dcf", {"company": "기아", "wacc_pct": 8.7})
    b = runid.stamp("dcf", {"company": "기아", "wacc_pct": 8.7})
    assert a["run_id"] == b["run_id"]


def test_different_inputs_give_different_run_ids():
    a = runid.stamp("dcf", {"company": "기아", "wacc_pct": 8.7})
    b = runid.stamp("dcf", {"company": "기아", "wacc_pct": 8.8})
    assert a["run_id"] != b["run_id"]


def test_float_noise_does_not_change_the_run_id():
    """8.700000000000001 과 8.7 이 다른 run 으로 잡히면 재현성 표시가 거짓말이 된다."""
    a = runid.stamp("dcf", {"wacc_pct": 8.7})
    b = runid.stamp("dcf", {"wacc_pct": 8.7 + 1e-15})
    assert a["run_id"] == b["run_id"]


def test_engine_version_is_part_of_the_run_id():
    a = runid.stamp("dcf", {"x": 1})
    b = runid.stamp("comps", {"x": 1})
    assert a["run_id"] != b["run_id"]
    assert a["engine_version"] and b["engine_version"]


def test_run_line_is_renderable():
    line = runid.line(runid.stamp("dcf", {"x": 1}))
    assert line.startswith("run ") and "dcf v" in line and "inputs " in line


@pytest.mark.parametrize("engine", ["dcf", "comps", "sangjeung"])
def test_engines_stamp_their_runs(engine):
    import inspect
    import importlib

    mod = importlib.import_module(f"engines.{engine}")
    src = inspect.getsource(mod.evaluate)
    assert "runid.stamp(" in src, f"{engine}: run 각인이 없다"
    assert "runid.line(run)" in src, f"{engine}: note 에 run 을 안 남긴다"


# ── #11 세율 정의 ─────────────────────────────────────────────────────
def test_effective_tax_excludes_loss_years_and_outliers(monkeypatch):
    from engines import dcf_inputs
    from providers import dart

    def series(item, *a, **k):
        data = {
            # 2023 은 세전적자, 2022 는 환급으로 음수 세율 → 둘 다 제외돼야 한다
            "tax_expense": {2025: 300, 2024: 250, 2023: 10, 2022: -100},
            "net_income": {2025: 700, 2024: 750, 2023: -500, 2022: 900},
        }[item]
        return {"corp_name": "X", "stock_code": "1", "corp_code": "1", "item": item,
                "rcept": "1", "filing_date": None,
                "series": [{"year": y, "amount": v} for y, v in data.items()]}

    monkeypatch.setattr(dart, "financial_item_nyear",
                        lambda c, item, *a, **k: series(item))
    monkeypatch.setattr(dart, "resolve", lambda c: {"corp_name": "X"})
    v = dcf_inputs.effective_tax_rate("X", 4)
    # 2025: 300/(700+300)=30.0%, 2024: 250/(750+250)=25.0% → 중앙값 27.5%
    # 2023(세전 -490)·2022(-12.5%)는 제외
    assert v.value == pytest.approx(27.5, abs=0.01)
    assert "제외" in v.provenance.note
    assert "2023" in v.provenance.note and "2022" in v.provenance.note


def test_effective_tax_refuses_when_no_usable_year(monkeypatch):
    """전 연도가 적자면 조용히 0% 를 쓰면 안 된다."""
    from engines import dcf_inputs
    from providers import dart

    monkeypatch.setattr(dart, "financial_item_nyear", lambda c, item, *a, **k: {
        "corp_name": "X", "stock_code": "1", "corp_code": "1", "item": item,
        "rcept": "1", "filing_date": None,
        "series": [{"year": 2025, "amount": -100 if item == "net_income" else 10}]})
    monkeypatch.setattr(dart, "resolve", lambda c: {"corp_name": "X"})
    with pytest.raises(DataError, match="유효세율"):
        dcf_inputs.effective_tax_rate("X")


def test_terminal_value_uses_the_marginal_rate_not_the_effective_one():
    """영구 구간에 공제·감면이 이어진다고 볼 근거가 없다."""
    import inspect

    from engines import dcf as dcf_engine

    src = inspect.getsource(dcf_engine.build_model)
    assert "term_taxr" in src
    assert re.search(r"ufcf_n = \(_last\[.ebit.\] \* \(1 - term_taxr\)", src)


def test_dcf_note_shows_both_tax_rates():
    import inspect

    from engines import dcf as dcf_engine

    src = inspect.getsource(dcf_engine.evaluate)
    assert "예측기간" in src and "계속가치" in src


def test_effective_tax_tool_is_registered():
    from agent import registry

    s = next(s for s in registry.tool_schemas() if s["name"] == "get_effective_tax_rate")
    assert "예측기간" in s["description"] and "계속가치" in s["description"]


def test_compute_dcf_accepts_a_separate_terminal_tax_rate():
    import inspect

    from agent import registry

    props = next(s for s in registry.tool_schemas()
                 if s["name"] == "compute_dcf")["input_schema"]["properties"]
    assert "terminal_tax_rate_pct" in props
    # 스키마가 광고하는 인자는 실제로 바인딩돼야 한다(엑셀 이음매 사고와 같은 유형)
    fn = registry.REGISTRY["compute_dcf"]["fn"]
    inspect.signature(fn).bind(company="X", wacc_pct=8.0, net_debt=0,
                               revenue_growth_pct=5.0, ebit_margin_pct=10.0, da_pct=3.0,
                               capex_pct=4.0, nwc_pct=2.0, terminal_growth_pct=2.0,
                               terminal_tax_rate_pct=26.4)


# ── #15 전송 실패 시 메시지 유실 ──────────────────────────────────────
def test_send_failure_restores_the_question():
    assert "restoreQuestion" in JS
    assert "serverAccepted" in JS


def test_server_acceptance_is_decided_by_the_start_event():
    """start 를 받았으면 서버 세션에 기록된 것 — 되돌리면 중복 전송이 된다."""
    assert re.search(r"ev\.type === 'start'[\s\S]{0,120}serverAccepted = true", JS)


def test_failed_send_removes_the_optimistic_user_message():
    assert "state.messages.pop()" in JS


def test_restore_does_not_overwrite_what_the_user_typed_since():
    assert re.search(r"function restoreQuestion[\s\S]{0,200}questionBox\.value\.trim\(\)\) return",
                     JS)


def test_retry_banner_exists_and_starts_hidden():
    assert 'id="retry-banner"' in HTML
    assert re.search(r'id="retry-banner"[^>]*hidden', HTML)
    assert "showRetry" in JS and "dismissRetry" in JS

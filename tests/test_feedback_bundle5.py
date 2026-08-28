"""IB 피드백 묶음 5 — 셀 단위 기간 표기(#7) · 연도별 경로와 시나리오(#12)."""
from __future__ import annotations

import inspect

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import comps, scenarios


def _v(value, unit="KRW", as_of="FY2025", label="x"):
    return Value(value, unit, label=label,
                 provenance=Provenance(source="테스트", source_url="",
                                       source_type=SourceType.AUTHORITATIVE, as_of=as_of))


# ── #7 셀 단위 기간 표기 ──────────────────────────────────────────────
def _row_stubs(monkeypatch, *, da_basis="LTM"):
    from engines import market_data as md

    monkeypatch.setattr(md, "resolve", lambda c, m: {
        "name": c, "market": m, "currency": "KRW", "symbol": "X", "native_id": "1"})
    monkeypatch.setattr(md, "market_cap",
                        lambda spec, as_of=None: _v(1000, as_of="20260827"))
    monkeypatch.setattr(md, "net_debt", lambda spec, il=True: _v(-100))
    monkeypatch.setattr(md, "supports_ltm", lambda m: True)

    def ltm(spec, item):
        basis = da_basis if item == "da" else "LTM"
        return _v({"operating_income": 80, "da": 20, "net_income": 60,
                   "revenue": 500}[item], as_of=("LTM~" if basis == "LTM" else "FY2025")), basis

    monkeypatch.setattr(md, "ltm", ltm)
    monkeypatch.setattr(md, "point", lambda spec, item: _v(300))


def test_each_multiple_carries_its_own_basis(monkeypatch):
    """기준은 배수마다 다르다 — 분모가 무엇이냐로 정해진다."""
    _row_stubs(monkeypatch)
    r = comps._row("삼성전자", "KR", None, True)
    mb = r["multiple_basis"]
    assert mb["ev_ebitda"] == "LTM"
    assert mb["per"] == "LTM"
    assert mb["pbr"] == "FY2025", "P/B 분모는 잔액이라 시점 기준이다"


def test_mixed_period_ebitda_is_visible_on_the_cell(monkeypatch):
    """한국 기업은 D&A 가 연간 주석에만 있어 LTM EBIT + FY D&A 가 불가피하다.

    거부하면 그 회사는 EV/EBITDA 를 영영 못 낸다 — 막지 말고 셀에 표시한다.
    """
    _row_stubs(monkeypatch, da_basis="FY")
    r = comps._row("삼성전자", "KR", None, True)
    assert "혼용" in r["multiple_basis"]["ev_ebitda"]
    assert "LTM" in r["multiple_basis"]["ev_ebitda"]
    assert "FY" in r["multiple_basis"]["ev_ebitda"]


def test_balance_sheet_items_get_a_point_in_time_basis(monkeypatch):
    _row_stubs(monkeypatch)
    r = comps._row("삼성전자", "KR", None, True)
    assert r["basis"]["equity"] == "FY2025"
    assert r["basis"]["market_cap"] == "20260827"
    assert r["price_basis"] == "20260827"


def test_multiples_appear_in_extras_with_the_basis_in_the_label(monkeypatch):
    """note 문자열에만 두면 LLM 이 다시 파싱해야 하고 그 과정에서 기준이 떨어진다."""
    _row_stubs(monkeypatch, da_basis="FY")
    m = {"rows": [comps._row("삼성전자", "KR", None, True)], "fx": {},
         "price_date": "20260827"}
    ex = comps._extras(m)
    key = "삼성전자.ev_ebitda"
    assert key in ex
    assert "[" in ex[key].label and "혼용" in ex[key].label
    assert ex[key].provenance.original_field.startswith("basis=")


def test_nm_cells_still_carry_a_basis(monkeypatch):
    """NM 이어도 어느 기준으로 NM 인지 알아야 한다."""
    from engines import market_data as md

    _row_stubs(monkeypatch)
    monkeypatch.setattr(md, "ltm", lambda spec, item: (
        _v(-50 if item == "net_income" else 80), "LTM"))
    m = {"rows": [comps._row("적자회사", "KR", None, True)], "fx": {},
         "price_date": "20260827"}
    ex = comps._extras(m)
    assert ex["적자회사.per"].value is None
    assert ex["적자회사.per"].provenance.original_field == "basis=LTM"


def test_basis_table_is_a_grid_for_rendering(monkeypatch):
    _row_stubs(monkeypatch)
    m = {"rows": [comps._row("삼성전자", "KR", None, True)]}
    t = comps.basis_table(m)
    assert t[0]["company"] == "삼성전자"
    assert t[0]["ev_ebitda"] and t[0]["pbr"]


def test_prompt_requires_the_badge_in_the_same_cell():
    from agent import brain

    p = brain.SYSTEM_PROMPT
    assert "같은 칸" in p
    assert "각주로 몰면" in p


# ── #12 연도별 경로 ───────────────────────────────────────────────────
def _dcf_stubs(monkeypatch):
    from engines import business_mix, dcf as dcf_engine
    from providers import damodaran

    monkeypatch.setattr(dcf_engine.dart, "financial_item",
                        lambda *a, **k: _v(1_000_000, label="테스트 매출액"))
    monkeypatch.setattr(dcf_engine.dart, "shares_outstanding",
                        lambda *a, **k: _v(1_000, "주"))
    monkeypatch.setattr(damodaran, "corporate_tax_rate", lambda c: _v(25.0, "%"))
    monkeypatch.setattr(business_mix, "classify",
                        lambda *a, **k: {"company": "테스트", "kind": "industrial",
                                         "single_dcf_ok": True, "reason": "", "evidence": []})
    monkeypatch.setattr(dcf_engine.reality_check, "market_reference",
                        lambda *a, **k: {"market_cap": None, "price": None,
                                         "as_of": None, "error": "테스트"})


def test_margin_accepts_a_per_year_path(monkeypatch):
    """'초기 둔화 후 정상화' 는 단일값으로 만들 수 없다."""
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, [8, 6, 4, 3, 3], [5, 6, 7, 8, 8],
                               3.0, 4.0, 2.0, 2.0)
    assert m["margin_path"] == [5.0, 6.0, 7.0, 8.0, 8.0]
    assert m["growth_path"] == [8.0, 6.0, 4.0, 3.0, 3.0]
    # 마진이 해마다 올라가면 EBIT/매출 비율도 그래야 한다
    ratios = [r["ebit"] / r["rev"] for r in m["rows"]]
    assert ratios[0] == pytest.approx(0.05)
    assert ratios[-1] == pytest.approx(0.08)


def test_short_margin_vector_is_extended_with_its_last_value(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, [10, 8], 3.0, 4.0, 2.0, 2.0,
                               forecast_years=5)
    assert m["margin_path"] == [10.0, 8.0, 8.0, 8.0, 8.0]


def test_scalar_margin_still_works(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    m = dcf_engine.build_model("테스트", 10.0, 0, 5.0, 12.0, 3.0, 4.0, 2.0, 2.0)
    assert set(m["margin_path"]) == {12.0}


def test_empty_margin_vector_is_rejected(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    with pytest.raises(DataError):
        dcf_engine.build_model("테스트", 10.0, 0, 5.0, [], 3.0, 4.0, 2.0, 2.0)


def test_note_shows_the_path_only_when_it_varies(monkeypatch):
    from engines import dcf as dcf_engine

    _dcf_stubs(monkeypatch)
    flat = dcf_engine.evaluate("테스트", 10.0, 0, 5.0, 12.0, 3.0, 4.0, 2.0, 2.0)
    assert "마진 경로" not in (flat.provenance.note or "")
    fade = dcf_engine.evaluate("테스트", 10.0, 0, [8, 6, 4], [5, 6, 7], 3.0, 4.0, 2.0, 2.0)
    assert "마진 경로" in (fade.provenance.note or "")
    assert "성장 경로" in (fade.provenance.note or "")


# ── #12 시나리오 ──────────────────────────────────────────────────────
_KW = dict(wacc_pct=10.0, net_debt=0, revenue_growth=5.0, ebit_margin_pct=20.0,
           da_pct=3.0, capex_pct=4.0, nwc_pct=2.0, terminal_growth_pct=2.0)


def test_three_cases_are_built_from_deltas(monkeypatch):
    _dcf_stubs(monkeypatch)
    d = scenarios.build("테스트", **_KW)
    assert set(d["cases"]) == {"bear", "base", "bull"}
    assert d["cases"]["bull"]["per_share"] > d["cases"]["base"]["per_share"]
    assert d["cases"]["bear"]["per_share"] < d["cases"]["base"]["per_share"]


def test_deltas_shift_both_scalars_and_vectors(monkeypatch):
    _dcf_stubs(monkeypatch)
    d = scenarios.build("테스트", **{**_KW, "revenue_growth": [8, 6, 4],
                                    "bull_growth_delta_pct": 2.0})
    assert d["cases"]["bull"]["growth_path"] == [10.0, 8.0, 6.0]


def test_only_the_base_case_hits_the_market(monkeypatch):
    """같은 시세를 세 번 조회하지 않는다."""
    src = inspect.getsource(scenarios.build)
    assert 'skip_market_check=(name != "base")' in src


def test_probabilities_are_normalised(monkeypatch):
    _dcf_stubs(monkeypatch)
    d = scenarios.build("테스트", **_KW, probabilities=[1, 2, 1])
    assert sum(d["probabilities"].values()) == pytest.approx(1.0)
    assert d["probabilities"]["base"] == pytest.approx(0.5)


def test_bad_probabilities_are_rejected(monkeypatch):
    _dcf_stubs(monkeypatch)
    with pytest.raises(DataError):
        scenarios.build("테스트", **_KW, probabilities=[0.5, 0.5])
    with pytest.raises(DataError):
        scenarios.build("테스트", **_KW, probabilities=[0, 0, 0])


def test_terminal_growth_delta_cannot_cross_wacc(monkeypatch):
    _dcf_stubs(monkeypatch)
    with pytest.raises(DataError, match="TV"):
        scenarios.build("테스트", **_KW, bull_terminal_growth_delta_pct=9.0)


def test_range_is_the_headline_and_weighting_is_flagged_as_secondary(monkeypatch):
    """확률가중은 세 시나리오를 다시 한 점으로 뭉개는 것이라 결론이 될 수 없다."""
    _dcf_stubs(monkeypatch)
    v = scenarios.evaluate("테스트", **_KW)
    note = v.provenance.note or ""
    assert "범위" in note
    assert "참고용" in note and "결론은 범위로" in note


def test_each_case_is_exposed_as_an_extra(monkeypatch):
    _dcf_stubs(monkeypatch)
    v = scenarios.evaluate("테스트", **_KW)
    assert {"bear_per_share", "base_per_share", "bull_per_share"} <= set(v.extras)


def test_value_is_the_base_case(monkeypatch):
    _dcf_stubs(monkeypatch)
    v = scenarios.evaluate("테스트", **_KW)
    assert v.value == v.extras["base_per_share"].value


def test_scenarios_tool_is_registered():
    from agent import registry

    s = next(x for x in registry.tool_schemas() if x["name"] == "compute_scenarios")
    assert "범위" in s["description"]
    props = s["input_schema"]["properties"]
    assert "bull_growth_delta_pct" in props and "probabilities" in props
    # 성장·마진은 스칼라와 배열을 모두 받아야 한다
    assert "anyOf" in props["revenue_growth_pct"]
    assert "anyOf" in props["ebit_margin_pct"]


def test_compute_dcf_advertises_vectors_too():
    from agent import registry

    props = next(x for x in registry.tool_schemas()
                 if x["name"] == "compute_dcf")["input_schema"]["properties"]
    assert "anyOf" in props["revenue_growth_pct"]
    assert "anyOf" in props["ebit_margin_pct"]


def test_prompt_routes_range_questions_to_scenarios():
    from agent import brain

    p = brain.SYSTEM_PROMPT
    assert "compute_scenarios" in p
    assert "범위가 본체" in p


def test_prompt_stays_within_the_size_budget():
    """프롬프트가 커지면 tool-calling 정확도가 떨어진다 — 늘릴 땐 중복을 먼저 지운다."""
    from agent import brain

    assert len(brain._system_prompt()) < 14_000

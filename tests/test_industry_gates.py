"""산업별 예외 게이트 회귀 테스트 — IB 평가 보고서(2026-08-27)의 P0/P1/P2 결함 대응.

여기 있는 테스트는 전부 **실측에서 실제로 틀렸던 것**에 대응한다. 네트워크는 타지 않고
provider 경계를 스텁한다(conftest 가 키를 비우므로 진짜 호출은 어차피 실패한다).
"""
from __future__ import annotations

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import business_mix, dcf as dcf_engine, dcf_inputs
from providers import dart


def _v(value, unit="KRW", label="x", as_of=None):
    return Value(value, unit, label=label,
                 provenance=Provenance(source="테스트", source_type=SourceType.AUTHORITATIVE,
                                       source_url="", as_of=as_of))


# ── P0-4: 엔티티 리졸버 ────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expect", [
    ("현대자동차(005380)", ("현대자동차", "005380")),
    ("현대자동차（005380）", ("현대자동차", "005380")),   # 전각 괄호
    ("005380", ("", "005380")),
    ("005380.KS", ("", "005380")),
    ("005380.kq", ("", "005380")),
    ("삼성전자", ("삼성전자", None)),
    ("삼성전자(반도체)", ("삼성전자", None)),            # 종목코드가 아닌 괄호도 떼어낸다
    ("  현대차  ", ("현대차", None)),
])
def test_parse_identifier(raw, expect):
    """실측 실패: '현대자동차(005380)' 이 못 찾음으로 떨어졌다.

    _norm_name 이 괄호 기호만 지우고 숫자는 남겨 '현대자동차005380' 토큰이 만들어져
    부분일치 길이 게이트(>=9자)와 fuzzy cutoff(0.78) 두 곳에 동시에 막혔다.
    """
    assert dart.parse_identifier(raw) == expect


def test_resolve_prefers_stock_code_over_name(monkeypatch):
    """괄호 안 종목코드는 가장 강한 신호 — 이름보다 먼저 쓴다."""
    target = {"corp_code": "00164742", "corp_name": "현대자동차", "stock_code": "005380"}
    monkeypatch.setattr(dart, "_corp_index",
                        lambda: ({}, {"005380": target}, {"현대자동차": [target]}))
    assert dart.resolve("현대자동차(005380)") is target
    assert dart.resolve("005380.KS") is target


def test_resolve_indexes_english_names(monkeypatch):
    """corpCode.xml 의 corp_eng_name 도 색인해야 'Hyundai Motor' 가 잡힌다."""
    entry = {"corp_code": "1", "corp_name": "현대자동차",
             "corp_eng_name": "HYUNDAI MOTOR COMPANY", "stock_code": "005380"}
    norm = dart._norm_name(entry["corp_eng_name"])
    assert norm and norm != dart._norm_name(entry["corp_name"])


# ── P0-1: 사업부문 3분류 게이트 ────────────────────────────────────────
def _stub_mix(monkeypatch, *, finance_assets=(), total_assets=1000,
              has_inventory=True, finance_revenue=(), operating_revenue=(),
              section=None, subs=()):
    def fake(company, year, prefer, deep):
        return ("테스트회사", 2025, total_assets, has_inventory,
                tuple(finance_assets), tuple(finance_revenue), tuple(operating_revenue),
                section, tuple(subs))

    business_mix._cached.cache_clear()
    monkeypatch.setattr(business_mix, "_cached", fake)


def test_manufacturer_is_industrial(monkeypatch):
    _stub_mix(monkeypatch, operating_revenue=[("매출액", 900)])
    d = business_mix.classify("삼성전자")
    assert d["kind"] == "industrial" and d["single_dcf_ok"]


def test_material_finance_assets_alone_make_it_mixed(monkeypatch):
    """2-of-3 만 쓰면 현대자동차를 놓친다 — 신호는 1개인데 금융업 자산이 총자산의 51%였다."""
    _stub_mix(monkeypatch, finance_assets=[("금융업채권", 510)], total_assets=1000,
              operating_revenue=[("매출액", 900)])
    d = business_mix.classify("현대자동차")
    assert d["kind"] == "mixed" and not d["single_dcf_ok"]
    assert d["hits"] == 1, "신호 1개만으로도 중요성 기준으로 잡아야 한다"
    assert "51%" in d["reason"]


def test_immaterial_lease_receivable_does_not_trigger(monkeypatch):
    """제조업의 소액 리스채권 한 줄로 SOTP 로 보내면 오탐이다."""
    _stub_mix(monkeypatch, finance_assets=[("금융리스채권", 20)], total_assets=1000,
              operating_revenue=[("매출액", 900)])
    assert business_mix.classify("어떤제조사")["kind"] == "industrial"


def test_two_weak_signals_are_enough(monkeypatch):
    _stub_mix(monkeypatch, finance_assets=[("할부금융자산", 20)], total_assets=1000,
              operating_revenue=[("매출액", 900)], section="(금융업)")
    assert business_mix.classify("어떤회사")["kind"] == "mixed"


def test_pure_financial_company_is_detected(monkeypatch):
    """삼성카드 실측: 재고자산 행이 없고 IS 가 이자수익·수수료수익으로 구성."""
    _stub_mix(monkeypatch, has_inventory=False,
              finance_revenue=[("이자수익", 2028), ("수수료수익", 1813)],
              finance_assets=[("금융리스채권", 31)], total_assets=30000)
    d = business_mix.classify("삼성카드")
    assert d["kind"] == "financial" and not d["single_dcf_ok"]
    assert "FCFF" in d["reason"]


def test_other_operating_revenue_does_not_hide_a_financial_company(monkeypatch):
    """'기타영업수익' 이 '영업수익' 부분문자열에 걸려 금융회사가 제조업으로 분류됐다."""
    _stub_mix(monkeypatch, has_inventory=False,
              finance_revenue=[("이자수익", 5490)],
              operating_revenue=[("기타영업수익", 92)], total_assets=30000)
    # 정확일치라 '기타영업수익' 은 주 매출로 잡히지 않는다 → financial
    assert business_mix.classify("삼성카드")["kind"] == "financial"


def test_finance_debt_split_is_honest_when_it_cannot_split(monkeypatch):
    """계정명으로 못 가르면 **추정으로 쪼개지 않는다**(현대자동차 사채 106.9조)."""
    rows = [{"sj_div": "BS", "account_nm": "사채", "thstrm_amount": "106,900,459"},
            {"sj_div": "BS", "account_nm": "장기차입금", "thstrm_amount": "24,340,003"}]
    monkeypatch.setattr(business_mix.dart, "resolve",
                        lambda c: {"corp_code": "1", "corp_name": "현대자동차"})
    monkeypatch.setattr(business_mix.dart, "_latest_year", lambda *a, **k: 2025)
    monkeypatch.setattr(business_mix.dart, "_statement_rows",
                        lambda *a, **k: (rows, "연결(CFS)"))
    d = business_mix.split_finance_debt("현대자동차")
    assert d["confident"] is False
    assert d["finance"] == 0 and d["industrial"] == d["total"]
    assert "세그먼트" in d["basis"]


# ── P0-1 소비처: compute_dcf 차단 ──────────────────────────────────────
def _dcf_stubs(monkeypatch, kind="industrial"):
    from providers import damodaran

    monkeypatch.setattr(dcf_engine.dart, "financial_item",
                        lambda *a, **k: _v(300_000_000, label="테스트 매출액", as_of="FY2025"))
    monkeypatch.setattr(dcf_engine.dart, "shares_outstanding",
                        lambda *a, **k: _v(1_000_000, "주", as_of="FY2025"))
    monkeypatch.setattr(damodaran, "corporate_tax_rate",
                        lambda c: _v(26.4, "%", as_of="2026-01-01"))
    monkeypatch.setattr(business_mix, "classify",
                        lambda *a, **k: {"company": "테스트회사", "kind": kind,
                                         "single_dcf_ok": kind == "industrial",
                                         "reason": f"{kind} 판정", "evidence": []})


def test_dcf_blocks_captive_finance(monkeypatch):
    """현대자동차류: 단일 DCF 를 값으로 내지 않고 SOTP 로 안내한다."""
    _dcf_stubs(monkeypatch, "mixed")
    with pytest.raises(DataError) as e:
        dcf_engine.evaluate("현대자동차", 8.0, 1e12, 5.0, 6.0, 5.0, 5.0, 16.0, 2.0)
    msg = str(e.value)
    assert "SOTP" in msg and "allow_mixed" in msg


def test_dcf_blocks_pure_financial_with_equity_based_alternative(monkeypatch):
    _dcf_stubs(monkeypatch, "financial")
    with pytest.raises(DataError) as e:
        dcf_engine.evaluate("삼성카드", 8.0, 0, 5.0, 20.0, 3.0, 3.0, 5.0, 2.0)
    assert "P/B" in str(e.value)


def test_dcf_allows_industrial(monkeypatch):
    _dcf_stubs(monkeypatch, "industrial")
    v = dcf_engine.evaluate("삼성전자", 9.0, -1e12, 8.0, 15.0, 14.0, 12.0, 16.5, 2.0)
    assert v.value is not None and v.value > 0


def test_allow_mixed_override_carries_a_warning(monkeypatch):
    """우회를 허용하되 결과가 그대로 인용되지 않게 경고를 붙인다."""
    _dcf_stubs(monkeypatch, "mixed")
    v = dcf_engine.evaluate("현대자동차", 9.0, -1e12, 8.0, 15.0, 14.0, 12.0, 16.0, 2.0,
                            allow_mixed=True)
    assert "allow_mixed" in (v.provenance.note or "")


# ── P2: 엔진 봉인(NM) ─────────────────────────────────────────────────
def test_negative_equity_value_is_sealed_as_nm(monkeypatch):
    """예전에는 −5,042,055원/주 같은 값을 그대로 내보내고 LLM 이 사후에 걸렀다."""
    _dcf_stubs(monkeypatch, "industrial")
    v = dcf_engine.evaluate("테스트", 9.0, 1e15, 5.0, 3.0, 3.0, 40.0, 20.0, 2.0)
    assert v.value is None, "봉인되면 주당가치를 숫자로 내보내지 않는다"
    assert "NM" in (v.label or "") and "산출 불가" in (v.provenance.note or "")
    assert v.extras["enterprise_value"].value is not None, "EV 는 진단용으로 남긴다"


def test_high_tv_share_warns_but_still_returns_a_value(monkeypatch):
    """TV 비중 과다는 봉인 사유가 아니라 경고다 — 값은 주고 지배 사실을 알린다."""
    _dcf_stubs(monkeypatch, "industrial")
    v = dcf_engine.evaluate("테스트", 5.0, 0, 3.0, 10.0, 5.0, 5.0, 5.0, 4.5)
    assert v.value is not None
    assert "Terminal Value" in (v.provenance.note or "")


# ── P1-3: 기준일 요약이 note 앞에 온다 ─────────────────────────────────
def test_asof_summary_leads_the_note(monkeypatch):
    """앞자리는 '해석을 바꾸는 정보' 의 몫이다.

    순서는 시장·구조 대조 → 기준일 → 본문 → run 각인. 시장 대조가 첫 자리인 이유는
    "이 값이 시장과 얼마나 다른가" 가 결론을 읽기 전에 알아야 하는 정보이기 때문이다.
    """
    _dcf_stubs(monkeypatch, "industrial")
    v = dcf_engine.evaluate("삼성전자", 9.0, -1e12, 8.0, 15.0, 14.0, 12.0, 16.5, 2.0)
    note = v.provenance.note or ""
    assert "[시장·구조 대조]" in note[:120], "시장 대조는 맨 앞에 있어야 한다"
    body = note.index("[DCF")
    assert 0 < note.index("[기준일]") < body, "기준일 요약은 본문보다 앞이어야 한다"
    assert "기준일이 서로 다릅니다" in note, "FY 재무 vs 연간 데이터셋 불일치를 알려야 한다"


# ── P0-2: ΔNWC 안전장치 ───────────────────────────────────────────────
def _ratio_stubs(monkeypatch, *, cf_nwc, revs, wc=None, narrow=False):
    """historical_ratios 를 네트워크 없이 돌린다."""
    years = sorted(revs, reverse=True)

    def nyear(company, item, n=5, year=None, report="annual", prefer="CFS"):
        src = {"revenue": revs, "trade_receivables": (wc or {}),
               "inventories": {y: 0 for y in years}, "trade_payables": {y: 0 for y in years},
               "operating_income": {y: revs[y] * 0.1 for y in years}}[item]
        return {"corp_name": "테스트회사", "stock_code": "000000", "item": item,
                "series": [{"year": y, "amount": src[y]}
                           for y in years if src.get(y) is not None]}

    monkeypatch.setattr(dcf_inputs.dart, "financial_item_nyear", nyear)
    monkeypatch.setattr(dcf_inputs.dart, "cf_extras_nyear", lambda *a, **k: {
        "corp_name": "테스트회사",
        "capex": [{"year": y, "amount": revs[y] * 0.05} for y in years],
        "capex_intangible": [], "da": [{"year": y, "amount": revs[y] * 0.06} for y in years],
        "nwc_change": [{"year": y, "amount": cf_nwc.get(y)} for y in years],
        "ocf": [], "interest": [],
    })
    monkeypatch.setattr(dcf_inputs.dart, "da_best", lambda *a, **k: _v(1))
    monkeypatch.setattr(business_mix, "classify",
                        lambda *a, **k: {"single_dcf_ok": not narrow, "kind":
                                         "mixed" if narrow else "industrial"})


def test_nwc_uses_the_balance_sheet_level_not_cash_flow_deltas(monkeypatch):
    """운전자본은 **수준(level)** 으로 잡는다.

    예전 1차 경로는 현금흐름표 '자산부채의 변동' 증감이었는데 두 가지가 동시에 잘못됐다:
    집계에 금융업채권이 섞이고(현대차 161.51%), Δ매출이 작은 해에 분모가 0 에 가까워져
    비율이 폭발한다(−73.43%). 수준 비율은 분모가 매출이라 그런 일이 없다.
    """
    revs = {2025: 1000.0, 2024: 900.0, 2023: 800.0}
    cf_nwc = {2025: -1000.0, 2024: -900.0}          # 오염된 집계 — 1차로 쓰이면 안 된다
    wc = {2025: 120.0, 2024: 100.0, 2023: 90.0}     # 좁은 정의(AR+재고−AP)
    _ratio_stubs(monkeypatch, cf_nwc=cf_nwc, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("현대자동차", 3)
    assert r["nwc_basis"] == "level"
    # 수준 비율: 120/1000=12%, 100/900=11.1%, 90/800=11.25% → median 11.25%
    assert r["nwc_pct"].value == pytest.approx(11.25, abs=0.01)
    assert r["nwc_pct"].value < 100, "오염된 집계(-1000/100=1000%)가 쓰이면 안 된다"


def test_level_ratio_does_not_explode_on_a_flat_revenue_year(monkeypatch):
    """Δ매출이 0.5% 인 해가 있어도 수준 비율은 멀쩡하다 — 예전엔 여기서 폭발했다."""
    revs = {2025: 1000.0, 2024: 995.0, 2023: 800.0}
    wc = {2025: 200.0, 2024: 100.0, 2023: 90.0}
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 3)
    # 20.0%, 10.05%, 11.25% → median 11.25%. 증감 방식이면 (200-100)/(1000-995)=2000%.
    assert r["nwc_pct"].value == pytest.approx(11.25, abs=0.05)
    assert abs(r["nwc_pct"].value) < 50


def test_median_not_mean_so_one_outlier_cannot_dominate(monkeypatch):
    revs = {2025: 1000.0, 2024: 900.0, 2023: 800.0, 2022: 700.0}
    wc = {2025: 400.0, 2024: 110.0, 2023: 100.0, 2022: 90.0}   # 2025 가 이상치
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 4)
    # 40.0, 12.2, 12.5, 12.9 → median 12.7 (평균이면 19.4 로 이상치에 끌려간다)
    assert r["nwc_pct"].value == pytest.approx(12.7, abs=0.2)
    assert r["nwc_pct"].value < 19.0, "산술평균이면 이상치가 결과를 지배한다"


def test_extreme_nwc_raises_confirmation_flag(monkeypatch):
    revs = {2025: 1000.0, 2024: 900.0, 2023: 800.0}
    wc = {2025: 500.0, 2024: 450.0, 2023: 400.0}     # NWC/매출 50%
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 3)
    assert r["nwc_needs_confirmation"], "통상 범위를 넘으면 자동 채택하지 않는다"
    assert "확인" in r["nwc_pct"].provenance.note


def test_unstable_turnover_raises_a_flag(monkeypatch):
    """회전율이 유지된다는 전제가 이 방식의 근거다 — 편차가 크면 전제가 약하다."""
    revs = {2025: 1000.0, 2024: 900.0, 2023: 800.0}
    wc = {2025: 50.0, 2024: 200.0, 2023: 60.0}       # 5% ~ 22%
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 3)
    assert r["nwc_needs_confirmation"], "연도별 편차가 크면 확인을 요구해야 한다"
    assert "편차" in r["nwc_needs_confirmation"]


def test_turnover_days_are_reported(monkeypatch):
    """비율보다 회전일수가 실무에서 읽기 쉽고 이상치를 눈으로 잡을 수 있다."""
    revs = {2025: 1000.0, 2024: 900.0}
    wc = {2025: 120.0, 2024: 100.0}
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 2)
    note = r["nwc_pct"].provenance.note
    assert "DSO" in note and "DIO" in note and "DPO" in note


def test_cash_flow_method_is_kept_as_a_cross_check(monkeypatch):
    """증감 방식을 버리지는 않는다 — 교차검증으로 남겨 두 값을 나란히 보여준다."""
    revs = {2025: 1000.0, 2024: 900.0, 2023: 800.0}
    cf_nwc = {2025: -11.0, 2024: -10.0}
    wc = {2025: 120.0, 2024: 100.0, 2023: 90.0}
    _ratio_stubs(monkeypatch, cf_nwc=cf_nwc, revs=revs, wc=wc, narrow=False)
    r = dcf_inputs.historical_ratios("테스트", 3)
    assert "교차검증" in r["nwc_pct"].provenance.note


def test_per_year_levels_are_exposed(monkeypatch):
    revs = {2025: 1000.0, 2024: 900.0}
    wc = {2025: 120.0, 2024: 100.0}
    _ratio_stubs(monkeypatch, cf_nwc={}, revs=revs, wc=wc, narrow=True)
    r = dcf_inputs.historical_ratios("테스트", 2)
    lv = r["nwc_levels"]
    assert [x["year"] for x in lv] == [2025, 2024]
    assert lv[0]["ratio_pct"] == pytest.approx(12.0)
    assert lv[0]["nwc"] == pytest.approx(120.0)

def test_market_kd_flags_the_risk_free_inversion(monkeypatch):
    """실효 Kd < Rf 는 오류가 아니라 과거 저금리 조달분 때문 — 그 사실을 말해야 한다."""
    from providers import ecos

    monkeypatch.setattr(ecos, "corporate_bond_yield",
                        lambda rating="AA-": _v(4.5, "%", as_of="2026-08-26"))
    monkeypatch.setattr(ecos, "risk_free_rate", lambda tenor="10Y": _v(3.816, "%"))
    monkeypatch.setattr(dcf_inputs.dart, "financial_item", lambda *a, **k: _v(50.0))
    monkeypatch.setattr(dcf_inputs.dart, "cf_extras",
                        lambda *a, **k: {"interest": _v(1.0)})
    monkeypatch.setattr(dcf_inputs, "cost_of_debt", lambda *a, **k: _v(3.79, "%"))

    v = dcf_inputs.market_cost_of_debt("SK하이닉스")
    assert v.value == 4.5
    note = v.provenance.note
    assert "무위험수익률" in note and "신용스프레드가 음수라는 뜻이 아닙니다" in note
    assert "이자보상배율" in note


def test_market_kd_picks_bbb_for_weak_coverage(monkeypatch):
    from providers import ecos

    seen = {}
    def _yield(rating="AA-"):
        seen["rating"] = rating
        return _v(10.3, "%")

    monkeypatch.setattr(ecos, "corporate_bond_yield", _yield)
    monkeypatch.setattr(ecos, "risk_free_rate", lambda tenor="10Y": _v(3.816, "%"))
    monkeypatch.setattr(dcf_inputs.dart, "financial_item", lambda *a, **k: _v(2.0))
    monkeypatch.setattr(dcf_inputs.dart, "cf_extras", lambda *a, **k: {"interest": _v(1.0)})
    monkeypatch.setattr(dcf_inputs, "cost_of_debt",
                        lambda *a, **k: (_ for _ in ()).throw(DataError("없음")))

    dcf_inputs.market_cost_of_debt("취약회사")
    assert seen["rating"] == "BBB-", "이자보상배율 2배는 투자적격 경계(5배) 미달"


def test_market_kd_is_korea_only():
    with pytest.raises(DataError, match="한국"):
        dcf_inputs.market_cost_of_debt("Micron", country="US")


# ── P2: 베타 R² 게이팅 ────────────────────────────────────────────────
def test_low_r2_switches_to_industry_beta(monkeypatch):
    """R² < 0.3 이면 회귀베타를 자본비용에 쓰지 않는다."""
    from engines import beta as beta_engine

    reg = _v(1.9, "배", label="회귀베타")
    reg.extras = {"r_squared": _v(0.12, "")}
    monkeypatch.setattr(beta_engine, "regression_beta", lambda *a, **k: reg)
    monkeypatch.setattr(beta_engine, "industry_beta",
                        lambda industry, country=None, **k: _v(1.3, "배", label="산업베타"))

    v = beta_engine.beta_for("어떤회사", industry="Semiconductor")
    assert v.value == 1.3
    assert "산업베타로 전환" in v.provenance.note
    assert v.extras["regression_beta_rejected"].value == 1.9


def test_low_r2_without_industry_returns_value_with_loud_warning(monkeypatch):
    from engines import beta as beta_engine

    reg = _v(1.9, "배", label="회귀베타")
    reg.extras = {"r_squared": _v(0.12, "")}
    monkeypatch.setattr(beta_engine, "regression_beta", lambda *a, **k: reg)
    v = beta_engine.beta_for("어떤회사")
    assert v.value == 1.9
    assert "[저신뢰]" in v.provenance.note

def test_acceptable_r2_keeps_the_regression_beta(monkeypatch):
    from engines import beta as beta_engine

    reg = _v(1.65, "배", label="회귀베타")
    reg.extras = {"r_squared": _v(0.56, "")}
    monkeypatch.setattr(beta_engine, "regression_beta", lambda *a, **k: reg)
    monkeypatch.setattr(beta_engine, "industry_beta", lambda *a, **k: pytest.fail(
        "R² 0.56 은 충분한데 산업베타로 갈아탔다"))
    assert beta_engine.beta_for("SK하이닉스", industry="Semiconductor").value == 1.65


# ── get_beta 는 KOSPI 로 고정하지 않고 실제 상장 거래소를 판별해야 한다 ──────────
# 실측: 리노공업(KOSDAQ)을 KOSPI 와 회귀하면 R² 0.270(저신뢰) < KOSDAQ 대비 0.504.
def test_get_beta_detects_kosdaq_instead_of_hardcoding_kospi(monkeypatch):
    from agent import registry

    monkeypatch.setattr(registry.dart, "resolve",
                        lambda c: {"corp_name": "리노공업", "stock_code": "058470"})
    monkeypatch.setattr(registry.naver, "exchange_for", lambda code: "KOSDAQ")
    seen = {}

    def fake_beta_for(company, industry, country, period, years, index, market, symbol):
        seen["index"] = index
        return _v(1.0, "배", label="베타")

    monkeypatch.setattr(registry.beta_engine, "beta_for", fake_beta_for)
    registry._beta("리노공업")
    assert seen["index"] == "KOSDAQ"


def test_get_beta_falls_back_to_kospi_when_exchange_lookup_fails(monkeypatch):
    """실패는 조용히 넘어가지 않는다 — note 에 사유가 남아야 다음에 왜 KOSPI 로 폴백했는지
    (네트워크 문제인지, 코드 버그인지) 배포 환경에서도 재현 없이 알 수 있다."""
    from agent import registry

    monkeypatch.setattr(registry.dart, "resolve",
                        lambda c: {"corp_name": "삼성전자", "stock_code": "005930"})
    monkeypatch.setattr(registry.naver, "exchange_for",
                        lambda code: (_ for _ in ()).throw(RuntimeError("네트워크 오류")))
    seen = {}

    def fake_beta_for(company, industry, country, period, years, index, market, symbol):
        seen["index"] = index
        return _v(1.0, "배", label="베타")

    monkeypatch.setattr(registry.beta_engine, "beta_for", fake_beta_for)
    v = registry._beta("삼성전자")
    assert seen["index"] == "KOSPI", "거래소 판별이 실패해도 베타 조회 자체는 막지 않는다"
    assert "자동판별 실패" in v.provenance.note
    assert "네트워크 오류" in v.provenance.note


def test_get_beta_skips_exchange_lookup_for_overseas(monkeypatch):
    from agent import registry

    monkeypatch.setattr(registry.dart, "resolve", lambda c: pytest.fail(
        "해외 종목(symbol 지정)인데 DART 를 조회했다"))
    monkeypatch.setattr(registry.naver, "exchange_for", lambda code: pytest.fail(
        "해외 종목인데 네이버 거래소 조회를 탔다"))
    seen = {}

    def fake_beta_for(company, industry, country, period, years, index, market, symbol):
        seen["index"] = index
        return _v(1.0, "배", label="베타")

    monkeypatch.setattr(registry.beta_engine, "beta_for", fake_beta_for)
    registry._beta("Apple", country="US", market="US", symbol="AAPL")
    assert seen["index"] == "KOSPI", "해외 시장에서는 지수 파라미터를 쓰지 않으므로 값이 무의미하다"


# ── P1-6: 상증법 법령 판정 ────────────────────────────────────────────
def _sang_stubs(monkeypatch, *, ni_series, equity, shares, mix):
    from engines import sangjeung

    monkeypatch.setattr(sangjeung.dart, "financial_item_multiyear", lambda *a, **k: {
        "corp_name": "테스트회사", "stock_code": "000000", "fs_label": "별도(OFS)",
        "rcept": "20260101000001", "filing_date": "2026-01-01",
        "series": ni_series} if ni_series else (_ for _ in ()).throw(DataError("3개년 없음")))
    monkeypatch.setattr(sangjeung.dart, "financial_item",
                        lambda *a, **k: _v(equity, label="테스트회사 자본총계", as_of="FY2025"))
    monkeypatch.setattr(sangjeung.dart, "shares_outstanding",
                        lambda *a, **k: _v(shares, "주"))
    monkeypatch.setattr(sangjeung, "_asset_mix", lambda *a, **k: mix)
    return sangjeung


_MIX_PLAIN = {"year": 2025, "fs_label": "별도(OFS)", "total_assets": 1000,
              "real_estate": 0, "stock": 20, "real_estate_rows": [], "stock_rows": [],
              "ratio_real_estate": 0.0, "ratio_stock": 0.02, "ratio_combined": 0.02}


def test_real_estate_heavy_flips_the_weights(monkeypatch):
    """부동산 비중 50% 이상이면 가중치가 3:2 → 2:3 으로 뒤집힌다(상증령 §54①)."""
    mix = dict(_MIX_PLAIN, real_estate=600, ratio_real_estate=0.60, ratio_combined=0.62)
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 100},
                                            {"period": "FY2024", "amount": 100},
                                            {"period": "FY2023", "amount": 100}],
                    equity=1000, shares=100, mix=mix)
    m = s.build_model("부동산회사")
    assert (m["w_income"], m["w_asset"]) == (2, 3)
    assert m["real_estate_heavy"] and m["real_estate_heavy_auto"]


def test_normal_company_keeps_3_to_2(monkeypatch):
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 100},
                                            {"period": "FY2024", "amount": 100},
                                            {"period": "FY2023", "amount": 100}],
                    equity=1000, shares=100, mix=_MIX_PLAIN)
    m = s.build_model("제조회사")
    assert (m["w_income"], m["w_asset"]) == (3, 2)


def test_short_history_becomes_nav_only_instead_of_an_error(monkeypatch):
    """사업개시 3년 미만은 계산 실패가 아니라 **순자산가치 단독평가 사유**다."""
    s = _sang_stubs(monkeypatch, ni_series=None, equity=1000, shares=100, mix=_MIX_PLAIN)
    m = s.build_model("신설회사")
    assert m["nav_only"] and any("3개년" in r for r in m["nav_only_reasons"])
    assert m["results"]["value"] == pytest.approx(10.0)   # 순자산 1000/100


def test_asset_heavy_company_is_nav_only(monkeypatch):
    """부동산·주식이 자산의 80% 이상이면 순자산가치 단독평가(상증령 §54④)."""
    mix = dict(_MIX_PLAIN, real_estate=700, stock=150,
               ratio_real_estate=0.70, ratio_stock=0.15, ratio_combined=0.85)
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 1000},
                                            {"period": "FY2024", "amount": 1000},
                                            {"period": "FY2023", "amount": 1000}],
                    equity=1000, shares=100, mix=mix)
    m = s.build_model("자산보유회사")
    assert m["nav_only"] and any("80%" in r or "85%" in r for r in m["nav_only_reasons"])
    assert m["results"]["value"] == pytest.approx(10.0), "순손익가치를 섞지 않는다"


def test_control_premium_applies_and_sme_is_exempt(monkeypatch):
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 100},
                                            {"period": "FY2024", "amount": 100},
                                            {"period": "FY2023", "amount": 100}],
                    equity=1000, shares=100, mix=_MIX_PLAIN)
    base = s.build_model("회사")["results"]["value"]
    with_premium = s.build_model("회사", largest_shareholder=True)["results"]
    assert with_premium["value"] == pytest.approx(base * 1.2)
    assert with_premium["value_before_premium"] == pytest.approx(base)

    sme = s.build_model("회사", largest_shareholder=True, sme=True)["results"]
    assert sme["value"] == pytest.approx(base), "중소기업은 할증 제외"


def test_explicit_flag_overrides_auto_detection(monkeypatch):
    mix = dict(_MIX_PLAIN, real_estate=600, ratio_real_estate=0.60, ratio_combined=0.62)
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 100},
                                            {"period": "FY2024", "amount": 100},
                                            {"period": "FY2023", "amount": 100}],
                    equity=1000, shares=100, mix=mix)
    m = s.build_model("회사", real_estate_heavy=False)
    assert (m["w_income"], m["w_asset"]) == (3, 2)
    assert m["real_estate_heavy_explicit"]


def test_evaluate_surfaces_the_legal_judgment(monkeypatch):
    mix = dict(_MIX_PLAIN, real_estate=600, ratio_real_estate=0.60, ratio_combined=0.62)
    s = _sang_stubs(monkeypatch, ni_series=[{"period": "FY2025", "amount": 100},
                                            {"period": "FY2024", "amount": 100},
                                            {"period": "FY2023", "amount": 100}],
                    equity=1000, shares=100, mix=mix)
    v = s.evaluate("부동산회사", largest_shareholder=True)
    note = v.provenance.note
    assert "[법령판정]" in note
    assert "가중치 2:3" in note and "부동산과다보유법인" in note
    assert "최대주주 할증 20%" in note
    assert "영업권 가산" in note, "미반영 한계도 함께 밝혀야 한다"


# ── P1-4: EDINET 연결 우선 ────────────────────────────────────────────
def test_edinet_matches_company_extension_namespace():
    """IFRS 채택 일본기업은 핵심 손익을 회사별 확장 태그로 낸다.

    실측(Toyota S100Y8NY): 연결 매출 50.68조엔은
    jpcrp030000-asr_E02144-000:OperatingRevenuesIFRSKeyFinancialData 에 있고,
    고정 태그 목록으로 잡히던 jpcrp_cor:NetSalesSummaryOfBusinessResults 는 개별 18.26조엔.
    """
    from providers import edinet

    rows = (
        {"tag": "jpcrp030000-asr_E02144-000:OperatingRevenuesIFRSKeyFinancialData",
         "ctx": "CurrentYearDuration", "relyear": "当期", "val": 50_684_952_000_000},
        {"tag": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
         "ctx": "CurrentYearDuration_NonConsolidatedMember", "relyear": "当期",
         "val": 18_259_979_000_000},
    )
    row, tag, basis = edinet._find_value(rows, edinet.ITEM_MAP["revenue"][1], "duration")
    assert row["val"] == 50_684_952_000_000
    assert basis == "연결"
    assert "OperatingRevenuesIFRSKeyFinancialData" in tag


def test_edinet_prefers_consolidated_over_tag_priority():
    """태그 우선순위보다 연결 여부가 먼저다."""
    from providers import edinet

    rows = (
        {"tag": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
         "ctx": "CurrentYearDuration_NonConsolidatedMember", "relyear": "当期", "val": 100},
        {"tag": "x:RevenueIFRS", "ctx": "CurrentYearDuration", "relyear": "当期", "val": 900},
    )
    row, _, basis = edinet._find_value(rows, edinet.ITEM_MAP["revenue"][1], "duration")
    assert row["val"] == 900 and basis == "연결"


def test_edinet_segment_contexts_are_excluded():
    """세그먼트 컨텍스트를 연결 총계로 오인하면 안 된다."""
    from providers import edinet

    rows = (
        {"tag": "x:SalesRevenuesIFRS",
         "ctx": "CurrentYearDuration_AutomotiveReportableSegmentMember",
         "relyear": "当期", "val": 43_199_865_000_000},
        {"tag": "x:SalesRevenuesIFRS", "ctx": "CurrentYearDuration", "relyear": "当期",
         "val": 48_036_704_000_000},
    )
    row, _, _ = edinet._find_value(rows, edinet.ITEM_MAP["revenue"][1], "duration")
    assert row["val"] == 48_036_704_000_000


def test_summary_tags_are_last_resort():
    """SummaryOfBusinessResults 계열은 개별로만 태깅되는 경우가 많아 맨 뒤여야 한다."""
    from providers import edinet

    order = edinet.ITEM_MAP["revenue"][1]
    assert order.index("NetSalesSummaryOfBusinessResults") > order.index("RevenueIFRS")


# ── P1-5: 대만 2차 출처 표기 ──────────────────────────────────────────
def test_taiwan_provenance_says_it_is_not_parsed_source(monkeypatch):
    from providers import finmind

    monkeypatch.setattr(finmind, "resolve",
                        lambda c: {"stock_id": "2330", "stock_name": "台積電"})
    monkeypatch.setattr(finmind, "_latest_complete_year", lambda *a, **k: 2025)
    monkeypatch.setattr(finmind, "_sum_income", lambda *a, **k: (100.0, "Revenue", 4))
    v = finmind.financial_item("2330", "revenue")
    assert "mopsov.twse.com.tw" in v.provenance.source_url
    assert "원문 탐색 진입점" in v.provenance.note
    assert v.provenance.source_type == SourceType.REFERENCE


def test_taiwan_incomplete_year_is_flagged_not_called_annual(monkeypatch):
    from providers import finmind

    monkeypatch.setattr(finmind, "resolve",
                        lambda c: {"stock_id": "2330", "stock_name": "台積電"})
    monkeypatch.setattr(finmind, "_latest_complete_year", lambda *a, **k: 2026)
    monkeypatch.setattr(finmind, "_sum_income", lambda *a, **k: (50.0, "Revenue", 2))
    v = finmind.financial_item("2330", "revenue")
    assert "연간 미완성" in v.provenance.note
    assert "연간이 아니다" in v.provenance.note

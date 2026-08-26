"""DCF 입력 자동 도출 회귀 테스트 — 네트워크 없이 순수 로직만 검증.

여기서 잡으려는 사고는 "조용히 틀린 밸류에이션":
  · ΔNWC 부호를 반대로 잡기 (현금흐름표의 자산부채 변동은 **현금 영향**이라 부호 반전 필요)
  · D&A 에 대손상각비가 섞여 들어가기 (계정명 문자열 매칭의 함정)
  · Kd 를 손익 '금융비용' 으로 계산하기 (환차손 포함 → 삼성전자 실측 48.8%)
  · CAPEX 에서 무형자산 취득을 빼먹기 (D&A 에는 무형자산상각비가 들어있으므로 비대칭)
"""
from __future__ import annotations

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import beta as beta_engine, dcf_inputs
from providers import dart


def _v(amount, label="x", as_of="FY2025"):
    return Value(amount, "KRW", label=label,
                 provenance=Provenance(source="DART (금융감독원)",
                                       source_type=SourceType.AUTHORITATIVE,
                                       source_url="https://dart.fss.or.kr", as_of=as_of))


# ── D&A 계정 선별 (대손상각비 오염 방지) ──────────────────────────
def test_da_rows_prefers_tags_and_excludes_bad_debt():
    """오리온 실측 케이스: '대손상각비 조정' 이 D&A 에 섞이면 5.9억이 과대계상된다."""
    rows = [
        {"account_id": "ifrs-full_AdjustmentsForDepreciationExpense",
         "account_nm": "감가상각비에 대한 조정"},
        {"account_id": "ifrs-full_AdjustmentsForAmortisationExpense",
         "account_nm": "무형자산상각비에 대한 조정"},
        {"account_id": "dart_AdjustmentsForBadDebtExpenses", "account_nm": "대손상각비 조정"},
        {"account_id": "dart_AdjustmentsForOtherBadDebtExpenses",
         "account_nm": "기타의 대손상각비 조정"},
    ]
    picked = dart._da_rows(rows)
    names = [r["account_nm"] for r in picked]
    assert "대손상각비 조정" not in names
    assert "기타의 대손상각비 조정" not in names
    assert len(picked) == 2


def test_da_rows_name_fallback_also_excludes_bad_debt():
    """표준계정코드를 안 쓰는 회사(이름 폴백 경로)에서도 대손·손상은 빠져야 한다."""
    rows = [
        {"account_id": "-표준계정코드 미사용-", "account_nm": "감가상각비"},
        {"account_id": "-표준계정코드 미사용-", "account_nm": "대손상각비"},
        {"account_id": "-표준계정코드 미사용-", "account_nm": "유형자산손상차손"},
    ]
    assert [r["account_nm"] for r in dart._da_rows(rows)] == ["감가상각비"]


# ── 순부채 ────────────────────────────────────────────────────────
@pytest.fixture
def stub_balance(monkeypatch):
    def apply(short, long, lease, cash):
        monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
            "short_term": _v(short, "테스트 단기차입금"),
            "long_term": _v(long), "lease": _v(lease)})
        monkeypatch.setattr(dart, "financial_item", lambda *a, **k: _v(cash))
    return apply


def test_net_debt_is_ibd_minus_cash(stub_balance):
    stub_balance(short=100, long=200, lease=50, cash=120)
    nd = dcf_inputs.net_debt("테스트")
    assert nd.value == 100 + 200 + 50 - 120
    assert nd.extras["interest_bearing_debt"].value == 350


def test_net_debt_can_exclude_lease(stub_balance):
    stub_balance(short=100, long=200, lease=50, cash=120)
    assert dcf_inputs.net_debt("테스트", include_lease=False).value == 180


def test_net_cash_is_negative_and_flagged(stub_balance):
    """삼성전자처럼 현금이 차입금보다 많으면 순부채는 음수(순현금)."""
    stub_balance(short=10, long=10, lease=0, cash=100)
    nd = dcf_inputs.net_debt("테스트")
    assert nd.value == -80
    assert "순현금" in nd.provenance.note


# ── 5개년 비율 ────────────────────────────────────────────────────
@pytest.fixture
def stub_history(monkeypatch):
    """매출 100→110→121, EBIT 10%, CAPEX 5+무형1, D&A 7, NWC현금영향 −2(=ΔNWC +2)."""
    years = [2025, 2024, 2023]
    revs = {2023: 100, 2024: 110, 2025: 121}

    def nyear(company, item, n=5, *a, **k):
        # 실제 provider 는 못 찾는 항목에 DataError 를 낸다 — 운전자본 폴백 경로가
        # 그 계약대로 동작하는지 보려면 스텁도 같아야 한다.
        table = {"revenue": revs, "operating_income": {y: revs[y] * 0.1 for y in years}}
        if item not in table:
            raise DataError(f"테스트 스텁에 없는 항목: {item}")
        src = table[item]
        return {"corp_name": "테스트", "series": [{"year": y, "amount": src[y]} for y in years]}

    monkeypatch.setattr(dart, "financial_item_nyear", nyear)
    monkeypatch.setattr(dart, "cf_extras_nyear", lambda *a, **k: {
        "corp_name": "테스트",
        "capex": [{"year": y, "amount": 5} for y in years],
        "capex_intangible": [{"year": y, "amount": 1} for y in years],
        "da": [{"year": y, "amount": 7} for y in years],
        "nwc_change": [{"year": y, "amount": -2} for y in years],
        "ocf": [{"year": y, "amount": 20} for y in years],
        "interest": [{"year": y, "amount": 1} for y in years],
    })
    return revs


def test_capex_ratio_includes_intangibles(stub_history):
    """D&A 에 무형자산상각비가 들어있으므로 CAPEX 도 무형자산 취득을 포함해야 대칭이다."""
    r = dcf_inputs.historical_ratios("테스트", n=3)
    # (5+1)/매출 의 3개년 평균 → 100/110/121 기준
    expected = (6 / 100 + 6 / 110 + 6 / 121) / 3 * 100
    assert r["capex_pct"].value == pytest.approx(expected, abs=0.01)
    assert "무형자산 취득" in r["capex_pct"].provenance.note


def test_nwc_sign_is_flipped_from_cash_flow(stub_history):
    """현금흐름표 '자산부채의 변동' = −2 (현금 유출) → ΔNWC = +2 (운전자본 증가)."""
    r = dcf_inputs.historical_ratios("테스트", n=3)
    # Δ매출 = 10(2024), 11(2025) → ΔNWC/Δ매출 = 2/10, 2/11 의 평균
    expected = (2 / 10 + 2 / 11) / 2 * 100
    assert r["nwc_pct"].value == pytest.approx(expected, abs=0.01)
    assert r["nwc_pct"].value > 0, "부호를 반전하지 않으면 음수가 되어 가치가 과대평가된다"


def test_nwc_falls_back_to_balance_sheet(monkeypatch, stub_history):
    """CF 에 자산부채 변동 합계가 없는 회사(SK하이닉스·네이버 실측)는 재무상태표로 계산한다.

    이 경로는 **부호를 반전하지 않는다** — NWC 잔액의 증가가 곧 ΔNWC(현금 유출)이기 때문.
    CF 경로와 규약이 반대라 별도로 못 박아 둔다."""
    years = [2025, 2024, 2023]
    revs = {2023: 100, 2024: 110, 2025: 121}
    # NWC = AR + 재고 − AP : 20 → 23 → 26 (매년 +3)
    wc = {2023: (10, 15, 5), 2024: (12, 16, 5), 2025: (14, 17, 5)}

    def nyear(company, item, n=5, *a, **k):
        if item == "revenue":
            src = revs
        elif item == "operating_income":
            src = {y: revs[y] * 0.1 for y in years}
        else:
            idx = {"trade_receivables": 0, "inventories": 1, "trade_payables": 2}[item]
            src = {y: wc[y][idx] for y in years}
        return {"corp_name": "테스트", "series": [{"year": y, "amount": src[y]} for y in years]}

    monkeypatch.setattr(dart, "financial_item_nyear", nyear)
    monkeypatch.setattr(dart, "cf_extras_nyear", lambda *a, **k: {
        "corp_name": "테스트",
        "capex": [{"year": y, "amount": 5} for y in years],
        "capex_intangible": [{"year": y, "amount": 0} for y in years],
        "da": [{"year": y, "amount": 7} for y in years],
        "nwc_change": [{"year": y, "amount": None} for y in years],   # CF 경로 없음
        "ocf": [], "interest": [],
    })

    r = dcf_inputs.historical_ratios("테스트", n=3)
    # ΔNWC = +3 매년, Δ매출 = 10(2024), 11(2025)
    expected = (3 / 10 + 3 / 11) / 2 * 100
    assert r["nwc_pct"].value == pytest.approx(expected, abs=0.01)
    assert r["nwc_pct"].value > 0
    assert "재무상태표" in r["nwc_pct"].provenance.note
    assert "매출채권" in r["nwc_pct"].provenance.note
    assert "nwc_pct" not in r["missing"]


def test_growth_reports_both_arithmetic_and_cagr(stub_history):
    r = dcf_inputs.historical_ratios("테스트", n=3)
    assert r["revenue_growth_pct"].value == pytest.approx(10.0, abs=0.01)
    assert "CAGR" in r["revenue_growth_pct"].provenance.note
    assert r["revenue_growth_pct"].extras["cagr"].value == pytest.approx(10.0, abs=0.01)


def test_ratios_expose_per_year_detail(stub_history):
    r = dcf_inputs.historical_ratios("테스트", n=3)
    assert r["missing"] == []
    assert "연도별" in r["da_pct"].provenance.note


def test_missing_inputs_are_reported_not_faked(monkeypatch, stub_history):
    """CF 에 D&A·NWC 가 없으면 0 으로 때우지 않고 missing 으로 알린다."""
    years = [2025, 2024, 2023]
    monkeypatch.setattr(dart, "cf_extras_nyear", lambda *a, **k: {
        "corp_name": "테스트",
        "capex": [{"year": y, "amount": 5} for y in years],
        "capex_intangible": [{"year": y, "amount": None} for y in years],
        "da": [{"year": y, "amount": None} for y in years],
        "nwc_change": [{"year": y, "amount": None} for y in years],
        "ocf": [], "interest": [],
    })
    monkeypatch.setattr(dart, "da_best", lambda *a, **k: (_ for _ in ()).throw(
        DataError("주석에서도 못 찾음")))
    r = dcf_inputs.historical_ratios("테스트", n=3)
    assert r["da_pct"] is None and r["nwc_pct"] is None
    assert set(r["missing"]) == {"da_pct", "nwc_pct"}


# ── 세전 타인자본비용 ─────────────────────────────────────────────
def test_cost_of_debt_uses_interest_over_ibd(monkeypatch):
    monkeypatch.setattr(dart, "cf_extras", lambda *a, **k: {
        "interest": _v(40, "테스트 이자비용")})
    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(600, "테스트 단기차입금"), "long_term": _v(400), "lease": _v(0)})
    kd = dcf_inputs.cost_of_debt("테스트")
    assert kd.value == pytest.approx(4.0)


def test_cost_of_debt_flags_implausible_result(monkeypatch):
    """차입금 대비 이자가 비상식적이면(오리온 실측 41.8%) 경고를 남긴다."""
    monkeypatch.setattr(dart, "cf_extras", lambda *a, **k: {"interest": _v(400)})
    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(0, "x 단기차입금"), "long_term": _v(0), "lease": _v(1000)})
    kd = dcf_inputs.cost_of_debt("테스트")
    assert kd.value == pytest.approx(40.0)
    assert "⚠️" in kd.provenance.note and "범위" in kd.provenance.note


def test_cost_of_debt_refuses_when_no_debt(monkeypatch):
    monkeypatch.setattr(dart, "cf_extras", lambda *a, **k: {"interest": _v(10)})
    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(0, "x 단기차입금"), "long_term": _v(0), "lease": _v(0)})
    with pytest.raises(DataError, match="무차입"):
        dcf_inputs.cost_of_debt("테스트")


def test_cost_of_debt_refuses_without_interest_account(monkeypatch):
    # debt_balances 도 함께 스텁해야 한다 — 안 그러면 실제 provider 가 불려 키 없는 CI 에서는
    # DataError(키 없음) → 비상장 감사보고서 폴백으로 새어나가 다른 오류가 난다.
    monkeypatch.setattr(dart, "cf_extras", lambda *a, **k: {"interest": None})
    monkeypatch.setattr(dart, "debt_balances", lambda *a, **k: {
        "short_term": _v(100, "테스트 단기차입금"), "long_term": _v(0), "lease": _v(0)})
    with pytest.raises(DataError, match="이자비용"):
        dcf_inputs.cost_of_debt("테스트")


def test_cost_of_debt_falls_back_to_audit_report_when_api_has_no_data(monkeypatch):
    """상장 API 가 013(데이터 없음)을 내면 비상장 감사보고서 경로로 넘어가야 한다."""
    def boom(*a, **k):
        raise DataError("DART 재무제표 오류: 013 조회된 데이타가 없습니다.")

    monkeypatch.setattr(dart, "cf_extras", boom)
    monkeypatch.setattr(dart, "debt_balances", boom)
    monkeypatch.setattr(dcf_inputs, "_audit", lambda company, year=None: {
        "_name": "비상장테스트", "_year": 2025, "_rcept": "R1",
        "interest_paid": {"amount": 2_900_000_000},
        "short_term_debt": {"amount": 60_000_000_000},
    })
    kd = dcf_inputs.cost_of_debt("비상장테스트")
    assert kd.value == pytest.approx(4.83, abs=0.01)
    assert "감사보고서" in kd.provenance.source
    assert "비상장" in kd.provenance.note


# ── 영구성장률 ────────────────────────────────────────────────────
def test_terminal_growth_is_capped_at_risk_free_rate(monkeypatch):
    from providers import ecos

    monkeypatch.setattr(ecos, "risk_free_rate", lambda tenor="10Y": Value(
        4.33, "%", label="국고채10년",
        provenance=Provenance(source="한국은행 ECOS", source_type=SourceType.AUTHORITATIVE,
                              source_url="https://ecos.bok.or.kr", as_of="2026-08-25")))
    g = dcf_inputs.terminal_growth("KR")
    assert g.value == 4.33
    assert "무위험수익률" in g.provenance.note
    assert g.extras["risk_free_rate"].value == 4.33


def test_terminal_growth_rejects_unsupported_country():
    with pytest.raises(DataError):
        dcf_inputs.terminal_growth("DE")


# ── 베타 ──────────────────────────────────────────────────────────
def test_ols_recovers_known_slope():
    """y = 2x 이면 β = 2, R² = 1."""
    xs = [0.01, -0.02, 0.03, -0.01, 0.005]
    ys = [2 * x for x in xs]
    slope, _, r2 = beta_engine._ols(xs, ys)
    assert slope == pytest.approx(2.0)
    assert r2 == pytest.approx(1.0)


def test_ols_zero_beta_for_uncorrelated_series():
    xs = [0.01, -0.01, 0.01, -0.01]
    ys = [0.02, 0.02, 0.02, 0.02]      # 시장과 무관하게 일정
    slope, _, r2 = beta_engine._ols(xs, ys)
    assert slope == pytest.approx(0.0, abs=1e-12)
    assert r2 == pytest.approx(0.0, abs=1e-12)


def test_returns_are_period_over_period():
    series = [{"date": "1", "close": 100.0}, {"date": "2", "close": 110.0},
              {"date": "3", "close": 99.0}]
    r = beta_engine._returns(series)
    assert r["2"] == pytest.approx(0.10)
    assert r["3"] == pytest.approx(-0.10)
    assert "1" not in r, "첫 관측치는 수익률을 만들 수 없다"


def test_hamada_relever_unlever_round_trip():
    bu, de, tax = 1.2, 0.4, 25.0
    bl = beta_engine.relever(bu, de, tax)
    assert bl == pytest.approx(1.2 * (1 + 0.75 * 0.4))
    assert beta_engine.unlever(bl, de, tax) == pytest.approx(bu)


def test_relever_equals_unlevered_when_no_debt():
    assert beta_engine.relever(0.9, 0.0, 25.0) == pytest.approx(0.9)

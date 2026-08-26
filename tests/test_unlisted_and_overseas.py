"""비상장(감사보고서) 폴백 + 해외(Yahoo) 베타 회귀 테스트. 네트워크 없이 순수 로직만.

막으려는 사고:
  · 감사보고서 라벨에 붙는 주석 참조('단기차입금(주석8,9,10,11)') 때문에 매칭이 전부 실패
  · 현금흐름표 유출 항목(음수 공시)을 그대로 써서 CAPEX·이자가 음수가 되는 것
  · 주석표(천원 단위)를 본표(원 단위)로 착각해 1000배 틀리는 것
  · 해외 종목인데 국내(네이버) 경로로 붙어 엉뚱한 지수와 회귀하는 것
"""
from __future__ import annotations

import pytest

from core.schema import DataError
from engines import beta as beta_engine
from providers import dart_audit, yahoo


# ── 감사보고서 라벨 정규화 ────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("매출채권(주석4, 10)", "매출채권"),                 # 포마트 실측
    ("단기차입금(주석8,9,10,11)", "단기차입금"),          # 포마트 실측
    ("(2) 재고자산", "재고자산"),                        # 앞머리 번호
    ("Ⅰ. 매출액", "매출액"),                             # 로마숫자
    ("자 본 총 계", "자본총계"),                          # 공백 삽입
    ("현금및현금성자산", "현금및현금성자산"),
])
def test_audit_label_normalisation(raw, expected):
    assert dart_audit._norm(raw) == expected


def test_audit_labels_cover_dcf_inputs():
    """DCF 입력에 필요한 계정이 라벨 사전에 다 있어야 한다."""
    need = {"cash", "trade_receivables", "inventories", "trade_payables",
            "short_term_debt", "long_term_debt", "capex", "interest_paid", "sga"}
    assert need <= set(dart_audit._LABELS)


def test_extract_row_takes_rightmost_two_numbers():
    rows = [["단기차입금(주석8)", "", "1,835,237,810", "19,698,624,431"]]
    assert dart_audit._extract_row(rows, "short_term_debt") == [1835237810, 19698624431]


def test_negative_cash_outflow_is_parsed():
    """현금흐름표 유출은 음수(△·괄호)로 공시된다."""
    assert dart_audit._num("(7,155,686,593)") == -7155686593
    assert dart_audit._num("△2,900,896,319") == -2900896319
    assert dart_audit._num("-") is None


def test_audit_dcf_inputs_normalises_outflow_signs(monkeypatch):
    """capex·이자의 지급은 절댓값으로 정규화해 상장사 경로와 부호를 맞춘다."""
    monkeypatch.setattr(dart_audit, "_reports_map", lambda cc: {2025: "R1"})

    table = {"cash": 100, "capex": -30, "interest_paid": -5,
             "short_term_debt": 60, "revenue": 1000}

    def fake_year_value(cc, item, year):
        if item not in table:
            raise DataError(f"없음: {item}")
        return table[item], "R1", 2025

    monkeypatch.setattr(dart_audit, "year_value", fake_year_value)
    d = dart_audit.dcf_inputs("X")
    assert d["capex"]["amount"] == 30
    assert d["interest_paid"]["amount"] == 5
    assert d["cash"]["amount"] == 100
    assert "long_term_debt" not in d, "못 찾은 항목은 0 이 아니라 아예 없어야 한다"


def test_audit_depreciation_rejected_when_unit_looks_wrong(monkeypatch):
    """주석표는 천원 단위일 수 있다 — 매출 대비 비율이 말이 안 되면 채택하지 않는다."""
    monkeypatch.setattr(dart_audit, "_reports_map", lambda cc: {2025: "R1"})

    # 매출 1,000,000,000 인데 감가상각비가 753,402 (천원 단위 값) → 0.075% → 기각
    table = {"revenue": 1_000_000_000, "depreciation": -753_402}

    def fake_year_value(cc, item, year):
        if item not in table:
            raise DataError("없음")
        return table[item], "R1", 2025

    monkeypatch.setattr(dart_audit, "year_value", fake_year_value)
    assert "depreciation" not in dart_audit.dcf_inputs("X")


def test_audit_depreciation_accepted_in_plausible_range(monkeypatch):
    monkeypatch.setattr(dart_audit, "_reports_map", lambda cc: {2025: "R1"})
    table = {"revenue": 1_000_000_000, "depreciation": -50_000_000}   # 5%

    def fake_year_value(cc, item, year):
        if item not in table:
            raise DataError("없음")
        return table[item], "R1", 2025

    monkeypatch.setattr(dart_audit, "year_value", fake_year_value)
    d = dart_audit.dcf_inputs("X")
    assert d["depreciation"]["amount"] == 50_000_000


# ── Yahoo ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("market, symbol, name", [
    ("US", "^GSPC", "S&P 500"),
    ("JP", "^N225", "Nikkei 225"),
    ("TW", "^TWII", "TAIEX"),
])
def test_market_index_mapping(market, symbol, name):
    assert yahoo.market_index(market) == (symbol, name)


def test_market_index_rejects_unknown():
    with pytest.raises(DataError, match="지원하지 않는 시장"):
        yahoo.market_index("XX")


def test_price_series_requires_symbol():
    with pytest.raises(DataError, match="심볼"):
        yahoo.price_series("")


def test_yahoo_interval_mapping_is_validated(monkeypatch):
    with pytest.raises(DataError, match="period"):
        yahoo._fetch("AAPL", "fortnight", 5)


# ── 베타 라우팅 ───────────────────────────────────────────────────
def test_overseas_beta_uses_yahoo_not_naver(monkeypatch):
    """해외 종목이 국내 지수(KOSPI)와 회귀되면 완전히 틀린 베타가 나온다."""
    calls = []

    def fake_price(symbol, period, years):
        calls.append(("yahoo_price", symbol))
        return [{"date": f"2025{i:04d}", "close": 100 + i} for i in range(1, 60)]

    def fake_index(market, period, years):
        calls.append(("yahoo_index", market))
        return [{"date": f"2025{i:04d}", "close": 200 + i} for i in range(1, 60)]

    monkeypatch.setattr(yahoo, "price_series", fake_price)
    monkeypatch.setattr(yahoo, "index_series", fake_index)
    monkeypatch.setattr(beta_engine.naver, "price_series", lambda *a, **k: pytest.fail(
        "해외 종목인데 네이버(KRX) 경로를 탔다"))

    v = beta_engine.regression_beta("Apple", "week", 5, "KOSPI", "US", "AAPL")
    assert ("yahoo_price", "AAPL") in calls
    assert ("yahoo_index", "US") in calls
    assert "Yahoo Finance" in v.provenance.source
    assert "S&P 500" in v.provenance.note


def test_overseas_beta_requires_ticker(monkeypatch):
    with pytest.raises(DataError, match="심볼"):
        beta_engine.regression_beta("", "week", 5, "KOSPI", "US", None)


def test_domestic_beta_still_uses_naver(monkeypatch):
    calls = []
    monkeypatch.setattr(beta_engine.dart, "resolve",
                        lambda c: {"corp_name": "삼성전자", "stock_code": "005930"})

    def fake_price(code, period, years):
        calls.append(("naver_price", code))
        return [{"date": f"2025{i:04d}", "close": 100 + i} for i in range(1, 60)]

    monkeypatch.setattr(beta_engine.naver, "price_series", fake_price)
    monkeypatch.setattr(beta_engine.naver, "index_series",
                        lambda *a, **k: [{"date": f"2025{i:04d}", "close": 200 + i}
                                         for i in range(1, 60)])
    monkeypatch.setattr(yahoo, "price_series", lambda *a, **k: pytest.fail(
        "국내 종목인데 Yahoo 경로를 탔다"))

    v = beta_engine.regression_beta("삼성전자")
    assert ("naver_price", "005930") in calls
    assert "네이버" in v.provenance.source

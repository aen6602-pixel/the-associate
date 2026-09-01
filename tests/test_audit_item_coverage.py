"""비상장사 감사보고서 파서 — 항목 커버리지와 실패 방식.

상장사는 DART 정기보고서 API 로 재무를 읽고, 실패하면 감사보고서 파싱으로 넘어간다
(`providers.dart.financial_item` 의 fallback). 그 파서가 모르는 항목을 만나면 **DataError**
여야 한다 — KeyError 를 올리면 호출부의 `except DataError` 를 뚫고 나가, 화면에는
"생성 실패: 'ppe'" 처럼 원인을 알 수 없는 문구만 남는다(실측: 비상장사 DCF 엑셀 다운로드).
"""
from __future__ import annotations

import pytest

from core.schema import DataError
from providers import dart, dart_audit


# DCF 전체모델(5시트)이 감사보고서 경로에서 읽어야 하는 항목들.
_DCF_FULL_ITEMS = ["revenue", "cogs", "sga", "interest_expense", "tax_expense",
                   "net_income", "ppe", "cash"]


@pytest.mark.parametrize("item", _DCF_FULL_ITEMS)
def test_audit_parser_knows_every_item_the_dcf_workbook_needs(item):
    assert item in dart_audit._LABELS, (
        f"'{item}' 라벨이 없으면 비상장사 DCF 엑셀이 이 항목에서 죽는다")


def test_audit_labels_are_a_subset_of_the_api_item_map():
    """두 경로가 같은 이름을 써야 fallback 이 성립한다(감사보고서 전용 항목은 예외)."""
    audit_only = {"short_term_debt", "long_term_debt", "lease_liability",
                  "capex_intangible", "interest_paid", "depreciation", "capex", "ocf"}
    unknown = set(dart_audit._LABELS) - set(dart.ITEM_MAP) - audit_only
    assert not unknown, f"API 쪽 ITEM_MAP 에 없는 이름: {sorted(unknown)}"


def test_unknown_item_raises_dataerror_not_keyerror():
    with pytest.raises(DataError):
        dart_audit._extract_row([["유형자산", "1,000"]], "no_such_item")


def test_ppe_row_is_extracted():
    rows = [["과 목", "당기", "전기"],
            ["유형자산(주석4, 10)", "1,234,000", "1,100,000"],
            ["무형자산", "10,000", "9,000"]]
    assert dart_audit._extract_row(rows, "ppe") == [1234000, 1100000]


# ── 5시트 모델이 성립하지 않는 회사 ────────────────────────────────
def test_full_model_refuses_an_empty_history_with_a_usable_message(monkeypatch):
    """비상장사는 정기보고서 5개년 시리즈가 없다 — 알 수 없는 오류 대신 이유와 대안을 준다."""
    from engines import dcf_full

    empty = {
        "years": [2025, 2024, 2023, 2022, 2021], "corp_name": "비상장㈜",
        "revenue": [None] * 5, "cogs": [None] * 5, "sga": [None] * 5,
        "interest_expense": [None] * 5, "tax_expense": [None] * 5,
        "net_income": [None] * 5, "capex": [None] * 5, "ocf": [None] * 5,
        "nwc_change": [None] * 5, "da": [None] * 5,
    }
    monkeypatch.setattr(dcf_full, "_history", lambda company, year: empty)
    monkeypatch.setattr(dcf_full.dart, "resolve",
                        lambda c: {"corp_name": "비상장㈜", "corp_code": "0", "stock_code": ""})

    with pytest.raises(DataError) as e:
        dcf_full.build_full_model("비상장㈜", {"wacc_pct": 10.0, "terminal_growth_pct": 2.0})
    msg = str(e.value)
    assert "매출액" in msg and "1시트" in msg, "무엇이 없고 무엇을 대신 쓰라는지 말해야 한다"

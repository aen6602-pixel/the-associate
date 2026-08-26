"""산출물 내보내기 배선 — 어떤 답변에서 어떤 엑셀이 나오는지.

엑셀은 그 답변에서 **실제로 성공한 계산 도구의 입력을 그대로 재사용**해 만든다. 화면 숫자와
파일 숫자가 어긋나지 않게 하려는 것이고, 클라이언트가 보낸 값을 믿지 않기 위한 것이다.
여기서는 네트워크를 타지 않도록 워크북 생성 함수를 스텁하고 **라우팅만** 검증한다.
(생성기 자체는 openpyxl 로 여는 별도 실측으로 확인했다.)
"""
from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from core import auth, history as hist
from excel import exporters
from server.main import XLSX_MIME, app

DCF_INPUT = {"company": "오리온", "wacc_pct": 8.0, "net_debt": -275874909483,
             "revenue_growth_pct": 9.32, "ebit_margin_pct": 16.65, "da_pct": 5.31,
             "capex_pct": 4.72, "nwc_pct": 20.0, "terminal_growth_pct": 4.32}
COMPS_INPUT = {"target": "SK하이닉스", "peers": ["삼성전자"]}
SANG_INPUT = {"company": "에스케이트리켐"}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")


@pytest.fixture
def stub_builders(monkeypatch):
    """생성 함수를 가로채 (호출된 이름, 넘어온 kwargs) 를 기록한다."""
    seen = {}

    def make(name, fname):
        def builder(**kwargs):
            seen["builder"] = name
            seen["kwargs"] = kwargs
            return b"PK\x03\x04fake-xlsx", fname
        return builder

    monkeypatch.setattr(exporters, "dcf_full_workbook",
                        make("dcf_full_workbook", "DCF_전체모델_오리온_FY2025.xlsx"))
    monkeypatch.setattr(exporters, "dcf_workbook",
                        make("dcf_workbook", "DCF_오리온_FY2025.xlsx"))
    monkeypatch.setattr(exporters, "comps_workbook",
                        make("comps_workbook", "Comps_SK하이닉스_FY2025.xlsx"))
    monkeypatch.setattr(exporters, "sangjeung_workbook",
                        make("sangjeung_workbook", "상증법_에스케이트리켐_FY2025.xlsx"))
    return seen


def _ok(name, inp):
    return {"name": name, "input": inp, "result": {"ok": True, "value": {
        "value": 1, "unit": "KRW/주", "provenance": {
            "source": "계산엔진", "source_type": "computed", "source_url": ""}}}}


def _seed(trace: list) -> str:
    sid = hist.new_session_id()
    hist.save_session(sid, [
        {"role": "user", "content": "밸류에이션 해줘"},
        {"role": "assistant", "content": "결과입니다", "trace": trace},
    ], auth.user_key_for("alice"))
    return sid


def _client(gated) -> TestClient:
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "alice", "password": "pw-alice"}).status_code == 200
    return c


@pytest.mark.parametrize("kind, tool, tool_input, builder", [
    ("dcf_full", "compute_dcf", DCF_INPUT, "dcf_full_workbook"),
    ("dcf", "compute_dcf", DCF_INPUT, "dcf_workbook"),
    ("comps", "compute_comps", COMPS_INPUT, "comps_workbook"),
    ("sangjeung", "evaluate_sangjeung_value", SANG_INPUT, "sangjeung_workbook"),
])
def test_each_kind_routes_to_its_builder(gated, stub_builders, kind, tool, tool_input, builder):
    sid = _seed([_ok(tool, tool_input)])
    c = _client(gated)
    r = c.post(f"/api/sessions/{sid}/export", json={"index": 1, "kind": kind})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(XLSX_MIME)
    assert r.content.startswith(b"PK")
    assert stub_builders["builder"] == builder
    assert stub_builders["kwargs"] == tool_input, "도구 입력을 그대로 재사용해야 한다"


def test_korean_filename_survives_content_disposition(gated, stub_builders):
    sid = _seed([_ok("evaluate_sangjeung_value", SANG_INPUT)])
    r = _client(gated).post(f"/api/sessions/{sid}/export",
                            json={"index": 1, "kind": "sangjeung"})
    disp = r.headers["content-disposition"]
    assert "filename*=UTF-8''" in disp
    assert urllib.parse.unquote(disp.split("''")[1]) == "상증법_에스케이트리켐_FY2025.xlsx"


@pytest.mark.parametrize("kind, expect", [
    ("comps", "Comps"), ("sangjeung", "상증법"), ("dcf_full", "DCF"),
])
def test_missing_calculation_is_refused_with_reason(gated, stub_builders, kind, expect):
    """계산하지 않은 방법의 엑셀을 요청하면 400 + 이유. 빈 파일을 만들지 않는다."""
    sid = _seed([_ok("get_financial_item", {"company": "오리온", "item": "revenue"})])
    r = _client(gated).post(f"/api/sessions/{sid}/export", json={"index": 1, "kind": kind})
    assert r.status_code == 400
    assert expect in r.json()["detail"]
    assert "builder" not in stub_builders


def test_failed_tool_call_does_not_qualify(gated, stub_builders):
    """실패한 계산으로 엑셀을 만들면 안 된다."""
    sid = _seed([{"name": "compute_dcf", "input": DCF_INPUT,
                  "result": {"ok": False, "error": "WACC<=g"}}])
    r = _client(gated).post(f"/api/sessions/{sid}/export", json={"index": 1, "kind": "dcf_full"})
    assert r.status_code == 400


def test_unknown_kind_is_rejected(gated, stub_builders):
    sid = _seed([_ok("compute_dcf", DCF_INPUT)])
    r = _client(gated).post(f"/api/sessions/{sid}/export", json={"index": 1, "kind": "nope"})
    assert r.status_code == 400
    assert "알 수 없는" in r.json()["detail"]


def test_html_report_still_works(gated):
    sid = _seed([_ok("compute_dcf", DCF_INPUT)])
    r = _client(gated).post(f"/api/sessions/{sid}/export",
                            json={"index": 1, "kind": "html_report"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert b"<html" in r.content.lower()


def test_export_requires_login(gated):
    sid = _seed([_ok("compute_dcf", DCF_INPUT)])
    r = TestClient(app).post(f"/api/sessions/{sid}/export",
                             json={"index": 1, "kind": "dcf_full"})
    assert r.status_code == 401


def test_cannot_export_another_users_session(gated, stub_builders, monkeypatch):
    """남의 세션 id 를 알아도 자기 폴더에 없으면 404."""
    monkeypatch.setenv("APP_USERS", "alice:pw-alice,bob:pw-bob")
    sid = hist.new_session_id()
    hist.save_session(sid, [{"role": "user", "content": "x"},
                            {"role": "assistant", "content": "y",
                             "trace": [_ok("compute_dcf", DCF_INPUT)]}],
                      auth.user_key_for("bob"))
    c = TestClient(app)
    c.post("/api/login", json={"name": "alice", "password": "pw-alice"})
    r = c.post(f"/api/sessions/{sid}/export", json={"index": 1, "kind": "dcf_full"})
    assert r.status_code == 404

"""산출물 내보내기 배선 — 어떤 답변에서 어떤 엑셀이 나오는지.

엑셀은 그 답변에서 **실제로 성공한 계산 도구의 입력을 그대로 재사용**해 만든다. 화면 숫자와
파일 숫자가 어긋나지 않게 하려는 것이고, 클라이언트가 보낸 값을 믿지 않기 위한 것이다.
여기서는 네트워크를 타지 않도록 워크북 생성 함수를 스텁하고 **라우팅만** 검증한다.
(생성기 자체는 openpyxl 로 여는 별도 실측으로 확인했다.)
"""
from __future__ import annotations

import inspect
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from core import auth, history as hist
from excel import exporters
from server.main import (_BUILDER_ALIASES, _XLSX_EXPORTS, XLSX_MIME,
                         _adapt_to_builder, app)

DCF_INPUT = {"company": "오리온", "wacc_pct": 8.0, "net_debt": -275874909483,
             "revenue_growth_pct": 9.32, "ebit_margin_pct": 16.65, "da_pct": 5.31,
             "capex_pct": 4.72, "nwc_pct": 20.0, "terminal_growth_pct": 4.32}
COMPS_INPUT = {"companies": ["삼성전자", "SK하이닉스", "MU:US", "2330:TW"],
               "basis": "LTM", "display_currency": "USD"}
SANG_INPUT = {"company": "에스케이트리켐"}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")


@pytest.fixture
def stub_builders(monkeypatch):
    """생성 함수를 가로채 (호출된 이름, 넘어온 kwargs) 를 기록한다.

    스텁에 **실제 생성기의 시그니처를 복사**해 붙인다 — `**kwargs` 스텁을 쓰면 서버의
    인자 어댑터가 무엇을 걸러내는지 검증할 수 없고, 이번에 엑셀이 깨진 원인이 바로 그
    이음매였다."""
    seen: dict = {}
    real_sigs = {n: inspect.signature(getattr(exporters, n)) for n in (
        "dcf_full_workbook", "dcf_workbook", "comps_workbook", "sangjeung_workbook")}

    def make(name, fname):
        def builder(*args, **kwargs):
            seen["builder"] = name
            seen["kwargs"] = kwargs
            return b"PK\x03\x04fake-xlsx", fname
        builder.__signature__ = real_sigs[name]
        return builder

    for name, fname in (
        ("dcf_full_workbook", "DCF_전체모델_오리온_FY2025.xlsx"),
        ("dcf_workbook", "DCF_오리온_FY2025.xlsx"),
        ("comps_workbook", "Comps_SK하이닉스_FY2025.xlsx"),
        ("sangjeung_workbook", "상증법_에스케이트리켐_FY2025.xlsx"),
    ):
        monkeypatch.setattr(exporters, name, make(name, fname))

    seen["expected_kwargs"] = lambda name, inp: _adapt_to_builder(
        getattr(exporters, name), inp)[0]
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


# ── 도구 ↔ 워크북 시그니처 정합성 (이 테스트가 없어서 엑셀이 깨진 채 배포됐다) ──────
#
# 실측 사고: compute_dcf 에 market 인자를 추가한 뒤 dcf_full_workbook 이
# `unexpected keyword argument 'market'` 로 죽어 DCF 엑셀 다운로드가 전부 400 이 됐다.
# 라우팅은 스텁으로, 생성기는 손으로 만든 인자로 각각 테스트했는데 **둘을 붙인 이음매**를
# 검증하지 않아 놓쳤다. 아래는 실제 도구 스키마 × 실제 생성기 시그니처를 맞춰본다.
def _tool_schema(name: str) -> dict:
    from agent import registry

    return next(s for s in registry.tool_schemas() if s["name"] == name)


@pytest.mark.parametrize("kind", sorted(_XLSX_EXPORTS))
def test_tool_input_binds_to_its_workbook_builder(kind):
    """도구가 낼 수 있는 모든 인자를 넣어도 생성기 호출이 성립해야 한다."""
    from excel import exporters

    tool_name, builder_name, _ = _XLSX_EXPORTS[kind]
    builder = getattr(exporters, builder_name)
    props = _tool_schema(tool_name)["input_schema"]["properties"]

    # 도구가 낼 수 있는 최대 입력(모든 프로퍼티) — 값은 타입만 맞춘 더미
    dummy = {}
    for k, spec in props.items():
        t = spec.get("type")
        dummy[k] = {"string": "X", "number": 1.0, "integer": 1,
                    "boolean": True, "array": ["X"]}.get(t, "X")

    kwargs, dropped = _adapt_to_builder(builder, dummy)
    inspect.signature(builder).bind(**kwargs)   # 여기서 TypeError 면 이음매가 깨진 것

    required = _tool_schema(tool_name)["input_schema"].get("required", [])
    lost = [k for k in required if k in dropped and k not in _BUILDER_ALIASES]
    assert not lost, f"{kind}: 필수 인자 {lost} 가 생성기에 전달되지 못한다"


def test_adapter_maps_renamed_growth_argument():
    """compute_dcf 는 revenue_growth_pct, dcf_workbook 은 revenue_growth 를 쓴다."""
    from excel import exporters

    kwargs, dropped = _adapt_to_builder(exporters.dcf_workbook,
                                        {"revenue_growth_pct": 9.32, "company": "오리온"})
    assert kwargs["revenue_growth"] == 9.32
    assert "revenue_growth_pct" not in kwargs
    assert dropped == []


def test_adapter_drops_arguments_the_builder_cannot_take():
    from excel import exporters

    kwargs, dropped = _adapt_to_builder(exporters.dcf_full_workbook,
                                        {"company": "오리온", "market": "KR", "bogus": 1})
    assert "market" not in kwargs and "bogus" not in kwargs
    assert set(dropped) == {"market", "bogus"}


def test_overseas_dcf_excel_is_refused_not_silently_korean(gated, stub_builders):
    """market=US 인데 생성기가 market 을 못 받으면, 한국 모델을 조용히 만들면 안 된다."""
    sid = _seed([_ok("compute_dcf", {**DCF_INPUT, "market": "US"})])
    r = _client(gated).post(f"/api/sessions/{sid}/export",
                            json={"index": 1, "kind": "dcf_full"})
    assert r.status_code == 400
    assert "한국" in r.json()["detail"] and "US" in r.json()["detail"]
    assert "builder" not in stub_builders


def test_domestic_dcf_excel_passes_through(gated, stub_builders):
    sid = _seed([_ok("compute_dcf", {**DCF_INPUT, "market": "KR"})])
    r = _client(gated).post(f"/api/sessions/{sid}/export",
                            json={"index": 1, "kind": "dcf_full"})
    assert r.status_code == 200
    assert "market" not in stub_builders["kwargs"], "생성기가 못 받는 인자는 걸러져야 한다"


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
    # 도구 입력을 재사용하되, 생성기가 받는 이름으로 맞춰져 넘어가야 한다.
    assert stub_builders["kwargs"] == stub_builders["expected_kwargs"](builder, tool_input)
    assert stub_builders["kwargs"], "인자가 전부 걸러지면 생성기가 기본값으로 엉뚱한 걸 만든다"


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

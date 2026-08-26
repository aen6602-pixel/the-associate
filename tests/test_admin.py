"""관리자 페이지 — 권한 경계와 집계 정확성.

가장 막아야 할 사고는 **일반 사용자가 남의 대화를 보는 것**이다. 관리자 판정과 API 접근을
각각 못 박는다. 관리자 지정은 ADMIN_USERS, 없으면 APP_USERS 의 첫 계정.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import admin, auth, history as hist
from server.main import app


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setenv("APP_USERS", "sanghwa:pw-a,teammate:pw-b,third:pw-c")
    monkeypatch.delenv("ADMIN_USERS", raising=False)


def login(client: TestClient, name: str, pw: str) -> None:
    assert client.post("/api/login", json={"name": name, "password": pw}).status_code == 200


# ── 관리자 판정 ───────────────────────────────────────────────────
def test_first_account_is_admin_by_default(team):
    assert auth.admins() == {"sanghwa"}
    assert auth.is_admin(auth.Viewer(key="k", label="sanghwa")) is True
    assert auth.is_admin(auth.Viewer(key="k", label="teammate")) is False


def test_admin_users_env_overrides(team, monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "teammate, third")
    assert auth.admins() == {"teammate", "third"}
    assert auth.is_admin(auth.Viewer(key="k", label="sanghwa")) is False
    assert auth.is_admin(auth.Viewer(key="k", label="teammate")) is True


def test_shared_password_setup_has_no_admin(monkeypatch):
    """공용 비밀번호는 사용자를 구분할 수 없으니 관리자도 없다."""
    monkeypatch.delenv("APP_USERS", raising=False)
    monkeypatch.delenv("ADMIN_USERS", raising=False)
    monkeypatch.setenv("APP_PASSWORD", "one-for-all")
    assert auth.admins() == set()
    assert auth.is_admin(auth.Viewer(key="k", label="member")) is False


def test_user_key_matches_authenticate(team):
    """관리자 집계는 이름으로 폴더를 찾는다 — 로그인 시 만들어지는 키와 같아야 한다."""
    v = auth.authenticate("teammate", "pw-b")
    assert v is not None
    assert auth.user_key_for("teammate") == v.key
    assert auth.user_key_for("TEAMMATE") == v.key, "대소문자가 달라도 같은 폴더여야 한다"


# ── 접근 통제 ─────────────────────────────────────────────────────
def test_non_admin_is_forbidden(team):
    with TestClient(app) as c:
        login(c, "teammate", "pw-b")
        assert c.get("/api/admin/overview").status_code == 403
        assert c.get("/api/admin/sessions/sanghwa/x").status_code == 403


def test_anonymous_is_unauthorized(team):
    with TestClient(app) as c:
        assert c.get("/api/admin/overview").status_code == 401


def test_admin_can_read_overview(team):
    with TestClient(app) as c:
        login(c, "sanghwa", "pw-a")
        r = c.get("/api/admin/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["admins"] == ["sanghwa"]
        assert {u["name"] for u in body["users"]} == {"sanghwa", "teammate", "third"}


def test_me_exposes_admin_flag(team):
    with TestClient(app) as c:
        login(c, "teammate", "pw-b")
        assert c.get("/api/me").json()["is_admin"] is False
        assert c.get("/api/bootstrap").json()["viewer"]["is_admin"] is False
    with TestClient(app) as c:
        login(c, "sanghwa", "pw-a")
        assert c.get("/api/me").json()["is_admin"] is True
        assert c.get("/api/bootstrap").json()["viewer"]["is_admin"] is True


# ── 집계 ──────────────────────────────────────────────────────────
def _seed(name: str, question: str, tools: list[tuple[str, bool]]) -> str:
    sid = hist.new_session_id()
    trace = [{"name": n, "input": {}, "result": {"ok": ok, "value": {
        "value": 1, "unit": "KRW", "provenance": {"source": "DART", "source_type":
                                                  "authoritative", "source_url": ""}}}}
             for n, ok in tools]
    hist.save_session(sid, [
        {"role": "user", "content": question},
        {"role": "assistant", "content": "답변입니다", "trace": trace},
    ], auth.user_key_for(name))
    return sid


def test_overview_counts_per_user(team):
    _seed("teammate", "삼성전자 매출액", [("get_financial_item", True)])
    _seed("teammate", "SK하이닉스 WACC", [("compute_wacc_auto", True),
                                          ("get_beta", False)])
    _seed("sanghwa", "한국 ERP", [("get_equity_risk_premium", True)])

    d = admin.overview()
    by = {u["name"]: u for u in d["users"]}
    assert by["teammate"]["sessions"] == 2
    assert by["teammate"]["questions"] == 2
    assert by["teammate"]["tool_calls"] == 3
    assert by["teammate"]["failed_calls"] == 1
    assert by["third"]["sessions"] == 0
    assert d["totals"]["questions"] == 3
    assert dict((t["name"], t["count"]) for t in d["tools"])["get_financial_item"] == 1
    assert any(r["user"] == "teammate" and "삼성전자" in r["question"] for r in d["timeline"])


def test_admin_can_open_another_users_session(team):
    sid = _seed("teammate", "오리온 순부채", [("get_net_debt", True)])
    with TestClient(app) as c:
        login(c, "sanghwa", "pw-a")
        r = c.get(f"/api/admin/sessions/teammate/{sid}")
        assert r.status_code == 200
        body = r.json()
        assert body["user"] == "teammate"
        assert body["messages"][0]["content"] == "오리온 순부채"
        assert "<p>" in body["messages"][1]["html"], "답변은 마크다운 렌더까지 돼야 한다"


def test_unknown_user_is_not_probeable(team):
    """존재하지 않는 계정으로 임의 폴더를 훑을 수 없어야 한다."""
    assert admin.user_session("nobody", "whatever") is None
    assert admin.user_session("", "whatever") is None

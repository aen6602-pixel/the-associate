"""배포 회귀 테스트 — 네트워크·LLM 호출 없이 서버 계약을 검증한다.

CI(.github/workflows/ci.yml)가 push 마다 돌려서 아래 사고를 막는다:
  1) 로그인 게이트가 열린 채로 배포되는 것
  2) 남의 대화가 보이는 것
  3) 배포 환경에서 못 쓰는 두뇌(claude CLI)가 UI 에 노출되는 것
  4) 앱이 import 단계에서 죽어 헬스체크만 통과하는 것
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import auth, history as hist
from server.main import app

GOOD = {"name": "alice", "password": "pw-alice"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice,bob:pw-bob")


@pytest.fixture
def no_auth(monkeypatch):
    monkeypatch.delenv("APP_USERS", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)


def login(client: TestClient, **creds) -> None:
    res = client.post("/api/login", json={**GOOD, **creds})
    assert res.status_code == 200, res.text


# ── 헬스체크 ──────────────────────────────────────────────────────
def test_healthz_is_public(client, gated):
    """호스팅 헬스체크는 로그인 없이 200 — 단 응답이 오려면 앱 import 가 성공해야 한다."""
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["auth"] is True


# ── 로그인 게이트 ─────────────────────────────────────────────────
def test_deploy_without_any_auth_is_blocked(client, no_auth):
    """DEPLOY_MODE=1 인데 인증 수단이 하나도 없으면 데이터를 내주지 않는다(fail closed)."""
    assert client.get("/api/bootstrap").status_code == 503
    assert client.get("/api/sessions").status_code == 503

    me = client.get("/api/me").json()
    assert me["authenticated"] is False and me["blocked"] is True
    assert "APP_USERS" in me["message"]


def test_anonymous_cannot_read_anything(client, gated):
    for path in ("/api/bootstrap", "/api/sessions", "/api/sessions/whatever"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/ask", json={"question": "삼성전자 매출액"}).status_code == 401


def test_wrong_password_is_rejected(client, gated):
    res = client.post("/api/login", json={"name": "alice", "password": "wrong"})
    assert res.status_code == 401
    assert auth.COOKIE_NAME not in res.cookies
    assert client.get("/api/bootstrap").status_code == 401


def test_unknown_user_is_rejected(client, gated):
    assert client.post("/api/login", json={"name": "eve", "password": "pw-alice"}).status_code == 401


def test_login_then_access(client, gated):
    login(client)
    me = client.get("/api/me").json()
    assert me["authenticated"] is True and me["label"] == "alice"

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    body = boot.json()
    assert body["viewer"]["label"] == "alice"
    assert body["gate"] is True
    # 배포 모드에선 claude CLI 두뇌가 목록에 없어야 한다 — 서버에 CLI 가 없다.
    assert "anthropic" not in [e["provider"] for e in body["engines"]]
    assert [s["name"] for s in body["sources"]], "데이터 소스 카탈로그가 비어 있다"


def test_logout_clears_access(client, gated):
    login(client)
    assert client.get("/api/bootstrap").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/bootstrap").status_code == 401


def test_shared_password_mode_needs_no_name(client, monkeypatch):
    monkeypatch.delenv("APP_USERS", raising=False)
    monkeypatch.setenv("APP_PASSWORD", "one-for-all")

    assert client.get("/api/me").json()["needs_name"] is False
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "one-for-all"}).status_code == 200
    assert client.get("/api/bootstrap").status_code == 200


# ── 세션 격리 ─────────────────────────────────────────────────────
def test_sessions_are_isolated_between_users(gated):
    """alice 가 만든 대화는 bob 의 목록·조회에서 보이지 않는다."""
    with TestClient(app) as alice:
        login(alice, name="alice", password="pw-alice")
        sid = alice.post("/api/sessions/new").json()["id"]
        hist.save_session(sid, [{"role": "user", "content": "삼성전자 매출액"}],
                          auth.authenticate("alice", "pw-alice").key)
        assert [s["id"] for s in alice.get("/api/sessions").json()["sessions"]] == [sid]
        assert alice.get(f"/api/sessions/{sid}").status_code == 200

    with TestClient(app) as bob:
        login(bob, name="bob", password="pw-bob")
        assert bob.get("/api/sessions").json()["sessions"] == []
        assert bob.get(f"/api/sessions/{sid}").status_code == 404


def test_session_id_path_traversal_is_rejected(client, gated):
    login(client)
    assert client.get("/api/sessions/..%2F..%2Fetc%2Fpasswd").status_code == 404


# ── 토큰 위조 ─────────────────────────────────────────────────────
def test_forged_cookie_is_rejected(client, gated):
    login(client)
    real = client.cookies.get(auth.COOKIE_NAME)
    payload, _, sig = real.rpartition(".")
    client.cookies.set(auth.COOKIE_NAME, f"{payload}.{'a' * len(sig)}")
    assert client.get("/api/bootstrap").status_code == 401


def test_expired_token_is_rejected(gated):
    v = auth.Viewer(key="k", label="alice")
    old = auth.issue_token(v, now=0)              # 1970년에 발급 → 이미 만료
    assert auth.parse_token(old) is None
    assert auth.parse_token(auth.issue_token(v)) == v


def test_removed_user_cannot_reuse_cookie(client, gated, monkeypatch):
    login(client)
    assert client.get("/api/bootstrap").status_code == 200
    monkeypatch.setenv("APP_USERS", "bob:pw-bob")  # alice 계정 삭제
    assert client.get("/api/bootstrap").status_code == 401


# ── 질문 스트리밍 (두뇌는 가짜로 대체) ─────────────────────────────
def test_ask_streams_trace_and_saves_session(client, gated, monkeypatch):
    """SSE 로 tool_use/tool_result/final/done 이 흐르고, 끝나면 대화가 저장된다."""
    fake_result = {
        "ok": True,
        "value": {"value": 300000000, "unit": "KRW-백만",
                  "provenance": {"source_type": "authoritative", "source": "DART",
                                 "source_url": "https://opendart.fss.or.kr", "as_of": "2025",
                                 "original_field": "revenue"},
                  "extras": {}},
    }

    def fake_answer(question, history=None, provider=None, model=None, **kw):
        assert provider == "openai"
        yield {"type": "tool_use", "name": "get_financial_item", "input": {"company": "삼성전자"}}
        yield {"type": "tool_result", "name": "get_financial_item",
               "input": {"company": "삼성전자"}, "result": fake_result}
        yield {"type": "final", "text": "삼성전자 매출액은 **300조원**입니다.\n\n| 항목 | 값 |\n|---|---|\n| 매출 | 300조 |"}

    monkeypatch.setattr("server.main.brain.answer", fake_answer)

    login(client)
    sid = client.post("/api/sessions/new").json()["id"]
    with client.stream("POST", "/api/ask", json={
            "question": "삼성전자 매출액", "session_id": sid, "provider": "openai"}) as res:
        assert res.status_code == 200
        events = [json.loads(line[6:]) for line in res.iter_lines()
                  if line.startswith("data: ")]

    kinds = [e["type"] for e in events]
    assert kinds == ["start", "tool_use", "tool_result", "final", "done"]

    final = events[kinds.index("final")]
    assert "<strong>300조원</strong>" in final["html"], "마크다운이 서버에서 렌더돼야 한다"
    assert "<table>" in final["html"], "표가 렌더돼야 한다"
    assert len(final["trace"]) == 1

    # 저장 확인 — 새로고침 후에도 대화가 남아있어야 한다.
    rec = client.get(f"/api/sessions/{sid}").json()
    assert [m["role"] for m in rec["messages"]] == ["user", "assistant"]
    assert rec["messages"][1]["trace"][0]["name"] == "get_financial_item"
    assert "<strong>" in rec["messages"][1]["html"]


def test_ask_survives_brain_failure(client, gated, monkeypatch):
    """두뇌가 터져도 500 이 아니라 스트림 안에서 오류로 전달된다(대화도 저장)."""
    def boom(*a, **kw):
        yield {"type": "tool_use", "name": "get_financial_item", "input": {}}
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("server.main.brain.answer", boom)

    login(client)
    with client.stream("POST", "/api/ask", json={"question": "x", "provider": "openai"}) as res:
        assert res.status_code == 200
        events = [json.loads(line[6:]) for line in res.iter_lines() if line.startswith("data: ")]

    assert "error" in [e["type"] for e in events]
    final = next(e for e in events if e["type"] == "final")
    assert "provider exploded" in final["text"]


def test_ask_rejects_unknown_provider(client, gated):
    login(client)
    res = client.post("/api/ask", json={"question": "x", "provider": "nonexistent"})
    assert res.status_code == 400


def test_ask_rejects_cli_provider_when_deployed(client, gated):
    """claude CLI 두뇌는 서버에 CLI 가 없어 동작 불가 → 400 으로 명확히 거절."""
    login(client)
    res = client.post("/api/ask", json={"question": "x", "provider": "anthropic"})
    assert res.status_code == 400


# ── 정적 UI ───────────────────────────────────────────────────────
def test_index_and_assets_are_served(client, gated):
    page = client.get("/")
    assert page.status_code == 200
    assert "The Associate" in page.text
    assert "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200

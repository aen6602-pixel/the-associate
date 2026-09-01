"""공용 계정에서 '누가 물었는지' 를 남기는 자기신고 이름.

한 계정(team 등)을 여러 명이 나눠 쓰면 대화기록이 한 폴더에 섞여, 관리자 화면에서 질문의
주인을 알 수 없다. 로그인 뒤 이름을 한 번 받아 **쿠키에 담고 질문마다 찍는다.**

여기서 못 박는 것:
- 이름은 권한이 아니다 — 계정(label)과 세션 폴더(key)는 이름을 바꿔도 그대로다.
- 이름은 질문 시점에 박제된다 — 나중에 바꿔도 과거 기록의 작성자는 안 변한다.
- 이름을 밝히기 전 기록(구 데이터)은 계정 이름으로 떨어진다(집계에서 사라지지 않는다).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import admin, auth, history as hist
from server.main import app


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setenv("APP_USERS", "deskteam:pw-team,deskboss:pw-a")
    monkeypatch.setenv("ADMIN_USERS", "deskboss")


def login(c: TestClient, name: str, pw: str) -> None:
    assert c.post("/api/login", json={"name": name, "password": pw}).status_code == 200


# ── 토큰 ─────────────────────────────────────────────────────────
def test_token_carries_the_member_name(team):
    v = auth.Viewer(key="k", label="deskteam", member="이상화")
    back = auth.parse_token(auth.issue_token(v))
    assert back is not None
    assert (back.label, back.member) == ("deskteam", "이상화")


def test_member_name_does_not_change_account_or_storage(team):
    """이름은 표시용이다 — 계정·세션 폴더·관리자 권한에 영향을 주면 안 된다."""
    plain = auth.authenticate("deskteam", "pw-team")
    assert plain is not None
    named = plain._replace(member="이상화")
    assert named.key == plain.key, "세션 폴더가 갈리면 남의 대화가 사라진 것처럼 보인다"
    assert named.label == "deskteam"
    assert auth.is_admin(named) is False


def test_clean_member_trims_and_caps():
    assert auth.clean_member("  이 상화  ") == "이 상화"
    assert auth.clean_member("\n\t") == ""
    assert len(auth.clean_member("가" * 80)) == auth.MEMBER_MAX


# ── 등록 API ──────────────────────────────────────────────────────
def test_bootstrap_asks_until_the_name_is_given(team):
    with TestClient(app) as c:
        login(c, "deskteam", "pw-team")
        v = c.get("/api/bootstrap").json()["viewer"]
        assert v["needs_member"] is True and v["member"] == ""

        assert c.post("/api/member", json={"name": "이상화"}).json()["member"] == "이상화"

        v = c.get("/api/bootstrap").json()["viewer"]
        assert v["needs_member"] is False and v["member"] == "이상화"
        assert v["label"] == "deskteam", "계정은 그대로여야 한다"


def test_too_short_name_is_rejected(team):
    with TestClient(app) as c:
        login(c, "deskteam", "pw-team")
        assert c.post("/api/member", json={"name": " ㄱ "}).status_code == 400
        assert c.get("/api/bootstrap").json()["viewer"]["needs_member"] is True


def test_member_endpoint_needs_login(team):
    with TestClient(app) as c:
        assert c.post("/api/member", json={"name": "이상화"}).status_code == 401


# ── 관리자 집계 ───────────────────────────────────────────────────
def _seed(account: str, question: str, by: str | None) -> str:
    sid = hist.new_session_id()
    user = {"role": "user", "content": question}
    if by is not None:
        user["by"] = by
    hist.save_session(sid, [user, {"role": "assistant", "content": "답변", "trace": []}],
                      auth.user_key_for(account))
    return sid


def test_overview_splits_a_shared_account_by_person(team):
    _seed("deskteam", "삼성전자 매출액", "이상화")
    _seed("deskteam", "SK하이닉스 WACC", "이상화")
    _seed("deskteam", "기아 DCF", "김동현")

    d = admin.overview()
    by_person = {m["name"]: m for m in d["members"]}
    assert by_person["이상화"]["questions"] == 2
    assert by_person["김동현"]["questions"] == 1
    assert by_person["이상화"]["account"] == "deskteam"
    # 전역 합계는 다른 테스트가 심은 기록까지 세므로 이 계정으로 좁혀 단정한다.
    mine = [m for m in d["members"] if m["account"] == "deskteam"]
    assert len(mine) == 2, "계정은 1개여도 사람은 2명으로 갈려야 한다"

    account = next(u for u in d["users"] if u["name"] == "deskteam")
    assert account["members"] == ["김동현", "이상화"]
    assert any(r["member"] == "김동현" and "기아" in r["question"] for r in d["timeline"])


def test_records_without_a_name_fall_back_to_the_account(team):
    """이름 기능 이전의 기록도 집계에서 빠지면 안 된다."""
    _seed("deskteam", "예전 질문", None)
    d = admin.overview()
    assert any(m["name"] == "deskteam" and m["account"] == "deskteam" for m in d["members"])
    assert any(r["member"] == "deskteam" for r in d["timeline"])


# ── 질문에 이름이 박히는 지점 ─────────────────────────────────────
def test_the_question_is_stamped_with_the_name_at_the_time(team, monkeypatch):
    """이름을 바꿔도 이미 저장된 질문의 작성자는 그대로여야 한다."""
    def fake_answer(question, history=None, provider=None, model=None, **kw):
        yield {"type": "final", "text": "답변"}

    monkeypatch.setattr("server.main.brain.answer", fake_answer)

    with TestClient(app) as c:
        login(c, "deskteam", "pw-team")
        c.post("/api/member", json={"name": "이상화"})
        sid = c.post("/api/sessions/new").json()["id"]
        with c.stream("POST", "/api/ask",
                      json={"question": "첫 질문", "session_id": sid,
                            "provider": "openai"}) as res:
            assert res.status_code == 200
            res.read()

        c.post("/api/member", json={"name": "김동현"})
        with c.stream("POST", "/api/ask",
                      json={"question": "둘째 질문", "session_id": sid,
                            "provider": "openai"}) as res:
            assert res.status_code == 200
            res.read()

        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        asked = [(m["content"], m.get("by")) for m in msgs if m["role"] == "user"]
        assert asked == [("첫 질문", "이상화"), ("둘째 질문", "김동현")]


def test_without_a_name_the_account_is_stamped(team, monkeypatch):
    def fake_answer(question, history=None, provider=None, model=None, **kw):
        yield {"type": "final", "text": "답변"}

    monkeypatch.setattr("server.main.brain.answer", fake_answer)

    with TestClient(app) as c:
        login(c, "deskteam", "pw-team")
        sid = c.post("/api/sessions/new").json()["id"]
        with c.stream("POST", "/api/ask",
                      json={"question": "이름 없이", "session_id": sid,
                            "provider": "openai"}) as res:
            res.read()
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        assert msgs[0]["by"] == "deskteam"

"""Market Muse — 구독 채널 전언. **본 채팅과 격리되어야 한다.**

이 데이터는 공시가 아니다. 작성자를 검증할 수 없고 정정·삭제 이력도 없다. 이 앱의 나머지
전부가 "모든 숫자는 출처가 추적된다" 를 지키려고 만들어져 있으므로, 이 소스가 밸류에이션
경로로 새어 들어가면 그 약속이 무의미해진다. 여기서 고정하는 것은 그 경계다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import history as hist
from providers import marketmuse
from server.main import app


@pytest.fixture
def snap(monkeypatch):
    """네트워크 없이 스냅샷을 갈아끼운다."""
    def _set(posts, channels=()):
        monkeypatch.setattr(marketmuse, "snapshot", lambda: {
            "posts": list(posts), "channels": list(channels),
            "count": len(posts), "generated_at": "2026-08-30T11:50:37+00:00",
            "lookback_days": 21})
    return _set


def _p(channel, date, text):
    return {"channel": channel, "date": f"{date}T00:00:00+00:00", "text": text}


# ── 경계 ─────────────────────────────────────────────────────────
def test_channel_chatter_is_not_a_valuation_tool():
    """본 채팅의 도구 목록에 이 소스가 없어야 한다 — 있으면 두뇌가 DCF 근거로 끌어다 쓴다."""
    from agent import registry

    names = " ".join(registry.REGISTRY)
    assert "muse" not in names.lower()
    assert "telegram" not in names.lower()
    assert "channel" not in names.lower()


def test_valuation_registry_does_not_import_the_chatter_provider():
    """import 만으로도 위험하다 — 누군가 도구 하나를 추가하면 바로 연결돼 버린다."""
    import inspect

    from agent import registry

    src = inspect.getsource(registry)
    assert "marketmuse" not in src


def test_muse_answer_path_has_no_tools():
    """도구를 붙이면 공시 조회 결과와 채널 전언이 한 답변에 섞인다."""
    import inspect

    from agent import muse

    src = inspect.getsource(muse)
    assert "registry" not in src, "muse 는 도구 레지스트리를 알면 안 된다"
    assert "tools=" not in src


def test_prompt_forbids_presenting_chatter_as_fact():
    from agent import muse

    assert "공신력" in muse.SYSTEM_PROMPT
    assert "전언" in muse.SYSTEM_PROMPT


# ── 검색 ─────────────────────────────────────────────────────────
def test_synonyms_find_the_same_company_written_differently(snap):
    snap([_p("@a", "2026-08-30", "삼전 목표주가 상향"),
          _p("@b", "2026-08-29", "관계없는 잡담")])
    hits = marketmuse.search("삼성전자")
    assert [h["channel"] for h in hits] == ["@a"]


def test_reposts_across_channels_are_collapsed(snap):
    """채널 간 퍼나르기가 잦다 — 같은 글이 여러 건으로 보이면 근거가 부풀려 보인다."""
    snap([_p("@a", "2026-08-30", "HBM 공급 확대 전망입니다"),
          _p("@b", "2026-08-29", "HBM 공급 확대 전망입니다"),
          _p("@c", "2026-08-28", "HBM 가격 하락")])
    assert len(marketmuse.search("HBM")) == 2


def test_newer_posts_come_first(snap):
    snap([_p("@old", "2026-08-01", "환율 이야기"), _p("@new", "2026-08-30", "환율 이야기 2")])
    assert marketmuse.search("환율")[0]["channel"] == "@new"


def test_channel_filter_limits_the_evidence(snap):
    snap([_p("@a", "2026-08-30", "반도체 좋다"), _p("@b", "2026-08-30", "반도체 나쁘다")])
    hits = marketmuse.search("반도체", channel="@b")
    assert [h["channel"] for h in hits] == ["@b"]


def test_no_match_returns_nothing_rather_than_something_close(snap):
    """관련 없는 글을 '그나마 가까운 것' 으로 내주면 없는 근거가 있는 것처럼 보인다."""
    snap([_p("@a", "2026-08-30", "환율 이야기")])
    assert marketmuse.search("리노공업 목표주가") == []


def test_clean_text_strips_urls_but_keeps_content():
    out = marketmuse.clean_text("**중요** https://t.me/x 삼성전자 실적")
    assert "https://" not in out
    assert "삼성전자 실적" in out


# ── HTTP 경계 ────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "alice", "password": "pw-alice"}).status_code == 200
    return c


def test_muse_requires_login(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")
    anon = TestClient(app)
    for path in ("/api/muse/channels", "/api/muse/sessions"):
        assert anon.get(path).status_code == 401, path


def test_muse_history_is_separate_from_valuation_history(client, monkeypatch):
    """두 기록이 한 목록에 섞이면 어느 답이 공시 근거였는지 구분할 수 없다."""
    from core import auth

    # viewer.key 는 해시라 손으로 못 만든다 — 실제 로그인 쿠키에서 꺼내 쓴다.
    viewer = auth.parse_token(client.cookies.get(auth.COOKIE_NAME))
    assert viewer is not None

    hist.save_session("s-muse", [{"role": "user", "content": "채널 질문"}],
                      f"{viewer.key}__muse")
    hist.save_session("s-main", [{"role": "user", "content": "밸류에이션 질문"}], viewer.key)

    muse_ids = [s["id"] for s in client.get("/api/muse/sessions").json()["sessions"]]
    main_ids = [s["id"] for s in client.get("/api/sessions").json()["sessions"]]
    assert "s-muse" in muse_ids and "s-muse" not in main_ids
    assert "s-main" in main_ids and "s-main" not in muse_ids


def test_muse_page_is_served(client):
    r = client.get("/muse")
    assert r.status_code == 200
    assert "Market Muse" in r.text
    assert "공신력이 없" in r.text, "경고가 화면에 상시 노출돼야 한다"

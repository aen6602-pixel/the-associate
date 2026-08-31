"""Market Muse — 텔레그램 채널을 직접 읽어 답한다. **본 채팅과 격리되어야 한다.**

이 데이터는 공시가 아니다. 작성자를 검증할 수 없고 정정·삭제 이력도 없다. 이 앱의 나머지
전부가 "모든 숫자는 출처가 추적된다" 를 지키려고 만들어져 있으므로, 이 소스가 밸류에이션
경로로 새어 들어가면 그 약속이 무의미해진다. 여기서 고정하는 것은 그 경계다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import history as hist
from providers import telegram_muse as tg
from server.main import app


@pytest.fixture
def db(tmp_path, monkeypatch):
    """테스트마다 빈 DB. 실제 텔레그램은 건드리지 않는다."""
    monkeypatch.setattr(tg, "DB_PATH", tmp_path / "muse.db")
    return tmp_path / "muse.db"


def _put(rows):
    with tg._conn() as c:
        c.executemany("INSERT OR IGNORE INTO posts(channel,msg_id,date,text) VALUES(?,?,?,?)", rows)


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── 경계 ─────────────────────────────────────────────────────────
def test_channel_chatter_is_not_a_valuation_tool():
    """본 채팅의 도구 목록에 없어야 한다 — 있으면 두뇌가 DCF 근거로 끌어다 쓴다."""
    from agent import registry

    names = " ".join(registry.REGISTRY).lower()
    assert "muse" not in names and "telegram" not in names and "channel" not in names


def test_valuation_registry_does_not_import_the_chatter_provider():
    """import 만으로도 위험하다 — 도구 하나만 추가하면 바로 연결돼 버린다."""
    import inspect

    from agent import registry

    src = inspect.getsource(registry)
    assert "telegram_muse" not in src and "marketmuse" not in src


def test_muse_answer_path_has_no_tools():
    import inspect

    from agent import muse

    src = inspect.getsource(muse)
    assert "registry" not in src, "muse 는 도구 레지스트리를 알면 안 된다"
    assert "tools=" not in src


def test_prompt_forbids_presenting_chatter_as_fact():
    from agent import muse

    assert "공신력" in muse.SYSTEM_PROMPT
    assert "전언" in muse.SYSTEM_PROMPT


def test_session_string_never_appears_in_error_messages(db, monkeypatch):
    """세션 문자열은 계정 접근 권한 그 자체다 — 어떤 경로로도 화면·로그에 나오면 안 된다."""
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_STRING", "SUPERSECRETSESSION")
    api_id, api_hash, session = tg._creds()
    assert session == "SUPERSECRETSESSION"

    monkeypatch.delenv("TG_SESSION_STRING")
    with pytest.raises(Exception) as ei:
        tg._creds()
    assert "SUPERSECRET" not in str(ei.value)
    assert "TG_SESSION_STRING" in str(ei.value), "무엇이 빠졌는지는 알려줘야 한다"


def test_missing_credentials_name_what_to_do(db, monkeypatch):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(Exception, match="_muse_login"):
        tg._creds()


# ── 검색 ─────────────────────────────────────────────────────────
def test_synonyms_find_the_same_company_written_differently(db):
    _put([("@a", 1, _ago(1), "삼전 목표주가 상향"), ("@b", 1, _ago(1), "관계없는 잡담")])
    assert [h["channel"] for h in tg.search("삼성전자")] == ["@a"]


def test_reposts_across_channels_are_collapsed(db):
    """채널 간 퍼나르기가 잦다 — 같은 글이 여러 건이면 근거가 부풀려 보인다."""
    _put([("@a", 1, _ago(1), "HBM 공급 확대 전망입니다"),
          ("@b", 1, _ago(2), "HBM 공급 확대 전망입니다"),
          ("@c", 1, _ago(3), "HBM 가격 하락")])
    assert len(tg.search("HBM")) == 2


def test_newer_posts_come_first(db):
    _put([("@old", 1, _ago(20), "환율 이야기"), ("@new", 1, _ago(0.2), "환율 이야기 둘")])
    assert tg.search("환율")[0]["channel"] == "@new"


def test_channel_filter_limits_the_evidence(db):
    _put([("@a", 1, _ago(1), "반도체 좋다"), ("@b", 1, _ago(1), "반도체 나쁘다")])
    assert [h["channel"] for h in tg.search("반도체", channel="@b")] == ["@b"]


def test_no_match_returns_nothing_rather_than_something_close(db):
    """관련 없는 글을 '그나마 가까운 것' 으로 내주면 없는 근거가 있는 것처럼 보인다."""
    _put([("@a", 1, _ago(1), "환율 이야기")])
    assert tg.search("리노공업 목표주가") == []


def test_empty_db_searches_cleanly(db):
    assert tg.search("아무거나") == []


def test_clean_text_strips_urls_but_keeps_content():
    out = tg.clean_text("**중요** https://t.me/x 삼성전자 실적")
    assert "https://" not in out and "삼성전자 실적" in out


# ── 수집 상태 ────────────────────────────────────────────────────
def test_fresh_db_is_stale_so_first_visit_collects(db):
    assert tg.is_stale() is True


def test_recent_collection_is_not_stale(db, monkeypatch):
    tg._meta_set("collected_at", datetime.now(timezone.utc).isoformat())
    assert tg.is_stale() is False


def test_stats_reports_per_channel_counts(db):
    _put([("@a", 1, _ago(1), "글1"), ("@a", 2, _ago(2), "글2"), ("@b", 1, _ago(1), "글3")])
    s = tg.stats()
    assert s["count"] == 3
    assert {c["id"]: c["n"] for c in s["channels"]} == {"@a": 2, "@b": 1}


def test_channel_list_ignores_comments_and_blanks(monkeypatch, tmp_path):
    f = tmp_path / "ch.txt"
    f.write_text("# 주석\n\n@one   # 이름\n@two\n1208429502\n", encoding="utf-8")
    monkeypatch.setattr(tg, "CHANNELS_FILE", f)
    assert tg.channels() == ["@one", "@two", "1208429502"]


def test_collect_refuses_to_run_twice_at_once(db):
    """텔레그램은 rate-limit 이 있다 — 겹쳐 돌리면 계정이 제한될 수 있다.
    락을 흉내내지 않고 실제로 잡아서 진짜 동시 실행 상황을 만든다."""
    assert tg._lock.acquire(blocking=False)
    try:
        with pytest.raises(Exception, match="이미 수집 중"):
            tg.collect()
    finally:
        tg._lock.release()


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
    assert anon.get("/api/muse/status").status_code == 401
    assert anon.post("/api/muse/collect").status_code == 401


def test_muse_history_is_separate_from_valuation_history(client):
    """두 기록이 한 목록에 섞이면 어느 답이 공시 근거였는지 구분할 수 없다."""
    from core import auth

    viewer = auth.parse_token(client.cookies.get(auth.COOKIE_NAME))
    assert viewer is not None
    hist.save_session("s-muse", [{"role": "user", "content": "채널 질문"}], f"{viewer.key}__muse")
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


def test_status_does_not_block_on_collection(client, db, monkeypatch):
    """수집은 수 분이 걸린다 — 화면 진입이 그만큼 멈추면 안 된다."""
    calls = []
    monkeypatch.setattr(tg, "collect_in_background", lambda only=None: calls.append(1) or True)
    monkeypatch.setattr(tg, "collect", lambda only=None: pytest.fail("동기 수집을 하면 안 된다"))
    d = client.get("/api/muse/status").json()
    assert d["refresh_started"] is True and len(calls) == 1

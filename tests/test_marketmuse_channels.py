"""Market Muse 2차 — 텔레그램 에이전트에서 옮겨온 기능들.

옮긴 것은 넷이다: **별칭**(40개를 `@handle` 로 외우는 사람은 없다), **채널 지목 인식**
("잠실개미 채널에서 뭐래?"), **후속 질문 맥락**("그거 왜?" 는 그 자체로 검색해봐야 아무것도
안 걸린다), **브리핑**(물어볼 게 아직 없을 때). 여기서는 각각이 실제로 답의 근거를
바꾸는지 — 그리고 채널 관리가 관리자 밖으로 새지 않는지 — 를 못 박는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from providers import telegram_muse as tg
from server.main import app


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    """테스트가 진짜 텔레그램을 두드리지 않게 한다. 겸사겸사, 화면 진입마다 뜨는 백그라운드
    수집 스레드가 다음 테스트까지 살아남아 수집 락을 물고 있는 사고도 막는다."""
    monkeypatch.setattr(tg, "collect_in_background", lambda only=None: True)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "DB_PATH", tmp_path / "muse.db")
    return tmp_path / "muse.db"


@pytest.fixture
def chfile(tmp_path, monkeypatch):
    """채널 목록 파일도 테스트마다 새로. 씨앗 복사가 끼어들지 않게 씨앗도 같이 옮긴다."""
    f = tmp_path / "muse_channels.txt"
    f.write_text("# 주석\n\n@jake8lee   # 잠실개미\n@hedgehara  # Pluto Research\n"
                 "@plain\n1208429502  # 주식 급등일보\n", encoding="utf-8")
    monkeypatch.setattr(tg, "CHANNELS_FILE", f)
    monkeypatch.setattr(tg, "CHANNELS_SEED", f)
    return f


def _put(rows):
    with tg._conn() as c:
        c.executemany("INSERT OR IGNORE INTO posts(channel,msg_id,date,text) VALUES(?,?,?,?)", rows)


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── 채널 식별자 정규화 ────────────────────────────────────────────
# 사람이 붙여넣는 건 대개 t.me 링크인데 DB 에도 Telethon 에도 그 형태는 없다.
# 한 번 걸러내지 않으면 같은 채널이 두 줄로 쌓이고, 둘 다 반쪽짜리가 된다.
def test_paste_a_telegram_link_and_get_the_handle():
    assert tg.normalize("https://t.me/jake8lee") == "@jake8lee"
    assert tg.normalize("https://t.me/jake8lee/") == "@jake8lee"


def test_a_bare_name_becomes_a_handle():
    assert tg.normalize("jake8lee") == "@jake8lee"
    assert tg.normalize("@jake8lee") == "@jake8lee"


def test_numeric_channels_stay_numeric():
    """공개 아이디가 없는 채널은 숫자 id 로만 부를 수 있다 — @ 를 붙이면 못 찾는다."""
    assert tg.normalize("1208429502") == "1208429502"
    assert tg.normalize("-1001234567") == "-1001234567"


def test_normalizing_the_same_channel_twice_gives_one_key(chfile):
    """링크로 넣든 핸들로 넣든 이미 있는 채널이면 거절돼야 한다."""
    assert tg.add_channel("https://t.me/jake8lee") is False


# ── 별칭 ──────────────────────────────────────────────────────────
def test_aliases_come_from_the_trailing_comment(chfile):
    assert tg.aliases()["@jake8lee"] == "잠실개미"
    assert "@plain" not in tg.aliases(), "별칭이 없으면 항목 자체가 없어야 한다"


def test_label_falls_back_to_the_handle(chfile):
    assert tg.label("@jake8lee") == "잠실개미 (@jake8lee)"
    assert tg.label("@plain") == "@plain"


def test_status_carries_aliases_so_the_screen_can_show_names(chfile, db):
    _put([("@jake8lee", 1, _ago(1), "글")])
    s = tg.stats()
    assert s["channels"][0]["alias"] == "잠실개미"
    # 아직 한 건도 못 읽은 채널도 목록에는 있어야 한다 — 없으면 방금 추가한 채널이
    # 사라진 것처럼 보인다.
    assert {c["id"] for c in s["configured_channels"]} == {
        "@jake8lee", "@hedgehara", "@plain", "1208429502"}


# ── 채널 지목 인식 ────────────────────────────────────────────────
def test_naming_a_channel_by_alias_narrows_the_search(chfile):
    assert tg.detect_channel("잠실개미 채널에서 반도체 뭐래?") == ("@jake8lee", "잠실개미")


def test_naming_a_channel_by_handle_narrows_the_search(chfile):
    ch, _ = tg.detect_channel("@hedgehara 에서 뭐라던데")
    assert ch == "@hedgehara"


def test_a_plain_question_stays_across_all_channels(chfile):
    assert tg.detect_channel("반도체 요즘 어때?") == (None, None)


def test_common_words_in_an_alias_do_not_pick_a_channel(chfile):
    """'Research' 같은 낱말은 채널 이름 절반에 들어 있다 — 그걸로 한 채널을 고르면
    사용자가 지목하지도 않은 채널만 근거가 된다."""
    assert tg.detect_channel("리서치 자료 좀 정리해줘") == (None, None)


def test_two_channels_at_once_is_treated_as_no_choice(chfile):
    """둘을 지목했는데 하나만 고르면, 나머지 하나는 말없이 빠진 근거가 된다."""
    chfile.write_text("@a  # 잠실개미\n@b  # 여의도 톺아보기\n", encoding="utf-8")
    assert tg.detect_channel("잠실개미랑 여의도 둘 다 뭐래?") == (None, None)
    assert tg.detect_channel("잠실개미 뭐래?")[0] == "@a"


def test_detection_actually_changes_the_evidence(chfile, db, monkeypatch):
    """인식만 하고 검색을 안 좁히면 아무 의미가 없다 — 실제 근거가 바뀌는지 본다."""
    from agent import muse

    _put([("@jake8lee", 1, _ago(1), "반도체 좋다"), ("@hedgehara", 1, _ago(1), "반도체 나쁘다")])
    monkeypatch.setattr(muse, "_chat", lambda *a, **k: "요약")
    evs = list(muse.answer("잠실개미 채널에서 반도체 뭐래?"))
    scope = next(e for e in evs if e["type"] == "scope")
    sources = next(e for e in evs if e["type"] == "sources")
    assert scope["channel"] == "@jake8lee" and scope["auto"] is True
    assert {p["channel"] for p in sources["posts"]} == {"@jake8lee"}


def test_an_explicit_filter_wins_over_detection(chfile, db, monkeypatch):
    """화면에서 채널을 골랐으면 그게 사용자의 뜻이다 — 질문 문구가 뒤집으면 안 된다."""
    from agent import muse

    _put([("@jake8lee", 1, _ago(1), "반도체 좋다"), ("@hedgehara", 1, _ago(1), "반도체 나쁘다")])
    monkeypatch.setattr(muse, "_chat", lambda *a, **k: "요약")
    evs = list(muse.answer("잠실개미 채널에서 반도체 뭐래?", channel="@hedgehara"))
    assert next(e for e in evs if e["type"] == "scope")["channel"] == "@hedgehara"


# ── 후속 질문 ─────────────────────────────────────────────────────
def test_a_bare_followup_reuses_the_previous_question():
    from agent import muse

    hist = [{"role": "user", "content": "HBM 공급 얘기"}, {"role": "assistant", "content": "…"}]
    assert muse._search_query("그거 왜 그래?", hist) == "HBM 공급 얘기 그거 왜 그래?"


def test_a_new_question_is_not_polluted_by_the_last_one():
    """모든 짧은 문장에 직전 질문을 붙이면 엉뚱한 글이 딸려온다."""
    from agent import muse

    hist = [{"role": "user", "content": "HBM 공급 얘기"}]
    assert muse._search_query("환율 전망은 어때?", hist) == "환율 전망은 어때?"


def test_the_first_question_has_nothing_to_reuse():
    from agent import muse

    assert muse._search_query("그거 왜?", []) == "그거 왜?"


def test_followup_finds_what_the_bare_words_cannot(chfile, db, monkeypatch):
    from agent import muse

    _put([("@a", 1, _ago(1), "HBM 공급 확대 전망")])
    monkeypatch.setattr(muse, "_chat", lambda *a, **k: "요약")
    prior = [{"role": "user", "content": "HBM 공급 얘기"}, {"role": "assistant", "content": "…"}]
    evs = list(muse.answer("그거 자세히", prior))
    assert any(e["type"] == "sources" and e["posts"] for e in evs)


# ── 브리핑 ────────────────────────────────────────────────────────
def test_recent_returns_newest_first_without_a_query(db):
    _put([("@a", 1, _ago(5), "옛 글"), ("@b", 1, _ago(0.1), "새 글")])
    assert [r["channel"] for r in tg.recent()] == ["@b", "@a"]


def test_recent_collapses_reposts(db):
    """브리핑에 같은 글이 세 번 실리면 그 주제가 세 배 커 보인다."""
    _put([("@a", 1, _ago(1), "HBM 공급 확대 전망입니다"),
          ("@b", 1, _ago(2), "HBM 공급 확대 전망입니다"),
          ("@c", 1, _ago(3), "환율 이야기")])
    assert len(tg.recent()) == 2


def test_brief_says_what_to_do_when_nothing_is_collected(db, chfile):
    from agent import muse

    evs = list(muse.brief())
    assert "수집" in next(e for e in evs if e["type"] == "final")["text"]


def test_brief_reads_recent_posts_not_a_search(db, chfile, monkeypatch):
    from agent import muse

    _put([("@a", 1, _ago(1), "환율 이야기"), ("@b", 1, _ago(2), "반도체 이야기")])
    monkeypatch.setattr(muse, "_chat", lambda *a, **k: "브리핑")
    evs = list(muse.brief())
    assert {p["channel"] for p in next(e for e in evs if e["type"] == "sources")["posts"]} == {
        "@a", "@b"}
    assert next(e for e in evs if e["type"] == "final")["text"] == "브리핑"


# ── 글 정리 ───────────────────────────────────────────────────────
def test_boilerplate_disclaimers_are_dropped():
    """수백 건 × 같은 면책 문구는 내용이 아니라 토큰이다."""
    out = tg.clean_text("삼성전자 실적 좋다\n매수-매도 등의 투자권유가 아니며 책임지지 않습니다.")
    assert "투자권유" not in out and "삼성전자 실적 좋다" in out


def test_divider_lines_are_dropped():
    out = tg.clean_text("제목\n────────\n본문")
    assert "─" not in out and "제목" in out and "본문" in out


# ── 채널 추가·제거 ────────────────────────────────────────────────
def test_adding_a_channel_keeps_the_alias(chfile):
    assert tg.add_channel("https://t.me/newone", "새채널") is True
    assert "@newone" in tg.channels()
    assert tg.aliases()["@newone"] == "새채널"


def test_removing_a_channel_also_removes_its_posts(chfile, db):
    """목록에서 뺐는데 검색에 계속 잡히면 뺀 게 아니다."""
    _put([("@jake8lee", 1, _ago(1), "반도체 좋다"), ("@plain", 1, _ago(1), "반도체 나쁘다")])
    assert tg.remove_channel("@jake8lee") is True
    assert "@jake8lee" not in tg.channels()
    assert [r["channel"] for r in tg.search("반도체")] == ["@plain"]


def test_removing_keeps_the_other_lines_intact(chfile):
    tg.remove_channel("@plain")
    assert tg.channels() == ["@jake8lee", "@hedgehara", "1208429502"]
    assert tg.aliases()["@hedgehara"] == "Pluto Research"


def test_removing_something_that_is_not_there_is_a_no_op(chfile):
    assert tg.remove_channel("@nope") is False
    assert len(tg.channels()) == 4


def test_the_channel_list_survives_a_redeploy(tmp_path, monkeypatch):
    """Railway 컨테이너는 재배포마다 갈아엎힌다 — 목록은 볼륨에 있어야 한다.
    리포지토리 파일은 씨앗일 뿐이고, 볼륨 사본이 생긴 뒤로는 그쪽이 진짜다."""
    seed = tmp_path / "seed.txt"
    seed.write_text("@one  # 하나\n", encoding="utf-8")
    live = tmp_path / "vol" / "muse_channels.txt"
    monkeypatch.setattr(tg, "CHANNELS_SEED", seed)
    monkeypatch.setattr(tg, "CHANNELS_FILE", live)

    assert tg.channels() == ["@one"]          # 첫 접근 — 씨앗이 복사된다
    tg.add_channel("@two", "둘")
    seed.write_text("@one  # 하나\n", encoding="utf-8")   # 배포로 씨앗이 되돌아와도
    assert tg.channels() == ["@one", "@two"]              # 추가한 채널은 남는다


# ── HTTP 권한 경계 ────────────────────────────────────────────────
@pytest.fixture
def team(monkeypatch):
    monkeypatch.setenv("APP_USERS", "boss:pw-a,member:pw-b")
    monkeypatch.delenv("ADMIN_USERS", raising=False)


def _client(name: str, pw: str) -> TestClient:
    c = TestClient(app)
    assert c.post("/api/login", json={"name": name, "password": pw}).status_code == 200
    return c


def test_only_an_admin_can_add_or_remove_channels(team, chfile, db):
    """채널을 바꾸면 팀 전원의 근거가 바뀐다 — 아무나 건드리게 두지 않는다."""
    member = _client("member", "pw-b")
    assert member.post("/api/muse/channels", json={"channel": "@x"}).status_code == 403
    assert member.delete("/api/muse/channels/@jake8lee").status_code == 403
    assert "@jake8lee" in tg.channels()


def test_an_admin_adds_a_channel_and_it_starts_reading(team, chfile, db, monkeypatch):
    """추가하고 다음 정기 수집까지 기다리면, 방금 넣은 채널이 검색에 안 나와
    추가가 실패한 것처럼 보인다."""
    started = []
    monkeypatch.setattr(tg, "collect_in_background", lambda only=None: started.append(only) or True)
    r = _client("boss", "pw-a").post(
        "/api/muse/channels", json={"channel": "https://t.me/newone", "alias": "새채널"})
    assert r.status_code == 200 and r.json()["channel"] == "@newone"
    assert started == ["@newone"], "새 채널만 읽어야 한다 — 40개를 다시 읽을 이유가 없다"


def test_adding_a_duplicate_says_so_instead_of_silently_passing(team, chfile, db):
    r = _client("boss", "pw-a").post("/api/muse/channels", json={"channel": "@jake8lee"})
    assert r.status_code == 409


def test_admins_see_the_management_controls_and_others_do_not(team, chfile, db):
    assert _client("boss", "pw-a").get("/api/muse/status").json()["can_manage"] is True
    assert _client("member", "pw-b").get("/api/muse/status").json()["can_manage"] is False


def test_brief_requires_login(team):
    assert TestClient(app).post("/api/muse/brief", json={}).status_code == 401


def test_brief_and_ask_keep_the_same_separate_history(team, chfile, db, monkeypatch):
    """브리핑도 muse 기록에 남아야 한다 — 나중에 열었을 때 이 답이 어디서 왔는지
    알아야 하고, 본 채팅 기록에 섞이면 안 된다."""
    from agent import muse

    _put([("@a", 1, _ago(1), "환율 이야기")])
    monkeypatch.setattr(muse, "_chat", lambda *a, **k: "브리핑")
    c = _client("boss", "pw-a")
    assert c.post("/api/muse/brief", json={}).status_code == 200
    assert len(c.get("/api/muse/sessions").json()["sessions"]) >= 1
    assert c.get("/api/sessions").json()["sessions"] == []


def test_reading_one_new_channel_does_not_postpone_the_full_refresh(chfile, db, monkeypatch):
    """한 채널만 읽고 수집 시각을 찍으면, 나머지 40개가 방금 갱신된 것처럼 보여
    정기 재수집이 STALE_HOURS 만큼 미뤄진다."""
    import telethon
    import telethon.sessions

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        def iter_messages(self, entity, limit=None):
            async def _none():
                return
                yield  # pragma: no cover — 빈 async 이터레이터를 만들기 위한 형식

            return _none()

    monkeypatch.setattr(telethon, "TelegramClient", FakeClient)
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda s=None: s)
    monkeypatch.setattr(tg, "_creds", lambda: (1, "hash", "session"))

    tg.collect(only="@jake8lee")
    assert tg.is_stale() is True, "채널 하나만 읽은 것으로 전체가 최신이 되면 안 된다"
    tg.collect()
    assert tg.is_stale() is False

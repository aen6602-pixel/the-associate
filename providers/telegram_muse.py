"""Market Muse — 텔레그램 채널을 **직접 읽어** 저장하고 검색한다.

⚠️ 이 소스는 공신력이 없다. 공시가 아니고, 정정·삭제 이력이 없으며, 작성자를 검증할 수
없다. 그래서 `/muse` 화면에서만 쓰이고 **밸류에이션 도구(agent/registry.py)에는 등록하지
않는다**. 여기서 본 수치를 평가에 쓰려면 사람이 공시로 확인한 뒤 가정으로 옮겨 적어야 한다.

인증에 대해: 채널 글은 **봇 토큰으로 못 읽는다**(봇은 자신이 속한 대화만 본다). 사람 계정
세션(MTProto)이 필요하고, 그 로그인은 전화번호로 받는 코드를 넣어야 해서 서버에서
자동화할 수 없다. 그래서 로컬에서 `_muse_login.py` 로 세션 문자열을 한 번 만들고
`TG_SESSION_STRING` 으로 넘긴다. 이 문자열은 **계정 접근 권한 그 자체**이므로 로그·에러
메시지에 절대 싣지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config
from core.schema import DataError

DB_PATH = config.DATA_DIR / "muse.db"
# 목록은 볼륨에 둔다 — 화면에서 채널을 더하고 빼도 재배포에 지워지지 않아야 한다.
# 리포지토리의 파일은 **씨앗**이고, 볼륨에 아직 없을 때 한 번 복사된다.
CHANNELS_FILE = config.DATA_DIR / "muse_channels.txt"
CHANNELS_SEED = config.ROOT / "muse_channels.txt"

# 한 번 수집할 때 채널당 최대 글 수 / 거슬러 올라갈 기간.
# 채널이 40개 × 500건이면 2만 건 — LIKE 스캔으로 충분한 규모다(형태소 분석 불필요).
PER_CHANNEL = int(os.getenv("MUSE_PER_CHANNEL", "500"))
LOOKBACK_DAYS = int(os.getenv("MUSE_LOOKBACK_DAYS", "21"))
# 이 시간이 지나면 낡은 것으로 보고 다시 수집한다(화면 진입 시 백그라운드로).
STALE_HOURS = float(os.getenv("MUSE_STALE_HOURS", "6"))


# ── 저장소 ────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            channel TEXT NOT NULL,
            msg_id  INTEGER NOT NULL,
            date    TEXT NOT NULL,          -- ISO8601 UTC
            text    TEXT NOT NULL,
            PRIMARY KEY (channel, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return c


def _meta_get(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ── 채널 목록 ────────────────────────────────────────────────────
# 한 줄에 하나. '#' 뒤는 **별칭**으로 쓴다 — 화면에는 `@jake8lee` 가 아니라 '잠실개미' 로
# 보여야 하고, "잠실개미 채널에서 뭐래?" 같은 질문도 알아들어야 하기 때문이다.
def _channels_path() -> Path:
    """볼륨 사본을 쓰되, 없으면 리포지토리 씨앗을 한 번 복사한다."""
    if not CHANNELS_FILE.exists() and CHANNELS_SEED.exists() and CHANNELS_FILE != CHANNELS_SEED:
        CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNELS_FILE.write_text(CHANNELS_SEED.read_text(encoding="utf-8"), encoding="utf-8")
    return CHANNELS_FILE


def normalize(token: str) -> str:
    """입력을 DB 의 channel 컬럼과 같은 표준형으로: '@아이디' 또는 숫자 문자열.

    사람이 붙여넣는 건 대개 `https://t.me/foo` 인데 Telethon 에도, 기존 DB 행에도
    그 형태는 없다. 여기서 한 번 걸러야 같은 채널이 두 줄로 쌓이지 않는다."""
    t = token.split("#", 1)[0].strip()
    if t.startswith("http://") or t.startswith("https://"):
        slug = t.rstrip("/").rsplit("/", 1)[-1]
        t = slug if slug.startswith("+") else "@" + slug
    if re.fullmatch(r"-?\d+", t):
        return t
    if t and not t.startswith(("@", "+")):
        t = "@" + t
    return t


def _lines() -> list[tuple[str, str]]:
    """[(표준형 채널, 별칭), ...] — 주석/빈 줄 제외."""
    path = _channels_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        body, _, alias = line.partition("#")
        body = body.strip()
        if body:
            out.append((normalize(body), alias.strip()))
    return out


def channels() -> list[str]:
    """구독 채널 목록(표준형)."""
    return [ch for ch, _ in _lines()]


def aliases() -> dict[str, str]:
    """{채널: 별칭}. 별칭이 없으면 항목 자체가 없다."""
    return {ch: alias for ch, alias in _lines() if alias}


def label(ch: str) -> str:
    """화면·프롬프트에 쓸 이름. 별칭이 있으면 '별칭(@아이디)'."""
    alias = aliases().get(ch)
    return f"{alias} ({ch})" if alias else ch


def add_channel(token: str, alias: str = "") -> bool:
    """목록에 추가. 이미 있으면 False. (실제 접근 가능 여부는 수집 때 드러난다)"""
    norm = normalize(token)
    if not norm or norm in channels():
        return False
    path = _channels_path()
    prev = path.read_text(encoding="utf-8").rstrip("\n") if path.exists() else ""
    line = f"{norm:<24} # {alias.strip()}" if alias.strip() else norm
    path.write_text(f"{prev}\n{line}\n", encoding="utf-8")
    return True


def remove_channel(token: str) -> bool:
    """목록에서 제거. 이미 모아둔 글도 함께 지운다 — 목록에서 뺐는데 검색에 계속
    잡히면 뺀 게 아니다."""
    path = _channels_path()
    if not path.exists():
        return False
    norm = normalize(token)
    kept, hit = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        body = line.split("#", 1)[0].strip()
        if body and normalize(body) == norm:
            hit = True
            continue
        kept.append(line)
    if not hit:
        return False
    path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    with _conn() as c:
        c.execute("DELETE FROM posts WHERE channel = ?", (norm,))
    return True


# 별칭에서 채널을 특정하기엔 너무 흔해서 오탐을 부르는 낱말들.
_CH_STOP = {
    "주식", "투자", "시장", "리서치", "뉴스", "채널", "정보", "분석", "종목", "이야기",
    "경제", "오늘", "요약", "정리", "research", "news", "invest", "stock", "market",
    "미국", "한국", "글로벌", "일본", "중국", "korean", "stocks", "증권", "관심",
}
_CH_SPLIT_RE = re.compile(r"[\s&/|()·,.\-]+")


def detect_channel(text: str) -> tuple[str | None, str | None]:
    """질문이 특정 채널을 지목하는지 본다 → (채널, 별칭) 또는 (None, None).

    두 개 이상 걸리면 어느 쪽인지 알 수 없으므로 전체 검색으로 둔다 — 임의로 하나를
    고르면 사용자가 지목하지도 않은 채널만 근거로 답하게 된다."""
    tl = (text or "").lower()
    pairs = _lines()
    for ch, _alias in pairs:                      # 1) '@아이디' 를 직접 쓴 경우 (확실)
        if ch.startswith("@") and ch.lower() in tl:
            return ch, aliases().get(ch)
    hits = {}
    for ch, alias in pairs:                       # 2) 별칭의 변별력 있는 토큰
        if not alias:
            continue
        for tok in _CH_SPLIT_RE.split(alias):
            tok = tok.strip()
            if len(tok) >= 3 and tok.lower() not in _CH_STOP and tok.lower() in tl:
                hits[ch] = alias
                break
    if len(hits) == 1:
        ch, alias = next(iter(hits.items()))
        return ch, alias
    return None, None

def stats() -> dict:
    alias_map = aliases()
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
        chans = c.execute("SELECT channel, COUNT(*) n, MAX(date) last FROM posts "
                          "GROUP BY channel ORDER BY n DESC").fetchall()
    return {
        "count": n,
        "channels": [{"id": r["channel"], "alias": alias_map.get(r["channel"], ""),
                      "n": r["n"], "last": r["last"]} for r in chans],
        "configured_channels": [{"id": ch, "alias": al} for ch, al in _lines()],
        "configured": len(channels()),
        "collected_at": _meta_get("collected_at"),
        "last_error": _meta_get("last_error"),
        "lookback_days": LOOKBACK_DAYS,
    }


def is_stale() -> bool:
    at = _meta_get("collected_at")
    if not at:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
    except ValueError:
        return True
    return age > STALE_HOURS * 3600


# ── 수집 (Telethon) ──────────────────────────────────────────────
_lock = threading.Lock()
_running = {"on": False, "note": ""}


def collect_status() -> dict:
    return {"running": _running["on"], "note": _running["note"]}


def _creds() -> tuple[int, str, str]:
    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    session = os.getenv("TG_SESSION_STRING", "").strip()
    missing = [n for n, v in (("TG_API_ID", api_id), ("TG_API_HASH", api_hash),
                              ("TG_SESSION_STRING", session)) if not v]
    if missing:
        raise DataError(
            f"텔레그램 설정이 없습니다: {', '.join(missing)}. "
            f"my.telegram.org 에서 api_id/api_hash 를 받고, `python _muse_login.py` 로 "
            f"세션 문자열을 만들어 .env 에 넣으세요(.env.example 참고).")
    if not api_id.isdigit():
        raise DataError("TG_API_ID 는 숫자여야 합니다.")
    return int(api_id), api_hash, session


async def _collect_async(only: str | None = None) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, session = _creds()
    targets = [only] if only else channels()
    if not targets:
        raise DataError(f"채널 목록이 비어 있습니다: {CHANNELS_FILE.name} 를 채워주세요.")

    names = aliases()
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    added, errors = 0, []

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise DataError("텔레그램 세션이 만료됐습니다. `python _muse_login.py` 로 다시 만드세요.")

        # 공개 아이디가 없는 채널은 숫자 id 로만 부를 수 있는데, Telethon 은 그 숫자를
        # 캐시에서 못 찾으면 **사용자 id 로 해석해** 실패한다("Could not find the input
        # entity for PeerUser"). 대화목록을 한 번 훑으면 캐시가 채워져 해결된다 —
        # 세션 문자열은 매번 새로 만들어지므로 수집 때마다 한 번씩 필요하다.
        if any(re.fullmatch(r"-?\d+", t) for t in targets):
            _running["note"] = "대화목록 확인 중…"
            async for _ in client.iter_dialogs():
                pass

        for ch in targets:
            _running["note"] = f"{names.get(ch) or ch} 읽는 중…"
            try:
                # 채널 식별자는 '@핸들' 또는 숫자 id 로 온다. Telethon 은 숫자를 int 로 받는다.
                entity = int(ch) if re.fullmatch(r"-?\d+", ch) else ch
                rows = []
                async for m in client.iter_messages(entity, limit=PER_CHANNEL):
                    if m.date and m.date < since:
                        break        # 최신순이라 기간을 벗어나면 그 채널은 끝
                    text = (m.message or "").strip()
                    if text:
                        rows.append((ch, m.id, m.date.astimezone(timezone.utc).isoformat(), text))
                with _conn() as c:
                    before = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
                    c.executemany(
                        "INSERT OR IGNORE INTO posts(channel,msg_id,date,text) VALUES(?,?,?,?)",
                        rows)
                    after = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
                added += after - before
            except Exception as e:  # noqa: BLE001 — 채널 하나가 막혀도 나머지는 계속 모은다
                errors.append(f"{names.get(ch) or ch}: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    # 기간이 지난 글은 버린다 — 무한히 쌓이면 검색이 옛 글로 오염된다.
    with _conn() as c:
        c.execute("DELETE FROM posts WHERE date < ?", (since.isoformat(),))

    # 채널 하나만 읽은 경우(방금 추가한 채널)에는 시각을 찍지 않는다 — 찍으면 나머지
    # 40개가 방금 갱신된 것처럼 보여 정기 재수집이 STALE_HOURS 만큼 미뤄진다.
    if not only:
        _meta_set("collected_at", datetime.now(timezone.utc).isoformat())
    _meta_set("last_error", " / ".join(errors[:5]) if errors else "")
    return {"added": added, "errors": errors, "channels": len(targets)}


def collect(only: str | None = None) -> dict:
    """동기 래퍼. 이미 돌고 있으면 겹쳐 돌리지 않는다(텔레그램 rate-limit 방지)."""
    if not _lock.acquire(blocking=False):
        raise DataError("이미 수집 중입니다. 잠시 뒤 다시 시도하세요.")
    _running.update(on=True, note="시작")
    try:
        return asyncio.run(_collect_async(only))
    finally:
        _running.update(on=False, note="")
        _lock.release()


def collect_in_background(only: str | None = None) -> bool:
    """화면을 막지 않고 수집한다. 이미 돌고 있으면 False."""
    if _running["on"]:
        return False

    def run():
        try:
            collect(only)
        except Exception as e:  # noqa: BLE001 — 백그라운드라 올릴 곳이 없다 → meta 에 남긴다
            _meta_set("last_error", f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True, name="muse-collect").start()
    return True


# ── 검색 ─────────────────────────────────────────────────────────
# 한국어 주식 대화는 같은 대상을 여러 표기로 부른다("삼성전자/삼전/005930").
# 형태소 분석기 없이도 이 정도는 잡아야 검색이 쓸모 있다.
_SYNONYM_SEED = {
    "삼성전자": ["삼전", "005930"],
    "sk하이닉스": ["하이닉스", "에스케이하이닉스", "000660"],
    "엔비디아": ["nvidia", "nvda"],
    "테슬라": ["tesla", "tsla"],
    "애플": ["apple", "aapl"],
    "마이크로소프트": ["microsoft", "msft"],
    "구글": ["google", "알파벳", "googl"],
    "아마존": ["amazon", "amzn"],
    "엘지에너지솔루션": ["lg에너지솔루션", "엘지엔솔", "lg엔솔", "373220"],
    "이차전지": ["2차전지", "배터리"],
    "반도체": ["semiconductor", "칩"],
    "에이치비엠": ["hbm", "고대역폭메모리"],
    "디램": ["dram", "d램"],
    "금리": ["기준금리", "이자율"],
    "환율": ["원달러", "달러환율"],
    "연준": ["fed", "연방준비제도", "fomc"],
    "코스피": ["kospi"],
    "코스닥": ["kosdaq"],
    "원전": ["원자력", "nuclear"],
    "방산": ["방위산업", "defense"],
    "조선": ["shipbuilding"],
    "바이오": ["bio", "제약"],
    "인공지능": ["ai"],
}


def _build_synonyms(seed: dict) -> dict:
    syn: dict[str, set] = {}
    for key, vals in seed.items():
        group = {key, *vals}
        for t in group:
            syn.setdefault(t.lower(), set()).update(g for g in group if g.lower() != t.lower())
    return {k: sorted(v) for k, v in syn.items()}


_SYNONYMS = _build_synonyms(_SYNONYM_SEED)
_URL_RE = re.compile(r"https?://\S+")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MULTINL_RE = re.compile(r"\n{3,}")
# 채널 글 대부분이 같은 구분선과 면책 문구를 달고 온다 — 수백 건을 프롬프트에 넣으면
# 그 반복만으로 토큰이 꽤 나간다. 내용이 아니므로 걷어낸다.
_DIVIDER_RE = re.compile(r"^[\s─=_–—\-]{4,}$", re.M)
_DISCLAIMER_RES = [
    re.compile(r"매수[-–]?매도.{0,12}투자권유.{0,40}않습니다\.?"),
    re.compile(r"해당 게시물의 내용은 부정확할 수 있으며.{0,50}책임입니다\.?"),
    re.compile(r"(해당 게시물의 내용은 )?어떤 경우에도 법적 근거로 사용될 수 없습니다\.?"),
    re.compile(r"Not a financial advice", re.I),
]


def clean_text(text: str) -> str:
    """URL·마크다운 기호·구분선·면책 문구·과한 공백만 걷어낸다. 내용은 건드리지 않는다."""
    t = _URL_RE.sub("", text or "")
    for r in _DISCLAIMER_RES:
        t = r.sub("", t)
    t = t.replace("***", "").replace("**", "").replace("*", "")
    t = _DIVIDER_RE.sub("", t)
    t = _MULTINL_RE.sub("\n\n", _MULTISPACE_RE.sub(" ", t))
    return t.strip()


def _expand(word: str) -> list[str]:
    out = [word]
    for s in _SYNONYMS.get(word.lower(), []):
        if s not in out:
            out.append(s)
    return out


def _recency_bonus(date_iso: str) -> float:
    """최신 글 가산점. 키워드 1개 매칭(=1점)과 견줄 크기로 둔다 —
    오늘 글이 2주 전 글(키워드 2개 매칭)보다 앞서야 대화가 쓸모 있다."""
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(date_iso)).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0
    for limit, bonus in ((1, 3.0), (3, 2.0), (7, 1.0), (14, 0.5)):
        if age <= limit:
            return bonus
    return 0.0


def _dedup_key(text: str) -> str:
    """채널 간 '퍼나르기' 가 잦다 — 정리 후 앞부분이 같으면 같은 글로 본다."""
    return re.sub(r"\s+", "", clean_text(text))[:300]


def search(query: str, limit: int = 30, channel: str | None = None) -> list[dict]:
    """관련도(동의어 그룹 매칭 수) + 최신성으로 뽑고 중복을 없앤다."""
    words = [w for w in re.split(r"\s+", (query or "").strip()) if len(w) >= 2]
    args: list = []
    with _conn() as c:
        if not words:
            sql = "SELECT channel,date,text,0 AS score FROM posts"
            where = []
        else:
            groups = [_expand(w) for w in words]
            score, where = [], []
            for terms in groups:
                ors = " OR ".join(["text LIKE ?"] * len(terms))
                score.append(f"(CASE WHEN ({ors}) THEN 1 ELSE 0 END)")
                args.extend(f"%{t}%" for t in terms)
            for terms in groups:
                where.append("(" + " OR ".join(["text LIKE ?"] * len(terms)) + ")")
                args.extend(f"%{t}%" for t in terms)
            sql = (f"SELECT channel,date,text,({' + '.join(score)}) AS score FROM posts "
                   f"WHERE ({' OR '.join(where)})")
            where = ["ok"]
        if channel:
            sql += (" AND" if where else " WHERE") + " channel = ?"
            args.append(channel)
        sql += " ORDER BY score DESC, date DESC LIMIT ?"
        args.append(limit * 5)
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]

    rows.sort(key=lambda r: r["score"] + _recency_bonus(r["date"]), reverse=True)
    seen, out = set(), []
    for r in rows:
        k = _dedup_key(r["text"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return sorted(out, key=lambda r: r["date"], reverse=True)   # 프롬프트엔 최신부터


def recent(limit: int = 300, channel: str | None = None) -> list[dict]:
    """질문 없이 '최근 무슨 얘기가 도는지' 볼 때 쓴다(브리핑). 중복은 없애고 최신순."""
    with _conn() as c:
        sql = "SELECT channel,date,text,0 AS score FROM posts"
        args: list = []
        if channel:
            sql += " WHERE channel = ?"
            args.append(channel)
        sql += " ORDER BY date DESC LIMIT ?"
        args.append(limit * 3)
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    seen, out = set(), []
    for r in rows:
        k = _dedup_key(r["text"])
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out

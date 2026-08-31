"""Market Muse — 구독 중인 텔레그램 채널 글 모음(martin-maketmuse 프로젝트의 스냅샷).

⚠️ **이 소스는 공신력이 없다.** 1차 공시가 아니고, 정정·삭제 이력이 없으며, 작성자를
검증할 수 없다. 그래서 등급을 `market_chatter` 로 따로 두고, **본 채팅의 도구로는 노출하지
않는다**(agent/registry.py 에 등록하지 않는다). 여기서 얻은 내용을 밸류에이션에 쓰려면
사람이 명시적으로 가정(assumption)으로 옮겨 적어야 한다.

데이터 경로: martin-maketmuse 저장소의 GitHub Actions 가 하루 2회 채널을 수집해
`web/api/_data/snapshot.json` 을 커밋한다. 여기서는 그 파일만 읽는다 —
텔레그램 수집(MTProto 세션·상시 프로세스)은 그 프로젝트의 몫으로 남긴다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from core import config
from core.cache import TTL_FRESH, ttl_cache
from core.http import probe
from core.schema import DataError

# 스냅샷 위치. 저장소가 비공개라 raw.githubusercontent 로는 못 읽고, Contents API 에
# Accept: raw 를 주면 토큰으로 원문을 받을 수 있다(1MB 제한이 없는 경로).
REPO = os.getenv("MUSE_REPO", "aen6602-pixel/martin-maketmuse")
SNAPSHOT_PATH = os.getenv("MUSE_SNAPSHOT_PATH", "web/api/_data/snapshot.json")
_API = f"https://api.github.com/repos/{REPO}/contents/{SNAPSHOT_PATH}"


def _token() -> str:
    return config.require(config.Keys.MUSE_GITHUB, "MUSE_GITHUB_TOKEN")


# 스냅샷은 하루 2회만 갱신된다 — 6시간 캐시면 충분하고, 7.5MB 를 매 질문마다 받지 않는다.
@ttl_cache(TTL_FRESH, maxsize=1)
def snapshot() -> dict:
    r = probe("GET", _API, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }, timeout=60)
    try:
        d = json.loads(r.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise DataError(f"Market Muse 스냅샷을 해석하지 못했습니다: {e}") from None
    if not isinstance(d, dict) or "posts" not in d:
        raise DataError("Market Muse 스냅샷 형식이 예상과 다릅니다(posts 없음).")
    return d


# ── 검색 (martin-maketmuse/db.py 의 의미를 그대로 옮김) ─────────────
# 원본은 SQLite LIKE 질의였다. 여기서는 스냅샷(메모리 리스트)이 대상이라 같은 점수식을
# 파이썬으로 계산한다 — 동의어 그룹 매칭 1점 + 최신성 가산 + 중복 제거.
_SYNONYM_SEED = {
    "삼성전자": ["삼전", "005930"],
    "sk하이닉스": ["하이닉스", "에스케이하이닉스", "000660"],
    "엔비디아": ["nvidia", "nvda"],
    "테슬라": ["tesla", "tsla"],
    "애플": ["apple", "aapl"],
    "마이크로소프트": ["microsoft", "msft", "ms"],
    "구글": ["google", "알파벳", "googl"],
    "아마존": ["amazon", "amzn"],
    "엘지에너지솔루션": ["lg에너지솔루션", "엘지엔솔", "lg엔솔", "373220"],
    "이차전지": ["2차전지", "배터리", "secondary battery"],
    "반도체": ["semiconductor", "칩", "chip"],
    "에이치비엠": ["hbm", "고대역폭메모리"],
    "디램": ["dram", "d램"],
    "금리": ["기준금리", "이자율", "rate"],
    "환율": ["원달러", "달러환율", "exchange rate"],
    "연준": ["fed", "연방준비제도", "fomc"],
    "코스피": ["kospi"],
    "코스닥": ["kosdaq"],
    "원전": ["원자력", "nuclear"],
    "방산": ["방위산업", "defense"],
    "조선": ["shipbuilding"],
    "바이오": ["bio", "제약"],
    "인공지능": ["ai", "에이아이"],
}


def _build_synonyms(seed: dict) -> dict:
    syn: dict[str, set] = {}
    for key, vals in seed.items():
        group = {key, *vals}
        for term in group:
            syn.setdefault(term.lower(), set()).update(
                g for g in group if g.lower() != term.lower())
    return {k: sorted(v) for k, v in syn.items()}


_SYNONYMS = _build_synonyms(_SYNONYM_SEED)

_URL_RE = re.compile(r"https?://\S+")
_DIVIDER_RE = re.compile(r"^[\s\-=_·•]{3,}$", re.M)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MULTINL_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """분석에 불필요한 토큰(URL·마크다운·구분선·과한 공백)을 제거. 내용은 보존한다."""
    t = _URL_RE.sub("", text or "")
    t = t.replace("***", "").replace("**", "").replace("*", "")
    t = _DIVIDER_RE.sub("", t)
    t = _MULTISPACE_RE.sub(" ", t)
    t = _MULTINL_RE.sub("\n\n", t)
    return t.strip()


def _expand(word: str) -> list[str]:
    terms = [word]
    for s in _SYNONYMS.get(word.lower(), []):
        if s not in terms:
            terms.append(s)
    return terms


def _recency_bonus(date_iso: str) -> float:
    """최신 글 가산점. 키워드 1개 매칭(=1점)과 견줄 크기 — 오늘 글은 2주 전 글보다 앞선다."""
    try:
        dt = datetime.fromisoformat(date_iso)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0
    for limit, bonus in ((1, 3.0), (3, 2.0), (7, 1.0), (14, 0.5)):
        if age <= limit:
            return bonus
    return 0.0


def _dedup_key(text: str) -> str:
    """중복 판정용 — 채널 간 '퍼나르기' 가 많아 정리 후 앞부분이 같으면 같은 글로 본다."""
    return re.sub(r"\s+", "", clean_text(text))[:300]


def channels() -> list[dict]:
    return [{"id": c[0], "name": c[1] if len(c) > 1 else c[0]}
            for c in snapshot().get("channels", [])]


def search(query: str, limit: int = 30, channel: str | None = None) -> list[dict]:
    """질문과 관련된 채널 글. 관련도(동의어 그룹 매칭 수) + 최신성으로 뽑고 중복을 없앤다."""
    posts = snapshot().get("posts", [])
    if channel:
        posts = [p for p in posts if p.get("channel") == channel]
    words = [w for w in re.split(r"\s+", (query or "").strip()) if len(w) >= 2]
    if not words:
        rows = sorted(posts, key=lambda p: p.get("date", ""), reverse=True)[:limit * 5]
    else:
        groups = [[t.lower() for t in _expand(w)] for w in words]
        rows = []
        for p in posts:
            low = (p.get("text") or "").lower()
            score = sum(1 for g in groups if any(t in low for t in g))
            if score:
                rows.append({**p, "score": score})
        rows.sort(key=lambda p: p["score"] + _recency_bonus(p.get("date", "")), reverse=True)
        rows = rows[:limit * 5]

    seen, out = set(), []
    for p in rows:
        k = _dedup_key(p.get("text", ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
        if len(out) >= limit:
            break
    # 프롬프트에 최신 글이 먼저 보이도록
    return sorted(out, key=lambda p: p.get("date", ""), reverse=True)


def ping() -> str:
    d = snapshot()
    return (f"채널 {len(d.get('channels', []))}개 · 글 {d.get('count', 0):,}건 "
            f"({str(d.get('generated_at', ''))[:10]} 수집)")

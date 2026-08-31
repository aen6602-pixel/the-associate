"""대화 세션 영속화 — dart-agent 의 reports/ 패턴(JSON 파일 1개=기록 1건, 목록·조회·삭제)을 그대로 따른다.

세션 1개 = 대화 스레드 1개(여러 질문-답변 turn 포함). 첫 메시지가 오가는 순간 파일이 생기고,
매 turn 마다 갱신된다. 왼쪽 사이드바에서 클릭하면 그 세션을 불러와 이어서 대화할 수 있다.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paths import ROOT, SESSIONS_DIR  # noqa: F401 — ROOT 는 기존 import 호환용


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _safe_id(sid: str) -> str:
    """경로 조작 방지 — dart-agent 의 safeId() 와 동일한 취지."""
    return sid if re.fullmatch(r"[\w.\-]+", str(sid or "")) else ""


def _user_dir(user_key: str) -> Path:
    """사용자별 세션 폴더. 멀티유저(배포) 시 남의 대화가 섞이지 않도록 격리한다.
    user_key 는 app 에서 로그인 이메일 해시(로컬은 'local')로 넘어온다."""
    uk = user_key if re.fullmatch(r"[\w.\-]+", str(user_key or "")) else "local"
    d = SESSIONS_DIR / uk
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid: str, user_key: str) -> Path | None:
    sid = _safe_id(sid)
    return _user_dir(user_key) / f"{sid}.json" if sid else None


def save_session(sid: str, messages: list[dict], user_key: str, title: str | None = None) -> None:
    """세션 전체를 덮어써 저장한다. messages 는 [{"role","content"}, ...]."""
    p = _path(sid, user_key)
    if p is None or not messages:
        return
    existing = load_session(sid, user_key)
    created_at = (existing or {}).get("created_at") or _now_iso()
    auto_title = title or (existing or {}).get("title") or _make_title(messages)
    rec = {
        "id": sid, "title": auto_title, "messages": messages,
        "created_at": created_at, "updated_at": _now_iso(),
    }
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


# 도구 → 제목에 쓸 방법 이름. **순서가 우선순위다** — 한 대화에서 베타·WACC·DCF 를 다
# 불렀다면 그 대화는 'DCF' 지 '베타' 가 아니다. 결론을 내는 방법이 위로 온다.
_TITLE_METHOD = [
    ("compute_scenarios", "DCF 시나리오"),
    ("diagnose_implied_assumptions", "목표가 역산"),
    ("compute_dcf", "DCF"),
    ("evaluate_sangjeung_value", "상증법"),
    ("compute_comps", "Comps"),
    ("compute_wacc_auto", "WACC"),
    ("compute_wacc", "WACC"),
    ("get_beta", "베타"),
    ("get_financial_history", "재무 시계열"),
]

# 질문 끝의 요청 표현. 제목에서는 정보가 없고 길이만 먹는다.
_FILLER = re.compile(
    r"(을|를|이|가|은|는|좀|한번|해서|에\s*대해서?)?\s*"
    r"(해\s?보고\s?싶은데|해\s?보고\s?싶어|하고\s?싶은데|하고\s?싶어|해\s?볼까|해\s?보자)?\s*"
    r"(알려\s?줘|알려주세요|해\s?줘|해주세요|해봐|해\s?줄래|뽑아\s?줘|보여\s?줘|구해\s?줘|"
    r"계산해\s?줘|만들어\s?줘|정리해\s?줘|찾아\s?줘|돌려\s?줘|가능해|가능한가요?|될까|되나요?|"
    r"어때|얼마야|얼마인가요?|궁금해|부탁해요?)?[.?!\s]*$")


def _from_trace(messages: list[dict]) -> str | None:
    """실제로 조회한 대상·방법으로 제목을 만든다.

    질문 원문을 자르면 "리노공업 DCF를 해보고 싶어…" 처럼 앞부분만 남아 목록에서 서로
    구분되지 않는다. 무엇을 어떤 방법으로 봤는지는 trace 에 이미 구조화돼 있다.
    """
    company, ran = None, set()
    for m in messages:
        for t in m.get("trace") or []:
            if not (t.get("result") or {}).get("ok"):
                continue
            company = company or (t.get("input") or {}).get("company")
            ran.add(t.get("name"))
    if not company:
        return None
    method = next((label for tool, label in _TITLE_METHOD if tool in ran), None)
    return f"{company} {method}" if method else str(company)


def _make_title(messages: list[dict]) -> str:
    """사이드바에서 한 줄로 보이도록 짧게. 우선 실제 작업(대상·방법)으로 짓고,
    도구를 안 쓴 대화만 질문 원문으로 대체한다."""
    from_trace = _from_trace(messages)
    if from_trace:
        return from_trace[:28]
    first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
    t = re.sub(r"\s+", " ", first_user).strip()
    t = _FILLER.sub("", t).strip(" ,·")
    return (t[:18] + "…") if len(t) > 18 else (t or "새 대화")


def load_session(sid: str, user_key: str) -> dict | None:
    p = _path(sid, user_key)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_sessions(user_key: str) -> list[dict]:
    """최신순. 미리보기(첫 질문)와 메타만 포함 — 목록 렌더용. 해당 사용자 폴더만 조회."""
    out = []
    for p in _user_dir(user_key).glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": rec.get("id"), "title": rec.get("title") or "새 대화",
            "n_turns": len(rec.get("messages", [])),
            "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
        })
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out


def delete_session(sid: str, user_key: str) -> bool:
    p = _path(sid, user_key)
    if p is None or not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False

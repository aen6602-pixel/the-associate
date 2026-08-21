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


def _make_title(messages: list[dict]) -> str:
    """사이드바에서 한 줄로 보이도록 짧게 자른다(긴 제목은 버튼에서 줄바꿈되어 목록이 늘어짐)."""
    first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
    t = re.sub(r"\s+", " ", first_user).strip()
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

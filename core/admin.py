"""관리자용 사용현황 집계 — 누가 무엇을 물었고 어떤 소스를 썼는지.

`core.history` 가 대화를 `DATA_DIR/sessions/<user_key>/*.json` 으로 저장하고, `user_key` 는
계정 이름의 해시다. 관리자 페이지는 `auth.user_key_for(name)` 로 이름→폴더를 되짚어
계정별로 묶는다(폴더 이름만 보고는 누구 것인지 알 수 없다).

집계는 저장된 파일을 읽기만 한다 — 별도 로그·DB 를 두지 않는다.
"""
from __future__ import annotations

from collections import Counter

from . import auth, history


def _tool_names(msg: dict) -> list[str]:
    return [t.get("name") for t in (msg.get("trace") or []) if t.get("name")]


def _session_stats(rec: dict) -> dict:
    msgs = rec.get("messages") or []
    questions = [m for m in msgs if m.get("role") == "user"]
    tools: list[str] = []
    failed = 0
    for m in msgs:
        for t in (m.get("trace") or []):
            if t.get("name"):
                tools.append(t["name"])
            if not (t.get("result") or {}).get("ok", True):
                failed += 1
    return {
        "id": rec.get("id"), "title": rec.get("title") or "새 대화",
        "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
        "questions": len(questions), "tool_calls": len(tools), "failed_calls": failed,
        "tools": tools,
        "first_question": (questions[0].get("content") if questions else None),
    }


def overview(recent_limit: int = 40) -> dict:
    """계정별 사용현황 + 최근 질문 타임라인 + 도구 사용 순위."""
    table = auth.users()
    names = [n for n in table if n]           # 공용 비밀번호(이름 "")는 집계 불가
    tool_counter: Counter = Counter()
    timeline: list[dict] = []
    users_out: list[dict] = []

    for name in names:
        key = auth.user_key_for(name)
        sessions = []
        for meta in history.list_sessions(key):
            rec = history.load_session(meta["id"], key)
            if rec is None:
                continue
            st = _session_stats(rec)
            sessions.append(st)
            tool_counter.update(st["tools"])
            for m in (rec.get("messages") or []):
                if m.get("role") == "user":
                    timeline.append({
                        "user": name, "at": rec.get("updated_at"),
                        "session_id": rec.get("id"), "question": m.get("content"),
                    })

        sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        users_out.append({
            "name": name,
            "is_admin": name in auth.admins(),
            "sessions": len(sessions),
            "questions": sum(s["questions"] for s in sessions),
            "tool_calls": sum(s["tool_calls"] for s in sessions),
            "failed_calls": sum(s["failed_calls"] for s in sessions),
            "last_active": sessions[0]["updated_at"] if sessions else None,
            "recent_sessions": [
                {k: v for k, v in s.items() if k != "tools"} for s in sessions[:12]
            ],
        })

    users_out.sort(key=lambda u: u.get("last_active") or "", reverse=True)
    timeline.sort(key=lambda t: t.get("at") or "", reverse=True)
    return {
        "users": users_out,
        "totals": {
            "users": len(users_out),
            "sessions": sum(u["sessions"] for u in users_out),
            "questions": sum(u["questions"] for u in users_out),
            "tool_calls": sum(u["tool_calls"] for u in users_out),
            "failed_calls": sum(u["failed_calls"] for u in users_out),
        },
        "tools": [{"name": n, "count": c} for n, c in tool_counter.most_common(20)],
        "timeline": timeline[:recent_limit],
    }


def user_session(name: str, session_id: str) -> dict | None:
    """특정 계정의 대화 한 건 전체 (관리자 열람용)."""
    if not (name or "").strip():
        return None
    if name.strip().lower() not in auth.users():
        return None
    return history.load_session(session_id, auth.user_key_for(name))

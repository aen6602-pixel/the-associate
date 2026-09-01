"""The Associate — HTTP 서버 (FastAPI).

역할 분담은 기존 원칙 그대로다: **숫자는 LLM 이 만들지 않는다.**
서버는 (1) 로그인, (2) 대화 세션 영속화, (3) 두뇌(brain) 호출 스트리밍 중계, (4) 산출물 내보내기만
한다. 값 추출은 providers, 계산은 engines 가 계속 담당한다.

- `POST /api/ask` 는 **SSE**(text/event-stream)로 두뇌의 이벤트를 그대로 흘린다.
  `brain.answer()` 는 동기 제너레이터라 엔드포인트를 `def`(async 아님)로 두면 Starlette 가
  threadpool 에서 돌려주므로 이벤트 루프를 막지 않는다.
- 인증은 서명 쿠키(`core.auth`). 브라우저를 새로고침해도 로그인이 유지된다.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import brain
from core import (admin, auth, config, health, history as hist, markdown, paths,
                  skills as skills_lib, sources)

log = logging.getLogger("associate")

WEB_DIR = paths.ROOT / "web"

@asynccontextmanager
async def lifespan(_: FastAPI):
    """기동 시 배포 설정을 로그로 남긴다 — Railway 로그만 보고 오설정을 잡을 수 있게."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("The Associate — deploy_mode=%s auth=%s persistent_storage=%s data_dir=%s",
             config.DEPLOY_MODE, "on" if auth.is_configured() else "OFF",
             paths.IS_PERSISTENT, paths.DATA_DIR)
    if config.DEPLOY_MODE and not auth.is_configured():
        log.error("인증이 설정되지 않았습니다 — APP_USERS 또는 APP_PASSWORD 를 지정하세요. "
                  "설정 전까지 모든 요청을 차단합니다.")
    if config.DEPLOY_MODE and auth.secret_is_ephemeral():
        log.warning("SESSION_SECRET 이 없습니다 — 재시작하면 모든 사용자가 로그아웃됩니다.")
    if config.DEPLOY_MODE and not paths.IS_PERSISTENT:
        log.warning("DATA_DIR 볼륨이 없습니다 — 재배포 시 대화기록이 사라집니다.")
    yield


app = FastAPI(title="The Associate", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


# ── 인증 ─────────────────────────────────────────────────────────
def _secure_cookies(request: Request) -> bool:
    """프록시(Railway) 뒤에서도 HTTPS 여부를 알아내 Secure 플래그를 결정한다."""
    if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https":
        return True
    return request.url.scheme == "https"


def current_viewer(request: Request) -> auth.Viewer:
    """로그인한 사용자. 미인증이면 401.

    게이트가 아예 설정되지 않은 경우: 로컬 개발이면 통과시키고, 배포(DEPLOY_MODE)면 막는다
    — URL 만 알면 아무나 들어오는 상태로 서비스되는 걸 방지(fail-closed)."""
    if not auth.is_configured():
        if config.DEPLOY_MODE:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="인증이 설정되지 않아 앱을 열 수 없습니다. 호스팅 환경변수에 "
                       "APP_USERS(예: alice:pw1,bob:pw2) 또는 APP_PASSWORD 를 설정하세요.",
            )
        return auth.LOCAL_VIEWER

    v = auth.parse_token(request.cookies.get(auth.COOKIE_NAME))
    if v is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return v


class LoginBody(BaseModel):
    name: str = ""
    password: str = ""


def _set_session_cookie(response: Response, request: Request, v: auth.Viewer) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, auth.issue_token(v),
        max_age=auth.ttl_seconds(), httponly=True, samesite="lax",
        secure=_secure_cookies(request), path="/",
    )


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    if not auth.is_configured():
        # 로컬 개발 — 게이트가 없으므로 로그인 자체가 불필요.
        return {"authenticated": True, "label": auth.LOCAL_VIEWER.label, "gate": False}

    v = auth.authenticate(body.name, body.password)
    if v is None:
        time.sleep(1)  # 무차별 대입 속도 제한
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="이름 또는 비밀번호가 올바르지 않습니다.")
    _set_session_cookie(response, request, v)
    return {"authenticated": True, "label": v.label, "gate": True}


class MemberBody(BaseModel):
    name: str = ""


@app.post("/api/member")
def set_member(body: MemberBody, request: Request, response: Response,
               viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    """공용 계정을 쓰는 사람이 **본인 이름을 밝힌다.** 쿠키를 다시 발급할 뿐 계정·권한은
    그대로다. 자기신고이므로 신원 증명이 아니고, 팀 안에서 서로 알아보기 위한 표시다."""
    name = auth.clean_member(body.name)
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="이름을 2글자 이상 입력해 주세요.")
    v = viewer._replace(member=name)
    _set_session_cookie(response, request, v)
    return {"member": v.member, "label": v.label}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"authenticated": False}


@app.get("/api/me")
def me(request: Request) -> dict:
    """로그인 화면을 그릴지 판단하는 용도 — 미인증에서도 200 을 준다."""
    gate = auth.is_configured()
    if not gate:
        blocked = config.DEPLOY_MODE
        return {"authenticated": not blocked, "gate": False, "blocked": blocked,
                "label": None if blocked else auth.LOCAL_VIEWER.label,
                "needs_name": False,
                "message": ("인증이 설정되지 않아 앱을 열 수 없습니다. 관리자가 APP_USERS 또는 "
                            "APP_PASSWORD 를 설정해야 합니다.") if blocked else None}
    v = auth.parse_token(request.cookies.get(auth.COOKIE_NAME))
    return {"authenticated": v is not None, "gate": True, "blocked": False,
            "label": v.label if v else None, "needs_name": auth.needs_name(), "message": None,
            "member": v.member if v else None,
            "is_admin": auth.is_admin(v) if v else False}


# ── 부트스트랩 (사이드바에 필요한 모든 정적 정보) ──────────────────
@app.get("/api/bootstrap")
def bootstrap(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    engines = []
    for key, p in config.LLM_PROVIDERS.items():
        # 배포에선 claude CLI 방식은 컨테이너에 CLI 가 없어 동작 불가 → 목록에서 숨긴다.
        if config.DEPLOY_MODE and p.get("auth_mode") == "cli":
            continue
        info = config.resolve_llm(key)
        engines.append({
            "provider": key, "label": p["label"], "default_model": p["default_model"],
            "presets": p["presets"], "connected": bool(info["key"]), "key_name": info["key_name"],
            # 추론강도(reasoning effort) — provider 마다 받을 수 있는 값이 다르다.
            "reasoning_levels": info["reasoning_levels"],
            "default_reasoning": info["default_reasoning"],
        })

    def _src(s: dict) -> dict:
        code, badge = sources.status(s)
        return {
            "name": s["name"], "org": s["org"], "tier": s["tier"],
            "tier_icon": sources.tier_icon(s["tier"]), "status": code, "badge": badge,
            "provides": s["provides"], "used_by": s["used_by"], "url": s["url"],
            "note": s.get("note"), "key_attr": s.get("key_attr"),
        }

    return {
        # needs_member: 아직 본인 이름을 안 밝힌 사람 → 앱이 팝업으로 한 번 물어본다.
        # 게이트가 없는 로컬 개발에서는 물어볼 이유가 없다.
        "viewer": {"label": viewer.label, "is_admin": auth.is_admin(viewer),
                   "member": viewer.member,
                   "needs_member": bool(auth.is_configured() and not viewer.member)},
        "gate": auth.is_configured(),
        "deploy_mode": config.DEPLOY_MODE,
        "persistent_storage": paths.IS_PERSISTENT,
        "engines": engines,
        "default_engine": {
            "provider": config.LLM_PROVIDER if any(
                e["provider"] == config.LLM_PROVIDER for e in engines) else engines[0]["provider"],
        },
        "reasoning_labels": config.REASONING_LABELS,
        "sources": [_src(s) for s in sources.SOURCES],
        "roadmap": [{"name": r["name"], "org": r["org"], "provides": r["provides"]}
                    for r in sources.ROADMAP],
        "skills": [{"name": s["name"], "description": s["description"],
                    "references": s["references"]} for s in skills_lib.available()],
    }


# ── 대화 세션 ────────────────────────────────────────────────────
def _with_html(messages: list[dict]) -> list[dict]:
    """저장된 메시지에 렌더된 HTML 을 붙여 보낸다(마크다운 변환을 서버가 담당)."""
    out = []
    for m in messages:
        item = dict(m)
        if m.get("role") == "assistant":
            item["html"] = markdown.render(m.get("content") or "")
        out.append(item)
    return out


@app.get("/api/sessions")
def list_sessions(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    return {"sessions": hist.list_sessions(viewer.key)}


@app.get("/api/sessions/{sid}")
def get_session(sid: str, viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    rec = hist.load_session(sid, viewer.key)
    if rec is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return {"id": rec.get("id"), "title": rec.get("title"),
            "messages": _with_html(rec.get("messages", []))}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    return {"deleted": hist.delete_session(sid, viewer.key)}


@app.post("/api/sessions/new")
def new_session(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    return {"id": hist.new_session_id()}


# ── 질문 → 두뇌 스트리밍 (SSE) ────────────────────────────────────
class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


@app.post("/api/ask")
def ask(body: AskBody, viewer: auth.Viewer = Depends(current_viewer)) -> StreamingResponse:
    provider = (body.provider or config.LLM_PROVIDER).lower()
    if provider not in config.LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 두뇌: {provider}")
    if config.DEPLOY_MODE and config.LLM_PROVIDERS[provider].get("auth_mode") == "cli":
        raise HTTPException(status_code=400,
                            detail="이 두뇌(claude CLI)는 배포 환경에서 쓸 수 없습니다.")
    # 추론강도는 provider 별로 받을 수 있는 값이 달라 조용히 무시하지 않고 400 으로 거른다
    # (무시하면 사용자가 "높음" 을 골랐는데 기본값으로 돈 것을 알 방법이 없다).
    if not config.is_valid_reasoning(provider, body.reasoning):
        raise HTTPException(
            status_code=400,
            detail=(f"{provider} 가 지원하지 않는 추론강도: {body.reasoning} "
                    f"(지원: {', '.join(config.reasoning_levels(provider)) or '없음'})"))

    sid = body.session_id or hist.new_session_id()
    rec = hist.load_session(sid, viewer.key) or {}
    prior: list[dict] = list(rec.get("messages", []))
    question = body.question.strip()

    def stream() -> Iterator[str]:
        yield _sse({"type": "start", "session_id": sid})
        trace: list[dict] = []
        final_text = ""
        try:
            for ev in brain.answer(question, history=prior,
                                   provider=provider, model=body.model,
                                   reasoning=body.reasoning):
                kind = ev.get("type")
                if kind == "tool_result":
                    trace.append({"name": ev["name"], "input": ev["input"],
                                  "result": ev["result"]})
                    yield _sse(ev)
                elif kind == "final":
                    final_text = ev.get("text") or ""
                elif kind == "error":
                    final_text = f"⚠️ {ev.get('text')}"
                    yield _sse(ev)
                elif kind in ("tool_use", "progress"):
                    yield _sse(ev)
                # assistant_text(중간 사고)는 트레이스에 노출하지 않는다 — 기존 UI 와 동일.
        except Exception as e:  # noqa: BLE001 — 두뇌/provider 예외가 스트림을 죽이지 않게
            log.exception("brain.answer 실패")
            final_text = f"⚠️ 처리 중 오류가 발생했습니다: {e}"
            yield _sse({"type": "error", "text": str(e)})

        # 질문에 **그때 밝힌 이름**을 찍는다. 나중에 이름을 바꿔도 과거 기록의 작성자는
        # 그대로 남아야 하므로 세션이 아니라 메시지에 붙인다.
        asked_by = viewer.member or viewer.label
        messages = prior + [
            {"role": "user", "content": question, "by": asked_by},
            {"role": "assistant", "content": final_text, "trace": trace},
        ]
        hist.save_session(sid, messages, viewer.key)
        yield _sse({"type": "final", "text": final_text,
                    "html": markdown.render(final_text), "trace": trace})
        yield _sse({"type": "done", "session_id": sid,
                    "sessions": hist.list_sessions(viewer.key)})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # 프록시가 스트림을 버퍼링하지 않도록
    })


# ── 산출물 내보내기 (저장된 trace 를 서버가 다시 계산) ──────────────
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 내보내기 종류 → (필요한 tool 이름, 워크북 생성 함수 이름, MIME, 없을 때 안내)
# 엑셀은 그 답변에서 **실제로 호출된 계산 도구의 입력을 그대로 재사용**해 만든다 —
# 화면에 보인 숫자와 파일의 숫자가 어긋나지 않게 하려는 것이고, 클라이언트 입력을 믿지 않는다.
# 값의 첫 원소는 **우선순위 순의 도구 목록**이다. compute_scenarios 는 compute_dcf 와
# 같은 기본안 입력(성장·마진·WACC·순부채…)에 Bull/Bear 델타만 얹은 것이라, 그 입력으로
# 그대로 DCF 워크북을 만들 수 있다(델타는 _adapt_to_builder 가 버린다). 시나리오로 돌린
# 대화에서 엑셀 버튼이 아예 안 나오던 것을 막는다 — 이때 받는 파일은 **Base 시나리오**다.
_XLSX_EXPORTS = {
    "dcf_full": (("compute_dcf", "compute_scenarios"), "dcf_full_workbook",
                 "이 답변에는 DCF 계산이 없습니다."),
    "dcf": (("compute_dcf", "compute_scenarios"), "dcf_workbook",
            "이 답변에는 DCF 계산이 없습니다."),
    "sangjeung": (("evaluate_sangjeung_value",), "sangjeung_workbook",
                  "이 답변에는 상증법 평가가 없습니다."),
    "comps": (("compute_comps",), "comps_workbook", "이 답변에는 Comps 계산이 없습니다."),
}


# 도구 인자명 → 워크북 생성기 인자명. 이름이 갈린 것을 여기서 흡수한다
# (compute_dcf 는 revenue_growth_pct, dcf_workbook 은 revenue_growth 를 쓴다).
_BUILDER_ALIASES = {"revenue_growth_pct": "revenue_growth"}


def _adapt_to_builder(builder, tool_input: dict) -> tuple[dict, list[str]]:
    """도구 입력을 생성기 시그니처에 맞춘다 → (kwargs, 버린 인자 목록).

    도구와 워크북은 각각 진화해서 인자가 어긋난다(실측: compute_dcf 에 market 을 추가한 뒤
    dcf_full_workbook 이 `unexpected keyword argument 'market'` 로 죽어 엑셀 다운로드가
    깨져 있었다). `**input` 을 그대로 넘기지 않고 받아주는 것만 골라 넘긴다."""
    params = inspect.signature(builder).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(tool_input or {}), []   # **kwargs 를 받는 생성기는 무엇이든 받는다
    kwargs: dict = {}
    dropped: list[str] = []
    for k, v in (tool_input or {}).items():
        alias = _BUILDER_ALIASES.get(k)
        if k in params:
            kwargs[k] = v
        elif alias and alias in params:
            kwargs[alias] = v
        else:
            dropped.append(k)
    return kwargs, dropped


class ExportBody(BaseModel):
    index: int = Field(ge=0)          # 세션 messages 안의 assistant 메시지 위치
    kind: str                         # _XLSX_EXPORTS 의 키 또는 "html_report"


def _attachment(data: bytes, filename: str, media_type: str) -> Response:
    # 한글 파일명 → RFC 5987 (브라우저가 깨지지 않게 filename* 사용)
    disp = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": disp})


@app.post("/api/sessions/{sid}/export")
def export(sid: str, body: ExportBody,
           viewer: auth.Viewer = Depends(current_viewer)) -> Response:
    rec = hist.load_session(sid, viewer.key)
    if rec is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    messages = rec.get("messages", [])
    if body.index >= len(messages):
        raise HTTPException(status_code=404, detail="해당 메시지가 없습니다.")
    msg = messages[body.index]
    trace = msg.get("trace") or []

    try:
        spec = _XLSX_EXPORTS.get(body.kind)
        if spec is not None:
            tool_names, builder_name, missing = spec
            call = next((t for name in tool_names for t in trace
                         if t["name"] == name and t["result"].get("ok")), None)
            if call is None:
                raise HTTPException(status_code=400, detail=missing)
            from excel import exporters

            builder = getattr(exporters, builder_name)
            kwargs, dropped = _adapt_to_builder(builder, call["input"])
            # 생성기가 market 을 못 받는데 국내가 아니면, 조용히 한국 모델을 만들어선 안 된다.
            mkt = str((call["input"] or {}).get("market") or "KR").upper()
            if "market" in dropped and mkt != "KR":
                raise HTTPException(
                    status_code=400,
                    detail=f"'{body.kind}' 엑셀은 한국(DART) 기업만 지원합니다 "
                           f"(이 계산의 시장: {mkt}). HTML 리포트를 사용하세요.")
            data, fname = builder(**kwargs)
            return _attachment(data, fname, XLSX_MIME)

        if body.kind == "html_report":
            from excel.html_report import build_html_report

            prior_q = (messages[body.index - 1]["content"]
                       if body.index > 0 and messages[body.index - 1].get("role") == "user"
                       else None)
            data, fname = build_html_report(msg.get("content") or "", trace, question=prior_q)
            return _attachment(data, fname, "text/html; charset=utf-8")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — 엑셀 생성 실패를 500 이 아니라 메시지로
        log.exception("내보내기 실패")
        raise HTTPException(status_code=400, detail=f"생성 실패: {e}") from e

    raise HTTPException(status_code=400, detail=f"알 수 없는 내보내기 종류: {body.kind}")


# ── 관리자 (지정된 계정만) ────────────────────────────────────────
def require_admin(request: Request) -> auth.Viewer:
    """관리자 전용. 로그인만으로는 부족하고 ADMIN_USERS 에 속해야 한다."""
    viewer = current_viewer(request)
    if not auth.is_admin(viewer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="관리자만 접근할 수 있습니다.")
    return viewer


@app.get("/api/admin/overview")
def admin_overview(viewer: auth.Viewer = Depends(require_admin)) -> dict:
    data = admin.overview()
    data["viewer"] = {"label": viewer.label}
    data["admins"] = sorted(auth.admins())
    return data


@app.get("/api/admin/sessions/{name}/{sid}")
def admin_session(name: str, sid: str,
                  viewer: auth.Viewer = Depends(require_admin)) -> dict:
    rec = admin.user_session(name, sid)
    if rec is None:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return {"user": name, "id": rec.get("id"), "title": rec.get("title"),
            "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
            "messages": _with_html(rec.get("messages", []))}


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> FileResponse:
    # 인증은 페이지가 아니라 API 에서 건다 — 화면은 껍데기고 데이터는 전부 API 로 온다.
    return FileResponse(WEB_DIR / "admin.html")


# ── Market Muse (구독 채널 전언 — 공신력 없음, 본 채팅과 격리) ─────
# 별도 라우트·별도 세션 네임스페이스로 둔다. 본 채팅의 도구 레지스트리에는 등록하지 않아
# 밸류에이션 경로가 이 데이터를 건드릴 수 없다.
class MuseBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None
    channel: str | None = None
    provider: str | None = None
    model: str | None = None


def _muse_key(viewer: auth.Viewer) -> str:
    """대화기록을 본 채팅과 섞지 않는다 — 목록에 함께 뜨면 어느 쪽 근거인지 헷갈린다."""
    return f"{viewer.key}__muse"


@app.get("/api/muse/status")
def muse_status(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    """수집 현황. 데이터가 낡았으면 **화면을 막지 않고** 뒤에서 다시 모은다."""
    from providers import telegram_muse as tg

    started = False
    if tg.is_stale() and not tg.collect_status()["running"]:
        started = tg.collect_in_background()
    return {**tg.stats(), **tg.collect_status(), "refresh_started": started}


@app.post("/api/muse/collect")
def muse_collect(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    """'지금 수집' — 관리자만. 텔레그램 rate-limit 이 있어 아무나 누르게 두지 않는다."""
    from providers import telegram_muse as tg

    if auth.is_configured() and not auth.is_admin(viewer):
        raise HTTPException(status_code=403, detail="수집은 관리자만 실행할 수 있습니다.")
    if not tg.collect_in_background():
        raise HTTPException(status_code=409, detail="이미 수집 중입니다.")
    return {"started": True}


@app.get("/api/muse/sessions")
def muse_sessions(viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    return {"sessions": hist.list_sessions(_muse_key(viewer))}


@app.get("/api/muse/sessions/{sid}")
def muse_session(sid: str, viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    rec = hist.load_session(sid, _muse_key(viewer))
    if rec is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return {"id": rec.get("id"), "title": rec.get("title"),
            "messages": _with_html(rec.get("messages", []))}


@app.post("/api/muse/ask")
def muse_ask(body: MuseBody, viewer: auth.Viewer = Depends(current_viewer)) -> StreamingResponse:
    from agent import muse

    sid = body.session_id or hist.new_session_id()
    prior = list((hist.load_session(sid, _muse_key(viewer)) or {}).get("messages", []))
    question = body.question.strip()

    def stream() -> Iterator[str]:
        yield _sse({"type": "start", "session_id": sid})
        final_text, posts = "", []
        try:
            for ev in muse.answer(question, prior, body.provider, body.model, body.channel):
                if ev.get("type") == "sources":
                    posts = ev.get("posts") or []
                elif ev.get("type") == "final":
                    final_text = ev.get("text") or ""
                yield _sse(ev)
        except Exception as e:  # noqa: BLE001
            log.exception("muse ask failed")
            yield _sse({"type": "error", "text": str(e)})

        messages = prior + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": final_text, "posts": posts},
        ]
        hist.save_session(sid, messages, _muse_key(viewer))
        yield _sse({"type": "final_html", "html": markdown.render(final_text)})
        yield _sse({"type": "done", "session_id": sid,
                    "sessions": hist.list_sessions(_muse_key(viewer))})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/muse", response_class=HTMLResponse)
def muse_page() -> FileResponse:
    return FileResponse(WEB_DIR / "muse.html")


# ── 데이터 소스 실측 점검 ────────────────────────────────────────
# bootstrap 에 넣지 않는 이유: 전 소스를 두드리는 데 수 초가 걸려 그만큼 첫 화면이 늦어진다.
# 프론트가 화면을 먼저 그린 뒤 따로 부른다.
@app.get("/api/health/sources")
def health_sources(refresh: bool = False,
                   viewer: auth.Viewer = Depends(current_viewer)) -> dict:
    return health.snapshot(force=refresh)


# ── 헬스체크 & 정적 파일 ─────────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    """호스팅 헬스체크. 이 응답이 오면 앱 모듈 import 와 라우팅이 정상이라는 뜻이다."""
    return {"status": "ok", "auth": auth.is_configured(), "persistent": paths.IS_PERSISTENT}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# ── PWA (홈 화면 설치 · 앱처럼 실행) ─────────────────────────────
# 서비스워커는 **자기 경로 아래만** 제어할 수 있다. /static/sw.js 로 두면 /static/* 만
# 담당하게 되어 화면 진입(navigation)을 못 잡는다 → 루트에서 서빙해야 scope 가 '/' 가 된다.
@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(
        WEB_DIR / "sw.js", media_type="application/javascript",
        headers={
            # SW 파일 자체가 캐시되면 새 배포가 사용자 기기에 영원히 안 내려간다.
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        })


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(
        WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"})


# iOS 는 링크 태그를 못 읽는 상황(홈화면 추가 시점)에 루트의 관례 경로를 직접 찾는다.
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_icon() -> FileResponse:
    return FileResponse(WEB_DIR / "icons" / "apple-touch-icon.png",
                        media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "icons" / "favicon-32.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

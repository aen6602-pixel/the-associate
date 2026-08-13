"""SKSQ Valuation Agent — Streamlit UI.

입력창에 질문 → 어떤 API(tool)를 쓰는지 실시간 트레이스 → 출처 붙은 답변.
실행:  .venv 의 streamlit 으로 `streamlit run app.py`
"""
from __future__ import annotations

import streamlit as st

# ── Streamlit Cloud 시크릿 → 환경변수 (config import 전에 실행해야 함) ──────
# 클라우드는 키를 st.secrets 로 주입하는데 core.config 는 import 시점에 os.getenv 로만
# 읽는다. config 가 로드되기 전에 secrets 를 os.environ 으로 옮겨준다. 로컬(.env)엔 무영향.
try:
    import os as _os
    for _k, _v in st.secrets.items():
        _os.environ.setdefault(_k, str(_v))
except Exception:
    pass

from core import config, sources, history as hist
from agent import brain

st.set_page_config(page_title="The Associate", page_icon="📊", layout="wide")


# ── 로그인 사용자 식별 (세션 격리용) ──────────────────────────────
def _current_user_key() -> str:
    """Streamlit Community Cloud 비공개 앱은 인증 이메일을 X-Streamlit-User 헤더로 준다
    (1.42+ 에선 st.user 가 비어 있음). 이를 해시해 사용자별 세션 폴더 키로 쓴다.
    로컬/미인증 실행이면 'local'."""
    try:
        email = st.context.headers.get("X-Streamlit-User")
    except Exception:
        email = None
    if not email:
        return "local"
    import hashlib
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


USER_KEY = _current_user_key()

# ── 세션 상태 초기화 (첫 로드 시 1회) ──────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = hist.new_session_id()
if "history" not in st.session_state:
    st.session_state.history = []  # [{"role": "user"|"assistant", "content": str}]
st.session_state.setdefault("llm_provider", config.LLM_PROVIDER)
st.session_state.setdefault(
    "llm_model", config.LLM_PROVIDERS[st.session_state.llm_provider]["default_model"]
)

# ── 전역 스타일 (매 렌더 주입) ─────────────────────────────────
_GLOBAL_CSS = """
<style>
/* 헤더 · 소개 */
.assoc-lede { font-size: 1.1rem; line-height: 1.62; color: #B9C0CC; max-width: 840px; margin: .15rem 0 0; }
.assoc-cap  { list-style: none; margin: 1.15rem 0 .3rem; padding-left: .2rem; max-width: 840px; }
.assoc-cap li { position: relative; color: #CAD0DA; font-size: 1.03rem; line-height: 1.95; padding-left: 1.4rem; }
.assoc-cap li::before { content: "▸"; position: absolute; left: 0; color: #F0B429; }
/* 사이드바 */
section[data-testid="stSidebar"] { font-size: .88rem; }
[data-testid="stSidebarUserContent"] { padding-top: .5rem; }
section[data-testid="stSidebar"] .stButton button { font-size: .82rem; }
.assoc-sec {
  font-size: .7rem; font-weight: 700; letter-spacing: .2em; text-transform: uppercase;
  color: #7E8797; margin: .55rem 0 .5rem;
}
</style>
"""
st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)

# ── 첫 로드 스플래시 (세션당 1회, CSS 로 자동 페이드아웃) ──────────────
_SPLASH_HTML = """
<style>
@keyframes assocFade   { to { opacity: 0; visibility: hidden; } }
@keyframes assocRise   { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes assocRule   { to { width: 180px; } }
@keyframes assocDot    { 0%, 20% { opacity: .15; } 50% { opacity: 1; } 100% { opacity: .15; } }
@keyframes assocPhrase { 0% { opacity: 0; } 6% { opacity: 1; } 27% { opacity: 1; } 33% { opacity: 0; } 100% { opacity: 0; } }
.assoc-splash {
  position: fixed; inset: 0; z-index: 99999; pointer-events: none;
  display: flex; align-items: center; justify-content: center;
  background: #0A0E17;
  animation: assocFade .8s ease 1.5s forwards;
}
.assoc-inner { text-align: center; animation: assocRise .8s ease both; }
.assoc-mark {
  font-family: -apple-system, "Segoe UI", system-ui, Roboto, sans-serif;
  font-weight: 600; font-size: 2.2rem;
  letter-spacing: .38em; padding-left: .38em;
  color: #F0B429;
}
.assoc-rule { width: 0; height: 1px; margin: 1.1rem auto 0; background: #F0B429; opacity: .5; animation: assocRule 1.4s ease .3s forwards; }
.assoc-sub  { margin-top: 1rem; color: #8A93A3; letter-spacing: .06em; font-size: .95rem; }
.assoc-load { position: relative; height: 1.2em; margin-top: 2.3rem; color: #E4E7EB; letter-spacing: .16em; font-size: .82rem; text-transform: uppercase; }
.assoc-load .ln { position: absolute; inset: 0; text-align: center; opacity: 0; animation: assocPhrase 1.5s infinite both; }
.assoc-load .ln:nth-child(2) { animation-delay: .5s; }
.assoc-load .ln:nth-child(3) { animation-delay: 1s; }
.assoc-load .d span { animation: assocDot 1.2s infinite both; }
.assoc-load .d span:nth-child(2) { animation-delay: .15s; }
.assoc-load .d span:nth-child(3) { animation-delay: .3s; }
</style>
<div class="assoc-splash"><div class="assoc-inner">
  <div class="assoc-mark">THE ASSOCIATE</div>
  <div class="assoc-rule"></div>
  <div class="assoc-sub">Every figure, traced to its source.</div>
  <div class="assoc-load">
    <span class="ln">Opening the desk<span class="d"><span>.</span><span>.</span><span>.</span></span></span>
    <span class="ln">Connecting to sources<span class="d"><span>.</span><span>.</span><span>.</span></span></span>
    <span class="ln">Spreading the comps<span class="d"><span>.</span><span>.</span><span>.</span></span></span>
  </div>
</div></div>
"""
if "splashed" not in st.session_state:
    st.session_state.splashed = True
    st.markdown(_SPLASH_HTML, unsafe_allow_html=True)

# ── 출처 등급 뱃지 ──────────────────────────────────────────────
_TIER = {
    "authoritative": ("🟢 공식", "정부·규제기관·중앙은행·거래소"),
    "reference": ("🔵 참조", "업계 표준 데이터셋(Damodaran 등)"),
    "computed": ("🟣 계산", "엔진이 계산한 파생값"),
    "llm_estimate": ("🔴 LLM추정", "소스 없음 — LLM 추정치, 검증 필요"),
}


def tier_badge(source_type: str) -> str:
    label, _ = _TIER.get(source_type, ("⚪ ?", ""))
    return label


def _fmt_args(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items())


def render_tool_result(name, tool_input, result):
    if result["ok"]:
        v = result["value"]
        p = v["provenance"]
        st.markdown(
            f"**{tier_badge(p['source_type'])}** &nbsp; `{name}({_fmt_args(tool_input)})` "
            f"→ **{v['value']} {v['unit']}**"
        )
        meta = []
        if p.get("as_of"):
            meta.append(f"기준 {p['as_of']}")
        if p.get("original_field"):
            meta.append(f"필드 `{p['original_field']}`")
        meta.append(f"출처 {p['source']}")
        st.caption(" · ".join(meta) + f"  \n{p['source_url']}")
        for extra in (v.get("extras") or {}).values():
            st.markdown(f"&nbsp;&nbsp;↳ **{extra['label']}**: {extra['value']:,} {extra['unit']}")
    else:
        st.error(f"`{name}` → {result['error']}")


# ── 사이드바: 키/모델 상태 ──────────────────────────────────────
def _source_popover(s: dict):
    """소스 하나를 클릭 가능한 popover(말풍선)로 렌더."""
    code, badge = sources.status(s)
    label = f"{sources.tier_icon(s['tier'])} {s['name']}  ·  {badge}"
    with st.popover(label, use_container_width=True):
        st.markdown(f"**{s['org']}**")
        tier_ko = "공식 (정부·중앙은행·거래소)" if s["tier"] == "authoritative" else "참조 (업계표준 데이터셋)"
        st.caption(f"등급: {sources.tier_icon(s['tier'])} {tier_ko}")
        st.markdown(f"**제공:** {s['provides']}")
        st.markdown(f"**사용처:** {s['used_by']}")
        if code == "nokey":
            st.warning(f"`.env` 에 `{s['key_attr']}_API_KEY` 를 넣으면 연결됩니다.")
        elif code == "planned":
            st.info("아직 provider 미연동 (예정)")
        else:
            st.success("연결됨 — 지금 사용 가능")
        if s.get("note"):
            st.caption(s["note"])
        st.markdown(f"🔗 [{s['url']}]({s['url']})")


def _switch_session(sid: str, messages: list[dict]):
    st.session_state.session_id = sid
    st.session_state.history = messages
    st.session_state.pop("xlsx", None)
    st.rerun()


_llm = config.resolve_llm(st.session_state.llm_provider, st.session_state.llm_model)
with st.sidebar:
    st.markdown("<div class='assoc-sec'>Conversations</div>", unsafe_allow_html=True)
    if st.button("＋  New session", use_container_width=True):
        _switch_session(hist.new_session_id(), [])

    _sessions = hist.list_sessions(USER_KEY)
    if not _sessions:
        st.caption("No conversations yet — send a question and it'll appear here.")
    else:
        # 대화가 쌓여도 사이드바 상단 1/3 안에 머물도록 높이를 고정하고 내부 스크롤을 쓴다.
        with st.container(height=150):
            for s in _sessions:
                is_active = s["id"] == st.session_state.session_id
                c1, c2 = st.columns([5, 1])
                with c1:
                    if st.button(("📍 " if is_active else "") + s["title"],
                                key=f"sess_{s['id']}", use_container_width=True,
                                type="primary" if is_active else "secondary"):
                        rec = hist.load_session(s["id"], USER_KEY)
                        if rec:
                            _switch_session(s["id"], rec["messages"])
                with c2:
                    if st.button("🗑", key=f"del_{s['id']}"):
                        hist.delete_session(s["id"], USER_KEY)
                        if is_active:
                            _switch_session(hist.new_session_id(), [])
                        else:
                            st.rerun()
    st.divider()

    st.markdown("<div class='assoc-sec'>Data Sources</div>", unsafe_allow_html=True)
    st.caption("Click any item for details. 🟢 official 🔵 reference · ✅ live ⬜ key needed 🔜 planned")

    # 두뇌(LLM) 선택 — 클릭해서 제공사/모델 전환.
    # 배포(DEPLOY_MODE)에선 claude CLI 방식(anthropic)은 클라우드에 CLI 가 없어 동작 불가 → 목록에서 숨김.
    _provider_keys = [k for k, v in config.LLM_PROVIDERS.items()
                      if not (config.DEPLOY_MODE and v.get("auth_mode") == "cli")]
    _provider_labels = {k: v["label"] for k, v in config.LLM_PROVIDERS.items()}
    # 이전에 고른 provider 가 숨겨졌으면(예: 배포 후 세션에 anthropic 잔존) 첫 항목으로 리셋 → index 오류 방지.
    if st.session_state.llm_provider not in _provider_keys:
        st.session_state.llm_provider = _provider_keys[0]
        st.session_state.llm_model = config.LLM_PROVIDERS[_provider_keys[0]]["default_model"]
    with st.expander(
        f"🧠 Engine: {_llm['label']} · `{_llm['model']}`  {'✅' if _llm['key'] else '⬜'}",
        expanded=not bool(_llm["key"]),
    ):
        st.caption("질문을 이해하고 **어떤 도구(API)를 부를지** 결정합니다. 숫자는 만들지 않습니다.")

        _new_provider = st.selectbox(
            "제공사", _provider_keys,
            index=_provider_keys.index(st.session_state.llm_provider),
            format_func=lambda k: _provider_labels[k],
            key="llm_provider_picker",
        )
        if _new_provider != st.session_state.llm_provider:
            st.session_state.llm_provider = _new_provider
            st.session_state.llm_model = config.LLM_PROVIDERS[_new_provider]["default_model"]
            st.rerun()

        _presets = config.LLM_PROVIDERS[st.session_state.llm_provider]["presets"]
        _custom_flag = "✏️ 직접 입력…"
        _options = _presets + [_custom_flag]
        _cur_model = st.session_state.llm_model
        _idx = _presets.index(_cur_model) if _cur_model in _presets else len(_presets)
        _picked = st.selectbox("모델", _options, index=_idx, key="llm_model_picker")
        if _picked == _custom_flag:
            _typed = st.text_input(
                "모델 ID", value=(_cur_model if _cur_model not in _presets else ""),
                placeholder="예: gpt-5, gemini-2.5-pro, claude-opus-5",
                key="llm_model_typed",
            )
            if _typed.strip():
                st.session_state.llm_model = _typed.strip()
        else:
            st.session_state.llm_model = _picked

        if _llm["key"]:
            st.success(f"`{_llm['key_name']}` 연결됨")
        else:
            st.warning(f"`.env` 에 `{_llm['key_name']}` 를 넣어야 이 두뇌를 쓸 수 있어요.")

    live = [s for s in sources.SOURCES if sources.status(s)[0] == "live"]
    nokey = [s for s in sources.SOURCES if sources.status(s)[0] == "nokey"]
    planned = [s for s in sources.SOURCES if sources.status(s)[0] == "planned"]

    st.markdown(f"**Connected ({len(live)})**")
    for s in live:
        _source_popover(s)
    if nokey:
        st.markdown(f"**Key needed ({len(nokey)})**")
        for s in nokey:
            _source_popover(s)
    if planned:
        st.markdown(f"**Planned ({len(planned)})**")
        for s in planned:
            _source_popover(s)

    st.markdown(f"**Roadmap ({len(sources.ROADMAP)})**")
    st.caption("Not yet wired up — data worth adding later, listed here for reference.")
    for r in sources.ROADMAP:
        with st.popover(f"🗺️ {r['name']}", use_container_width=True):
            st.markdown(f"**{r['org']}**")
            st.markdown(f"**제공 예정:** {r['provides']}")

st.title("The Associate")
st.markdown(
    "<p class='assoc-lede'>Your AI valuation associate — the work an investment bank or "
    "accounting firm would run, on demand.</p>"
    "<p class='assoc-lede' style='margin-top:.55rem'>Every figure traces back to a public "
    "source. No fabricated numbers. No hallucinations.</p>",
    unsafe_allow_html=True,
)

if not _llm["key"]:
    st.warning(f"`.env` 에 `{_llm['key_name']}` 를 넣어야 에이전트가 동작합니다. "
               "(채팅에 붙이지 말고 파일에 직접 입력)")

# ── 첫 진입: 이 에이전트가 하는 일 (30년차 IB 톤) ──────────────────
if not st.session_state.history:
    st.markdown(
        "<ul class='assoc-cap'>"
        "<li>Pull primary disclosures straight from the filing — Korea, the U.S., Japan and "
        "Taiwan — and read the financials line by line, footnotes included.</li>"
        "<li>Mark the inputs that actually move a valuation — risk-free curves, equity risk "
        "premia, corporate tax and country risk, Beta — off the Bank of Korea, FRED and Damodaran.</li>"
        "<li>Put it to work in whatever the situation calls for — DCF, trading comps, and "
        "Korean statutory appraisal — with every number tied back to where it came from.</li>"
        "</ul>",
        unsafe_allow_html=True,
    )

# ── 대화 기록 (트레이스는 message 에 저장돼 있으면 접힌 상태로 같이 복원) ──
# 다운로드 버튼은 이 루프(history 재렌더)에 둔다 — 방금 답변 직후 블록에 두면 바로 이어지는
# st.rerun() 에 밀려 클릭할 틈도 없이 사라진다.
for i, msg in enumerate(st.session_state.history):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("trace"):
            with st.expander(f"🔍 사용한 데이터 소스 ({len(msg['trace'])}개)", expanded=False):
                for t in msg["trace"]:
                    render_tool_result(t["name"], t["input"], t["result"])
            dcf_call = next((t for t in msg["trace"]
                            if t["name"] == "compute_dcf" and t["result"]["ok"]), None)
            if dcf_call:
                if st.button("📥 전체 DCF 모델 다운로드 (5시트)", key=f"full_dcf_btn_{i}"):
                    try:
                        from excel.exporters import dcf_full_workbook
                        data, fname = dcf_full_workbook(**dcf_call["input"])
                        st.session_state[f"full_dcf_xlsx_{i}"] = (data, fname)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"전체 모델 생성 실패: {e}")
                if st.session_state.get(f"full_dcf_xlsx_{i}"):
                    _data, _fname = st.session_state[f"full_dcf_xlsx_{i}"]
                    st.download_button(
                        f"⬇️ {_fname}", _data, _fname, key=f"full_dcf_dl_{i}",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            if st.button("📄 HTML 리포트 다운로드", key=f"html_report_btn_{i}"):
                try:
                    from excel.html_report import build_html_report
                    prior_q = (st.session_state.history[i - 1]["content"]
                              if i > 0 and st.session_state.history[i - 1]["role"] == "user" else None)
                    data, fname = build_html_report(msg["content"], msg["trace"], question=prior_q)
                    st.session_state[f"html_report_{i}"] = (data, fname)
                except Exception as e:  # noqa: BLE001
                    st.error(f"HTML 리포트 생성 실패: {e}")
            if st.session_state.get(f"html_report_{i}"):
                _hdata, _hfname = st.session_state[f"html_report_{i}"]
                st.download_button(f"⬇️ {_hfname}", _hdata, _hfname, key=f"html_report_dl_{i}",
                                   mime="text/html")

# ── 입력 처리 ──────────────────────────────────────────────
question = st.chat_input("Instruct the associate…  (밸류에이션·데이터 질문)")
if question:
    prior_history = list(st.session_state.history)  # 두뇌에 넘길 "이전" 맥락(현재 질문 제외)
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        trace_box = st.status("🔍 데이터 소스 조회 중…", expanded=True)
        final_text = ""
        trace_events: list[dict] = []
        with trace_box:
            for ev in brain.answer(question, history=prior_history,
                                   provider=st.session_state.llm_provider,
                                   model=st.session_state.llm_model):
                if ev["type"] == "tool_use":
                    st.write(f"🔧 호출: `{ev['name']}({_fmt_args(ev['input'])})`")
                elif ev["type"] == "progress":
                    st.write(f"🔧 {ev['text']}")
                elif ev["type"] == "tool_result":
                    render_tool_result(ev["name"], ev["input"], ev["result"])
                    trace_events.append({"name": ev["name"], "input": ev["input"],
                                         "result": ev["result"]})
                elif ev["type"] == "assistant_text":
                    pass  # 중간 사고 텍스트는 트레이스에 안 보임
                elif ev["type"] == "final":
                    final_text = ev["text"]
                elif ev["type"] == "error":
                    st.error(ev["text"])
                    final_text = f"⚠️ {ev['text']}"
        trace_box.update(label="✅ 조회 완료", state="complete", expanded=False)
        if final_text:
            st.markdown(final_text)

    st.session_state.history.append(
        {"role": "assistant", "content": final_text, "trace": trace_events}
    )
    hist.save_session(st.session_state.session_id, st.session_state.history, USER_KEY)
    st.rerun()  # 사이드바 대화 목록에 이번 세션(제목·순서) 즉시 반영

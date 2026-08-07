"""SKSQ Valuation Agent — Streamlit UI.

입력창에 질문 → 어떤 API(tool)를 쓰는지 실시간 트레이스 → 출처 붙은 답변.
실행:  .venv 의 streamlit 으로 `streamlit run app.py`
"""
from __future__ import annotations

import streamlit as st

from core import config
from agent import brain

st.set_page_config(page_title="SKSQ Valuation Agent", page_icon="📊", layout="wide")

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


# ── 사이드바: 키/모델 상태 ──────────────────────────────────────
_llm = config.active_llm()
with st.sidebar:
    st.header("⚙️ 상태")
    st.caption(f"두뇌: `{_llm['provider']}`  ·  모델 `{_llm['model']}`")
    st.markdown("**API 키**")
    st.write(("✅ " if _llm["key"] else "⬜ ") + f"{_llm['key_name']} (두뇌)")
    data_keys = {
        "DART (한국)": config.Keys.DART,
        "ECOS (한국은행)": config.Keys.ECOS,
        "FRED (미국)": config.Keys.FRED,
    }
    for name, val in data_keys.items():
        st.write(("✅ " if val else "⬜ ") + name)
    st.divider()
    st.markdown("**키 불필요 (지금 동작)**")
    st.write("🔵 Damodaran — ERP / CRP / 법인세율")
    st.write("🟢 ECB — 환율")
    st.divider()
    st.caption("등급: 🟢공식 🔵참조 🟣계산 🔴LLM추정")

st.title("📊 SKSQ Valuation Agent")
st.caption("공개 데이터로, 할루시네이션 없이. 모든 숫자에 출처가 붙습니다.")

if not _llm["key"]:
    st.warning(f"`.env` 에 `{_llm['key_name']}` 를 넣어야 에이전트가 동작합니다. "
               "(채팅에 붙이지 말고 파일에 직접 입력)")

# 예시 질문
with st.expander("💡 예시 질문", expanded=False):
    st.markdown(
        "- 한국의 market risk premium은?\n"
        "- 한국·미국·일본·대만 ERP를 비교해줘\n"
        "- 원/달러 환율 알려줘\n"
        "- 한국 법인세율과 국가위험프리미엄은?"
    )

# ── 대화 기록 ──────────────────────────────────────────────────
if "history" not in st.session_state:
    st.session_state.history = []  # [{"role": "user"|"assistant", "content": str}]

for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


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
    else:
        st.error(f"`{name}` → {result['error']}")


def _fmt_args(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items())


# ── 입력 처리 ──────────────────────────────────────────────────
question = st.chat_input("밸류에이션·데이터 질문을 입력하세요…")
if question:
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        trace_box = st.status("🔍 데이터 소스 조회 중…", expanded=True)
        final_text = ""
        with trace_box:
            for ev in brain.answer(question):
                if ev["type"] == "tool_use":
                    st.write(f"🔧 호출: `{ev['name']}({_fmt_args(ev['input'])})`")
                elif ev["type"] == "tool_result":
                    render_tool_result(ev["name"], ev["input"], ev["result"])
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

    st.session_state.history.append({"role": "assistant", "content": final_text})

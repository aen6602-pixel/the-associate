"""Market Muse 답변 — 채널 글을 근거로 한 단일 호출 요약.

본 채팅(agent/brain.py)과 **일부러 다른 경로**다:
  · 도구를 쓰지 않는다. 검색 결과를 근거로 한 번만 부른다(다단계 tool-use 가 필요 없다).
  · 이 답변의 숫자는 **공신력이 없다**. 밸류에이션 엔진에 절대 자동으로 흘려보내지 않는다.
    가져가려면 사람이 본 채팅에 가정(assumption)으로 옮겨 적어야 한다.
"""
from __future__ import annotations

import json
from typing import Iterator

from core import config
from providers import marketmuse

# 원본 프로젝트(web/api/ask.js)의 시스템 프롬프트를 옮기되, **출처의 성격**을 분명히 하는
# 문단을 더했다 — 이 답변이 The Associate 안에서 공시 기반 답변과 나란히 보이기 때문이다.
SYSTEM_PROMPT = """\
너는 사용자가 구독하는 텔레그램 채널들의 글을 분석해 한국어로 답하는 시장·투자 정보 비서
'Market Muse' 다.

## 이 데이터의 성격 (반드시 지킬 것)
- 채널 글은 **공신력 있는 1차 자료가 아니다.** 공시도, 감사받은 재무제표도 아니며,
  작성자를 검증할 수 없고 정정·삭제 이력도 없다.
- 그러므로 **여기서 나온 수치를 확정된 사실처럼 쓰지 마라.** "OO 채널이 …라고 언급"
  처럼 항상 전언(傳言)임이 드러나게 쓴다.
- 밸류에이션에 쓸 값을 요구받으면, 그 값을 만들어 주지 말고 "이건 채널 언급이라
  가치평가의 근거로 쓰려면 공시로 확인해야 한다"고 밝혀라.

## 원칙
1. 반드시 아래 '채널 글' 내용에만 근거해 답하라. 제공된 글에 없는 사실을 추측하거나
   일반 지식으로 메우지 마라.
2. 각 글에는 [채널명 | 날짜]가 붙어 있다. 핵심 주장에는 근거 채널명과 날짜를 함께 표기하라.
3. 여러 글을 종합하라. 상충하면 더 최근 글을 우선하되 **상충한다는 사실 자체를 알려라.**
4. 구체적으로. 종목명·티커·수치·날짜·핵심 발언을 살리고 두루뭉술한 일반론은 피하라.

## 형식
- 핵심 결론 1~2문장 → 주제별 **굵은 소제목** + '- ' 불릿.
- 관련 내용이 거의 없으면 없다고 솔직히 말하고 무엇이 부족한지 알려라.
- 시장·투자와 무관한 잡담은 정중히 거절하라.
"""

MAX_CONTEXT_CHARS = 18000
MAX_HISTORY = 6


def _context(posts: list[dict]) -> str:
    """근거 글 묶음. 길이 상한에 걸리면 **최신 것부터** 담아 뒤를 자른다."""
    out, used = [], 0
    for p in posts:
        body = marketmuse.clean_text(p.get("text", ""))
        if not body:
            continue
        block = f"[{p.get('channel', '?')} | {str(p.get('date', ''))[:10]}]\n{body}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        out.append(block)
        used += len(block)
    return "\n\n---\n\n".join(out)


def answer(question: str, history: list[dict] | None = None,
           provider: str | None = None, model: str | None = None,
           channel: str | None = None) -> Iterator[dict]:
    """이벤트 스트림 — 본 채팅과 같은 모양이라 프론트가 로직을 공유한다.
    {"type": "sources"|"final"|"error", ...}"""
    provider = (provider or config.LLM_PROVIDER).lower()

    try:
        posts = marketmuse.search(question, limit=30, channel=channel)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"채널 데이터를 불러오지 못했습니다: {e}"}
        return

    if not posts:
        yield {"type": "final",
               "text": "구독 채널 글에서 관련 내용을 찾지 못했습니다. "
                       "다른 표현이나 종목명으로 다시 물어봐 주세요."}
        return

    yield {"type": "sources", "posts": [
        {"channel": p.get("channel"), "date": p.get("date"),
         "excerpt": marketmuse.clean_text(p.get("text", ""))[:180]} for p in posts]}

    msgs = [{"role": h["role"], "content": h["content"]}
            for h in (history or [])[-MAX_HISTORY:] if h.get("content")]
    msgs.append({"role": "user",
                 "content": f"[채널 글]\n{_context(posts)}\n\n[질문]\n{question}"})

    try:
        text = _chat(provider, model, SYSTEM_PROMPT, msgs)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"{provider} 호출 실패: {type(e).__name__}: {e}"}
        return
    yield {"type": "final", "text": text}


def _chat(provider: str, model: str | None, system: str, messages: list[dict]) -> str:
    """도구 없는 단발 호출. brain 의 tool-use 루프를 재사용하지 않는 이유는
    여기서 도구를 부를 일이 없고, 부를 수 있으면 안 되기 때문이다(공시 도구와 섞이면
    채널 전언이 공시 근거처럼 보이게 된다)."""
    info = config.resolve_llm(provider, model)
    model = info["model"]

    if provider == "gemini":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=config.require(config.Keys.GEMINI, "GEMINI_API_KEY"))
        contents = [types.Content(role=("model" if m["role"] == "assistant" else "user"),
                                  parts=[types.Part.from_text(text=m["content"])])
                    for m in messages]
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.3))
        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts if cand and cand.content else None) or []
        return "".join(p.text for p in parts if getattr(p, "text", None))

    if provider == "anthropic":
        # 본 채팅과 같은 이유로 CLI 를 쓴다(구독 재활용). 도구는 주지 않는다.
        from agent import brain

        out = []
        for ev in brain._answer_anthropic(messages[-1]["content"], None, 1, model, None, ""):
            if ev.get("type") == "final":
                out.append(ev.get("text") or "")
        return "\n".join(out)

    import openai

    if provider == "deepseek":
        key = config.require(config.Keys.DEEPSEEK, "DEEPSEEK_API_KEY")
        client = openai.OpenAI(api_key=key, base_url="https://api.deepseek.com")
    else:
        key = config.require(config.Keys.OPENAI, "OPENAI_API_KEY")
        client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "system", "content": system}] + messages)
    return resp.choices[0].message.content or ""

"""Market Muse 답변 — 채널 글을 근거로 한 단일 호출 요약.

본 채팅(agent/brain.py)과 **일부러 다른 경로**다:
  · 도구를 쓰지 않는다. 검색 결과를 근거로 한 번만 부른다(다단계 tool-use 가 필요 없다).
  · 이 답변의 숫자는 **공신력이 없다**. 밸류에이션 엔진에 절대 자동으로 흘려보내지 않는다.
    가져가려면 사람이 본 채팅에 가정(assumption)으로 옮겨 적어야 한다.
"""
from __future__ import annotations

import os
from typing import Iterator

from core import config
from providers import telegram_muse as tg

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

# 짧은 후속 질문("그거 왜?")은 그 자체로 검색해봐야 아무것도 안 걸린다. 직전 질문을
# 붙여서 찾되, 아무 짧은 문장에나 붙이면 엉뚱한 글이 딸려오므로 **명백한 지시어**가
# 있을 때만 붙인다.
_FOLLOWUP_CUES = (
    "그거", "그건", "그게", "그것", "그 종목", "그 회사", "그 이유", "방금", "아까",
    "위에", "거기", "자세히", "더 자세", "더 알려", "추가로", "이유는", "왜 그", "계속",
)


def _search_query(question: str, history: list[dict] | None) -> str:
    """후속 질문이면 직전 사용자 질문을 검색어에 보탠다."""
    prev = next((h["content"] for h in reversed(history or [])
                 if h.get("role") == "user" and h.get("content")), None)
    if not prev:
        return question
    if len(question) <= 6 or any(cue in question for cue in _FOLLOWUP_CUES):
        return f"{prev} {question}"
    return question


def _context(posts: list[dict]) -> str:
    """근거 글 묶음. 길이 상한에 걸리면 **최신 것부터** 담아 뒤를 자른다."""
    names = tg.aliases()
    out, used = [], 0
    for p in posts:
        body = tg.clean_text(p.get("text", ""))
        if not body:
            continue
        ch = p.get("channel", "?")
        head = f"{names.get(ch) or ch} | {str(p.get('date', ''))[:10]}"
        block = f"[{head}]\n{body}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        out.append(block)
        used += len(block)
    return "\n\n---\n\n".join(out)


def _sources_event(posts: list[dict], names: dict) -> dict:
    return {"type": "sources", "posts": [
        {"channel": p.get("channel"), "alias": names.get(p.get("channel"), ""),
         "date": p.get("date"), "excerpt": tg.clean_text(p.get("text", ""))[:180]}
        for p in posts]}


def answer(question: str, history: list[dict] | None = None,
           provider: str | None = None, model: str | None = None,
           channel: str | None = None) -> Iterator[dict]:
    """이벤트 스트림 — 본 채팅과 같은 모양이라 프론트가 로직을 공유한다.
    {"type": "scope"|"sources"|"final"|"error", ...}"""
    provider = (provider or config.LLM_PROVIDER).lower()
    names = tg.aliases()

    # 화면에서 채널을 고르지 않았다면 질문이 채널을 지목하는지 본다
    # ("잠실개미 채널에서 뭐래?" → 그 채널만).
    detected = None
    if not channel:
        channel, detected = tg.detect_channel(question)
    scope_label = (names.get(channel) or channel) if channel else None
    if scope_label:
        yield {"type": "scope", "channel": channel, "label": scope_label,
               "auto": detected is not None}

    try:
        posts = tg.search(_search_query(question, history), limit=30, channel=channel)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"채널 데이터를 불러오지 못했습니다: {e}"}
        return

    if not posts:
        where = f"'{scope_label}' 채널" if scope_label else "구독 채널 글"
        yield {"type": "final",
               "text": f"{where}에서 관련 내용을 찾지 못했습니다. "
                       "다른 표현이나 종목명으로 다시 물어봐 주세요."}
        return

    yield _sources_event(posts, names)

    msgs = [{"role": h["role"], "content": h["content"]}
            for h in (history or [])[-MAX_HISTORY:] if h.get("content")]
    scope = f"(사용자가 '{scope_label}' 채널을 지목했다 — 그 채널 글만 실려 있다)\n" if channel else ""
    msgs.append({"role": "user",
                 "content": f"{scope}[채널 글]\n{_context(posts)}\n\n[질문]\n{question}"})

    try:
        text = _chat(provider, model, SYSTEM_PROMPT, msgs)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"{provider} 호출 실패: {type(e).__name__}: {e}"}
        return
    yield {"type": "final", "text": text}


BRIEF_LIMIT = int(os.getenv("MUSE_BRIEF_POSTS", "200"))


def brief(provider: str | None = None, model: str | None = None,
          channel: str | None = None) -> Iterator[dict]:
    """질문 없이 '최근에 무슨 얘기가 도는지' 훑는다. 검색어가 없으니 관련도 순위가
    의미 없어 **최신순 그대로** 넣고, 주제로 묶는 일은 모델에 맡긴다."""
    provider = (provider or config.LLM_PROVIDER).lower()
    names = tg.aliases()
    try:
        posts = tg.recent(limit=BRIEF_LIMIT, channel=channel)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"채널 데이터를 불러오지 못했습니다: {e}"}
        return
    if not posts:
        yield {"type": "final",
               "text": "아직 모아둔 채널 글이 없습니다. 사이드바의 **지금 수집** 을 눌러주세요."}
        return

    if channel:
        yield {"type": "scope", "channel": channel,
               "label": names.get(channel) or channel, "auto": False}
    yield _sources_event(posts[:30], names)

    where = f"'{names.get(channel) or channel}' 채널의" if channel else "구독 채널들의"
    msgs = [{"role": "user", "content":
             f"[채널 글]\n{_context(posts)}\n\n[요청]\n위는 {where} 최근 글이다(최신순). "
             "지금 시장에서 무슨 얘기가 돌고 있는지 **주제별로 묶어** 브리핑하라. "
             "한 채널만 말한 얘기와 여러 채널이 함께 말한 얘기를 구분하고, "
             "여러 채널이 겹쳐 다룬 주제를 위로 올려라. 종목명·수치·날짜를 살려서 쓰고, "
             "각 항목 끝에 근거 채널명을 붙여라."}]
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

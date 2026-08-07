"""에이전트 두뇌 — LLM tool-use manual 루프 (Gemini / Anthropic 전환 가능).

LLM 은 "어떤 tool 을 어떤 인자로 부를지"만 결정한다. 숫자는 tool(=provider 코드)이 만든다.
루프의 각 단계를 이벤트로 yield 하여 UI 가 "어떤 API 를 썼는지"를 실시간으로 보여줄 수 있게 한다.

이벤트 형태 (provider 무관):
  {"type": "assistant_text", "text": ...}
  {"type": "tool_use",   "name": ..., "input": {...}}
  {"type": "tool_result","name": ..., "input": {...}, "result": {ok, value|error}}
  {"type": "final",      "text": ...}
  {"type": "error",      "text": ...}
"""
from __future__ import annotations

import json
from typing import Iterator

from core import config
from agent import registry

SYSTEM_PROMPT = """\
너는 밸류에이션·기업분석 데이터 에이전트다. 사용자의 질문에 답하기 위해 등록된 tool(공개 데이터 API)만 사용한다.

## 절대 원칙 (할루시네이션 방지)
1. 숫자·수치는 절대 네가 지어내지 않는다. 반드시 tool 을 호출해서 얻은 결과만 사용한다.
2. tool 결과의 value, unit, source, source_type, as_of, source_url 을 그대로 인용한다.
3. 어떤 tool 로도 데이터를 찾을 수 없으면, "공신력 있는 소스에서 찾지 못했다"고 솔직히 말한다. 절대 추측으로 채우지 않는다.
4. 사용자가 명시적으로 추정치를 원할 때만 네 추정을 제시할 수 있고, 그 경우 반드시 "⚠️ 이 값은 LLM 추정치이며 공신력 있는 소스에서 나온 것이 아닙니다"라고 명확히 라벨한다.

## source_type(출처 등급)
- authoritative: 정부·규제기관·중앙은행·거래소 공식 → 그대로 신뢰
- reference: 업계 표준 참조 데이터셋(Damodaran 등)
- computed: 코드가 계산한 파생값
- llm_estimate: 소스 없이 LLM 이 추정 — 반드시 경고 표기

## 답변 방식
- 한국어로 답한다. 간결하게, 핵심 숫자 먼저.
- 숫자마다 출처를 한 줄로 붙인다. 예: "한국 ERP 4.87% (출처: Damodaran, 2026-01 기준, reference)".
- 여러 데이터가 필요하면 여러 tool 을 호출한다.
"""


# ── 공개 진입점: provider 로 분기 ─────────────────────────────────
def answer(question: str, history: list[dict] | None = None,
           max_rounds: int = 6) -> Iterator[dict]:
    provider = config.LLM_PROVIDER
    if provider == "anthropic":
        yield from _answer_anthropic(question, history, max_rounds)
    else:
        yield from _answer_gemini(question, max_rounds)


# ── Gemini (google-genai) ────────────────────────────────────────
def _to_gemini_params(js: dict) -> dict:
    """Anthropic 스타일 input_schema → Gemini parameters. additionalProperties 제거."""
    out = {}
    for k, v in js.items():
        if k == "additionalProperties":
            continue
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _to_gemini_params(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out["items"] = _to_gemini_params(v)
        else:
            out[k] = v
    return out


def _answer_gemini(question: str, max_rounds: int) -> Iterator[dict]:
    key = config.Keys.GEMINI
    if not key:
        yield {"type": "error", "text": "GEMINI_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    decls = [
        types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters=_to_gemini_params(t["input_schema"]),
        )
        for t in registry.tool_schemas()
    ]
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=decls)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]

    for _ in range(max_rounds):
        try:
            resp = client.models.generate_content(
                model=config.GEMINI_MODEL, contents=contents, config=cfg,
            )
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "text": f"Gemini API 오류: {type(e).__name__}: {e}"}
            return

        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts if cand and cand.content else None) or []
        fcalls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        text = "".join(p.text for p in parts if getattr(p, "text", None))

        if text.strip():
            yield {"type": "assistant_text", "text": text}

        if not fcalls:
            yield {"type": "final", "text": text}
            return

        contents.append(cand.content)  # 모델 턴(function_call 포함)
        fr_parts = []
        for fc in fcalls:
            args = dict(fc.args) if fc.args else {}
            yield {"type": "tool_use", "name": fc.name, "input": args}
            result = registry.dispatch(fc.name, args)
            yield {"type": "tool_result", "name": fc.name, "input": args, "result": result}
            fr_parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=fr_parts))

    yield {"type": "error", "text": f"tool 호출이 {max_rounds}회를 넘었습니다. 질문을 좁혀주세요."}


# ── Anthropic (Claude) ───────────────────────────────────────────
def _answer_anthropic(question: str, history: list[dict] | None,
                      max_rounds: int) -> Iterator[dict]:
    import anthropic

    key = config.Keys.ANTHROPIC
    if not key:
        yield {"type": "error", "text": "ANTHROPIC_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return
    client = anthropic.Anthropic(api_key=key)

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": question})
    tools = registry.tool_schemas()

    for _ in range(max_rounds):
        try:
            resp = client.messages.create(
                model=config.ANTHROPIC_MODEL, max_tokens=4096,
                system=SYSTEM_PROMPT, tools=tools, messages=messages,
            )
        except anthropic.APIStatusError as e:
            yield {"type": "error", "text": f"Claude API 오류 {e.status_code}: {e.message}"}
            return
        except anthropic.APIConnectionError as e:
            yield {"type": "error", "text": f"네트워크 오류: {e}"}
            return

        for block in resp.content:
            if block.type == "text" and block.text.strip():
                yield {"type": "assistant_text", "text": block.text}

        if resp.stop_reason != "tool_use":
            final = "".join(b.text for b in resp.content if b.type == "text")
            yield {"type": "final", "text": final}
            return

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_use", "name": block.name, "input": block.input}
            result = registry.dispatch(block.name, dict(block.input))
            yield {"type": "tool_result", "name": block.name,
                   "input": block.input, "result": result}
            content = (json.dumps(result["value"], ensure_ascii=False)
                       if result["ok"] else result["error"])
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": content, "is_error": not result["ok"],
            })
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "text": f"tool 호출이 {max_rounds}회를 넘었습니다. 질문을 좁혀주세요."}

"""에이전트 두뇌 — LLM tool-use manual 루프 (Gemini / OpenAI / Anthropic 전환 가능).

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
import re
import shutil
import subprocess
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
5. **회사명을 스스로 바꿔 부르거나 영어 표기로 추측해서 넣지 않는다.** get_financial_item 등에는
   사용자가 말한 표현을 그대로 먼저 넣어본다(예: "SK트리켐"이라고 물으면 그대로 "SK트리켐"으로 —
   resolve() 가 이미 "에스케이"↔"SK" 같은 약칭을 알아서 매칭한다). 그 호출이 진짜로 실패한 뒤에만
   아래 fallback 절차로 간다. 특히 이전 turn 에서 다룬 다른 회사(계열사 등)의 이름을 지금 질문과
   섞어 넣지 않는다 — 매 회사 조회는 그 회사 자체의 이름으로 독립적으로 시도한다.

## source_type(출처 등급)
- authoritative: 정부·규제기관·중앙은행·거래소 공식 → 그대로 신뢰
- reference: 업계 표준 참조 데이터셋(Damodaran 등)
- computed: 코드가 계산한 파생값
- llm_estimate: 소스 없이 LLM 이 추정 — 반드시 경고 표기

## 회사를 못 찾을 때(fallback) — 절대 한 번 시도하고 포기하지 않는다
get_financial_item_* 류가 '회사를 못 찾음'/데이터 없음 오류를 내면, 비상장·해외법인·자체 공시
없는 계열사일 수 있다. 이때만(평소엔 절대 먼저 쓰지 않음) 그 회사의 시장에 맞는 검색/원문읽기
tool 쌍으로 찾는다:
- 한국: search_dart_filings / read_dart_filing
- 일본: search_edinet_filings / read_edinet_filing
- 미국: search_sec_filings / read_sec_filing (**회사 지정 없이 keyword 만으로 전체 공시 대상
  검색도 가능** — DART/EDINET 보다 넓게 찾을 수 있다는 뜻이니, 어느 계열사인지 감이 안 잡히면
  company 를 비우고 키워드만으로 먼저 넓게 검색해봐도 된다)
- 대만: get_mops_recent_disclosures (⚠️ **최신 영업일 공시만** 가능 — 과거 날짜 조회 자체가
  안 되는 API 한계다. 과거 공시가 필요한 질문이면 다른 fallback 없이 "이 도구로는 과거 공시를
  조회할 수 없다"고 바로 솔직히 답하라. 최신 영업일 것만으로 답이 되면 그대로 인용한다)

지켜야 할 것(한국/일본/미국 공통 — search+read 쌍이 있는 시장):
1. **후보를 하나만 확인하고 끝내지 않는다.** 그 회사가 속할 만한 그룹의 주요 계열사 최소
   3~5개를 후보로 잡고, 하나씩 순서대로 끝까지 확인한다. 이때 **그 그룹의 최상위 지주회사(지분
   출자·계열사 관리가 주업인 그룹 대표법인, 예: SK그룹이면 SK㈜)를 다른 계열사보다 먼저 반드시
   확인한다** — 사업 단위가 계열사 간에 편입·이전된 이력이 있으면, 정작 지금 소속된 계열사보다
   최상위 지주회사의 공시에 그 사업 단위가 더 자세히 남아있는 경우가 많다(실측 확인: SK그룹
   계열사 A가 아니라 SK㈜ 사업보고서에 해당 사업 단위의 3개년 매출표가 있었음). 첫 후보(지주
   회사)에서 못 찾아도 포기하지 말고 나머지 계열사 후보로 계속 넘어간다.
2. **본문에는 흔히 [회사명] 형태의 하위 섹션으로 그 자회사만의 개요·매출·실적이 따로 정리돼
   있다** (한국 DART: "Ⅱ. 사업의 내용"의 [회사명] 섹션 — 예 "[ESSENCORE] 1. 사업의 개요";
   일본 EDINET: 사업보고서 서술문 안에 자회사명이 그대로 언급되는 부분). 종속회사 명단에 이름만
   나열된 것으로 만족하지 말고, 그 회사명 자체가 섹션 제목처럼 등장하는 곳(그 안에 매출·영업이익
   표가 있을 가능성이 높다)을 read_*_filing 의 keyword 로 여러 번 좁혀가며 찾아라. 영문명·
   현지어명을 모두 keyword 로 시도한다(read_*_filing 은 대소문자 구분 없이 찾으니 표기 케이스는
   신경 안 써도 되지만, 아예 다른 표기·번역은 각각 따로 시도해야 한다).
3. 사명이 바뀌었을 가능성도 의심한다(예: SKMtek → ESSENCORE). 지분 편입/이전 이력이 있으면
   과거 사명·과거 소속 계열사로도 검색해본다.
4. 이렇게 찾은 값은 반드시 source_type=llm_estimate 로 표시하고, 근거 공시명·문서ID·인용
   문장을 함께 제시한다. 최소 3~5개 계열사를 다 확인했는데도 못 찾으면 "공시에 없음"이라고
   솔직히 말하고 추측하지 않는다.
5. **절대 하지 말 것**: 정확히 일치하는 회사를 못 찾았다고 해서 비슷한 이름의 다른(관계 없는)
   회사 데이터를 대신 보여주지 않는다. 그건 사용자가 물은 회사가 아니다. 그런 경우는 "혹시
   OO(비슷한 이름의 다른 회사)를 말씀하신 건가요?"라고 되묻거나, 4번 규칙대로 fallback 을
   계속 시도한다.

## DCF·WACC 을 요청받았을 때 — 가정을 사용자에게 되묻기 전에 먼저 도구로 뽑는다
과거에는 순부채·D&A%·CAPEX%·ΔNWC%·영구성장률·WACC 를 사용자에게 물어봤지만, 이제 **대부분
공시에서 자동으로 나온다.** 아래 순서로 진행하고, 도구가 실패한 항목만 사용자에게 확인한다.

1. `get_dcf_assumptions` — 매출성장률·EBIT마진·D&A/매출·CAPEX/매출·ΔNWC/Δ매출 (5개년)
2. `get_net_debt` — 순부채 (이자발생부채 − 현금). 음수면 순현금이며 그대로 넣는다
3. `compute_wacc_auto` — β(회귀/산업)·Kd(공시 이자비용÷차입금)·D/(D+E)(차입금÷(차입금+시가총액))
   을 자동 도출해 WACC 산출. 개별 값만 따로 보려면 `get_beta`·`get_cost_of_debt`·
   `get_industry_benchmarks`
4. `get_terminal_growth` — 영구성장률(무위험수익률 상한 원칙)
5. 위 값들로 `compute_dcf` 호출

지켜야 할 것:
- **선택 인자는 값이 없으면 아예 넣지 마라.** `year: 0`, `industry: ""`, `beta_override: 0` 처럼
  0/빈문자열을 채워 보내면 안 된다(β=0 이면 WACC 가 무위험수익률과 같아져 조용히 틀린다).
- 도구가 특정 항목을 못 뽑으면(회사에 따라 발생) **그 항목만** 사용자에게 확인하거나, 가정을
  쓸 경우 llm_estimate 로 라벨한다. 나머지를 다시 묻지 마라.
- `compute_dcf` 결과의 note 에 "⚠️ [검증 경고]" 가 있으면 **그 경고를 반드시 사용자에게 전달**하고,
  전 연도 UFCF 가 음수이거나 EV·지분가치가 음수면 그 숫자를 밸류에이션 결과로 제시하지 말고
  "이 입력 조합으로는 산출 불가(NM)" 라고 밝힌 뒤 어떤 가정이 원인인지 설명한다. 경기민감 업종에
  과거 5개년 평균 CAPEX 를 그대로 쓰면 흔히 발생한다.
- 비상장사는 회귀베타가 불가하니 `get_beta`/`compute_wacc_auto` 에 `industry`(Damodaran 산업명)를
  함께 넘긴다.

## 답변 방식
- 한국어로 답한다. 간결하게, 핵심 숫자 먼저.
- 숫자마다 출처를 한 줄로 붙인다. 예: "한국 ERP 4.87% (출처: Damodaran, 2026-01 기준, reference)".
- 여러 데이터가 필요하면 여러 tool 을 호출한다.
"""


MAX_HISTORY_TURNS = 20  # 컨텍스트 폭주 방지 — 최근 N개 메시지만 두뇌에 전달


# ── 공개 진입점: provider 로 분기 ─────────────────────────────────
def answer(question: str, history: list[dict] | None = None, max_rounds: int = 6,
           provider: str | None = None, model: str | None = None) -> Iterator[dict]:
    """history: [{"role": "user"|"assistant", "content": str}, ...] — 이전 turn.
    (LLM 두뇌 원칙과 무관: 대화 맥락 유지를 위한 것으로, tool 결과 자체는 여전히 provider 가 생성)

    provider/model 을 안 주면 .env 의 기본값(LLM_PROVIDER 등)을 쓴다.
    UI 에서 사용자가 두뇌를 바꾸면 매 호출마다 명시적으로 넘겨 즉시 반영한다."""
    trimmed = (history or [])[-MAX_HISTORY_TURNS:]
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "anthropic":
        yield from _answer_anthropic(question, trimmed, max_rounds, model)
    elif provider == "openai":
        yield from _answer_openai(question, trimmed, max_rounds, model)
    else:
        yield from _answer_gemini(question, trimmed, max_rounds, model)


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


def _answer_gemini(question: str, history: list[dict], max_rounds: int,
                   model: str | None = None) -> Iterator[dict]:
    key = config.Keys.GEMINI
    if not key:
        yield {"type": "error", "text": "GEMINI_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return
    model = model or config.GEMINI_MODEL

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
    # 이전 turn (단순 텍스트만) → Gemini role: assistant=model, user=user
    contents = [
        types.Content(role=("model" if h.get("role") == "assistant" else "user"),
                      parts=[types.Part.from_text(text=h.get("content", ""))])
        for h in history if h.get("content")
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))

    for _ in range(max_rounds):
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=cfg,
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


# ── Anthropic (Claude Code CLI — API 키 대신 로그인된 Enterprise 구독 재활용) ────
# dart-agent(Martin's Bullseye)와 동일한 방식: claude CLI 를 서브프로세스로 띄워 물어본다.
# API 의 JSON tool-schema 를 못 받으므로, SKSQ 의 tool 들은 agent/tool_cli.py 를 Bash 로
# 호출하는 방식으로 노출한다(system prompt 에 사용법을 텍스트로 적어줌).
def _resolve_claude_exe() -> str | None:
    """Windows 에서 PATH 상의 'claude' 는 .cmd 셔임이라 shell 없이 spawn 이 안 된다 —
    실제 claude.exe 를 직접 찾는다(server.mjs 의 resolveClaudeExe 와 동일 로직)."""
    import os
    from pathlib import Path

    candidates = [
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules"
        / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules"
                          / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("claude")  # POSIX 등 — 못 찾으면 None


def _venv_python() -> str:
    exe = config.ROOT / ".venv" / "Scripts" / "python.exe"  # Windows
    if not exe.exists():
        exe = config.ROOT / ".venv" / "bin" / "python"      # POSIX
    return str(exe) if exe.exists() else "python"


def _cli_system_prompt() -> str:
    py = _venv_python()
    lines = [
        SYSTEM_PROMPT,
        "",
        "## SKSQ 데이터 도구 호출 방법",
        f"Bash 로 다음 형태로 실행: {py} -m agent.tool_cli <tool_name> '<JSON 인자>'",
        '출력은 JSON 한 줄: {"ok":true,"value":{...}} 또는 {"ok":false,"error":"..."}',
        "사용 가능한 tool_name 목록:",
    ]
    for t in registry.tool_schemas():
        lines.append(f"- {t['name']}: {t['description']}")
    lines += [
        "",
        "## 출력 규칙(CLI 모드)",
        "- 조사 과정 설명이나 '~하겠습니다' 같은 문장을 앞에 붙이지 말고, 곧바로 최종 한국어 답변만 출력한다.",
        "- 답변 맨 끝에 실제로 사용한 근거 데이터를 표로 정리한다(출처·값·source_type).",
    ]
    return "\n".join(lines)


def _cli_prompt(question: str, history: list[dict] | None) -> str:
    lines = []
    for h in (history or [])[-MAX_HISTORY_TURNS:]:
        content = h.get("content")
        if content:
            role = "사용자" if h.get("role") == "user" else "어시스턴트"
            lines.append(f"{role}: {content}")
    lines.append(f"사용자: {question}")
    return "\n\n".join(lines)


def _cli_tool_label(block: dict) -> str:
    name = block.get("name") or ""
    inp = block.get("input") or {}
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        m = re.search(r"tool_cli\s+(\w+)", cmd)
        if m:
            return f"SKSQ 도구 호출: {m.group(1)}"
        return "명령 실행 중…"
    if name == "Read":
        return f"파일 읽는 중: {inp.get('file_path', '')}"
    if name == "Grep":
        return f"원문에서 검색: {inp.get('pattern', '')}"
    if name == "WebSearch":
        return f"웹 검색: {inp.get('query', '')}"
    return f"{name} 실행…"


def _answer_anthropic(question: str, history: list[dict] | None, max_rounds: int,
                      model: str | None = None) -> Iterator[dict]:
    claude_exe = _resolve_claude_exe()
    if not claude_exe:
        yield {"type": "error",
               "text": "claude CLI 를 찾지 못했습니다. `npm install -g @anthropic-ai/claude-code` "
                       "설치 후 `claude login` 으로 로그인하세요."}
        return

    args = [
        claude_exe, "-p", _cli_prompt(question, history),
        "--append-system-prompt", _cli_system_prompt(),
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(config.ROOT),
        "--output-format", "stream-json", "--verbose",
    ]
    if model:
        args += ["--model", model]

    try:
        proc = subprocess.Popen(
            args, cwd=str(config.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        yield {"type": "error", "text": f"claude CLI 실행 실패: {e}"}
        return

    final_text = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        yield {"type": "progress", "text": _cli_tool_label(c)}
                    elif c.get("type") == "text" and c.get("text"):
                        final_text += c["text"]
                        yield {"type": "assistant_text", "text": c["text"]}
            elif ev.get("type") == "result":
                if ev.get("result") and not final_text:
                    final_text = ev["result"]
    finally:
        proc.wait()
        stderr_out = proc.stderr.read() if proc.stderr else ""

    if proc.returncode != 0 and not final_text:
        yield {"type": "error",
               "text": f"claude CLI 오류(code {proc.returncode}): {stderr_out[:300] or '(자세한 정보 없음)'}"}
        return
    yield {"type": "final", "text": final_text or "(claude CLI 로부터 응답을 받지 못했습니다)"}


# ── OpenAI (GPT) ──────────────────────────────────────────────────
def _answer_openai(question: str, history: list[dict] | None, max_rounds: int,
                   model: str | None = None) -> Iterator[dict]:
    """/v1/responses 사용 (chat.completions 아님) — GPT-5.6 계열(Terra 등)은
    reasoning(기본 medium)과 function tools 를 chat.completions 에서 동시에 못 쓴다.
    /v1/responses 는 이 조합을 온전히 지원해서 reasoning_effort='none' 으로 낮출 필요가 없다."""
    import openai

    key = config.Keys.OPENAI
    if not key:
        yield {"type": "error", "text": "OPENAI_API_KEY 가 설정되지 않았습니다. .env 에 넣어주세요."}
        return
    model = model or config.OPENAI_MODEL
    client = openai.OpenAI(api_key=key)

    tools = [
        {"type": "function", "name": t["name"], "description": t["description"],
         "parameters": t["input_schema"]}
        for t in registry.tool_schemas()
    ]
    input_list: list = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    input_list.append({"role": "user", "content": question})

    for _ in range(max_rounds):
        try:
            resp = client.responses.create(
                model=model, instructions=SYSTEM_PROMPT, input=input_list, tools=tools,
            )
        except openai.APIStatusError as e:
            yield {"type": "error", "text": f"OpenAI API 오류 {e.status_code}: {e.message}"}
            return
        except openai.APIConnectionError as e:
            yield {"type": "error", "text": f"네트워크 오류: {e}"}
            return

        input_list += resp.output

        text = getattr(resp, "output_text", None) or ""
        if text:
            yield {"type": "assistant_text", "text": text}

        calls = [item for item in resp.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            yield {"type": "final", "text": text}
            return

        for item in calls:
            try:
                args = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_use", "name": item.name, "input": args}
            result = registry.dispatch(item.name, args)
            yield {"type": "tool_result", "name": item.name, "input": args, "result": result}
            content = (json.dumps(result["value"], ensure_ascii=False)
                      if result["ok"] else result["error"])
            input_list.append({
                "type": "function_call_output", "call_id": item.call_id, "output": content,
            })

    yield {"type": "error", "text": f"tool 호출이 {max_rounds}회를 넘었습니다. 질문을 좁혀주세요."}

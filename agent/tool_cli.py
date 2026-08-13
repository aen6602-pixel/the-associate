"""SKSQ tool CLI 진입점 — Claude Code CLI(서브프로세스, Enterprise 구독 재활용) 에이전트가
Bash 로 SKSQ 의 tool 을 호출할 때 쓴다. CLI 로 구동되는 Claude 는 API 의 JSON tool-schema 를
받지 못하고 자체 Bash/Read/Grep 만 가지므로, 이 스크립트를 실행하는 방식으로 registry 의
tool 들을 호출하게 한다(브라우저의 tool-use 대신 커맨드라인 진입점).

사용: <venv_python> -m agent.tool_cli <tool_name> '<JSON 인자>'
출력: registry.dispatch() 결과와 동일한 JSON 한 줄 — {"ok":true,"value":{...}} 또는 {"ok":false,"error":"..."}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python agent/tool_cli.py 직접 실행 대비

# Windows 기본 콘솔 codepage(cp949)로 stdout 이 잡히면 한글이 깨진 바이트로 나가
# (UnicodeEncodeError 도 아니고 조용히 깨짐) Claude CLI 가 잘못 읽는다 — UTF-8 로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent import registry  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "사용법: tool_cli.py <tool_name> [\'<json_args>\']"},
                         ensure_ascii=False))
        sys.exit(1)
    name = sys.argv[1]
    raw_args = sys.argv[2] if len(sys.argv) > 2 else "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"인자 JSON 파싱 실패: {e}"}, ensure_ascii=False))
        sys.exit(1)
    result = registry.dispatch(name, args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

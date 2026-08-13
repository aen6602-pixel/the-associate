"""Gemini 키를 받아 .env 를 UTF-8 로 깔끔히 재생성한다.
입력은 화면에 안 보이고(getpass), 채팅에도 안 남는다. 편집기/인코딩 문제를 전부 우회."""
import getpass
from pathlib import Path

key = getpass.getpass("Gemini API 키를 붙여넣고 Enter (화면에 표시되지 않음): ").strip()
if not key:
    raise SystemExit("입력이 비어있습니다. 다시 실행하세요.")

TEMPLATE = """\
# 이 파일은 git 에 올라가지 않습니다(.gitignore). 값만 = 뒤에 붙여넣으세요.

# --- 에이전트 두뇌 ---
LLM_PROVIDER=gemini

# Google Gemini (무료티어): https://aistudio.google.com/apikey
GEMINI_API_KEY={key}
GEMINI_MODEL=gemini-flash-latest

# Anthropic Claude (선택): https://console.anthropic.com
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-5

# --- 무료 등록 키 (데이터 소스) ---
DART_API_KEY=
ECOS_API_KEY=
FRED_API_KEY=
EDINET_API_KEY=
FINMIND_TOKEN=
OPENFIGI_API_KEY=

# --- 키 불필요 (연락처만) ---
SEC_USER_AGENT=sksq-agent sanghwalee@sksquare.com
"""

p = Path(".env").resolve()
p.write_text(TEMPLATE.format(key=key), encoding="utf-8")
print("대상:", p)
print(f"저장 완료 → 길이 {len(key)}, 시작 {key[:4]!r}  (보통 39자·'AIza' 시작이면 정상)")

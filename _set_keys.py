"""데이터 소스 키들을 .env 에 안전하게 써넣는다 (편집기 우회).
이미 값이 있는 키는 자동으로 건너뛴다 — 비어있는 것만 물어본다.
특정 키를 다시 입력(교체)하고 싶으면 실행 시 인자로 그 ENV 이름을 준다:
  python _set_keys.py OPENAI_API_KEY DART_API_KEY
각 키는 화면에 안 보이게 입력받고, 그냥 Enter 치면 건너뛴다. 채팅에도 안 남음."""
import getpass
import re
import sys
from pathlib import Path

# (env 변수명, 안내, 발급 URL)
FIELDS = [
    ("OPENAI_API_KEY", "OpenAI (두뇌)", "https://platform.openai.com/api-keys"),
    ("ANTHROPIC_API_KEY", "Anthropic Claude (두뇌, 선택)", "https://console.anthropic.com"),
    ("DEEPSEEK_API_KEY", "DeepSeek (두뇌, 선택)", "https://platform.deepseek.com/api_keys"),
    ("DART_API_KEY", "한국 DART (공시/재무)", "https://opendart.fss.or.kr"),
    ("ECOS_API_KEY", "한국은행 ECOS (국고채/매크로)", "https://ecos.bok.or.kr/api"),
    ("FRED_API_KEY", "미국 FRED (금리/매크로)", "https://fred.stlouisfed.org/docs/api/api_key.html"),
    ("EDINET_API_KEY", "일본 EDINET (공시)", "https://api.edinet-fsa.go.jp"),
    ("FINMIND_TOKEN", "대만 FinMind (재무/주가)", "https://finmindtrade.com"),
    ("OPENFIGI_API_KEY", "OpenFIGI (식별자 매핑, 선택)", "https://www.openfigi.com/api"),
]

FORCE = set(sys.argv[1:])  # 인자로 준 이름은 이미 값이 있어도 다시 물어봄

p = Path(".env")
text = p.read_text(encoding="utf-8")


def current_value(env_name: str) -> str:
    m = re.search(rf"(?m)^{env_name}=(.*)$", text)
    return (m.group(1) if m else "").strip()


print("비어있는 키만 물어봅니다. 이미 설정된 키는 자동으로 건너뜁니다.")
print("(특정 키를 다시 입력하려면: python _set_keys.py 키이름)\n")

changed, skipped = [], []
for env_name, label, url in FIELDS:
    cur = current_value(env_name)
    if cur and env_name not in FORCE:
        skipped.append((env_name, len(cur)))
        continue
    val = getpass.getpass(f"[{label}] {env_name} (없으면 Enter): ").strip()
    if not val:
        continue
    if re.search(rf"(?m)^{env_name}=.*$", text):
        text = re.sub(rf"(?m)^{env_name}=.*$", f"{env_name}={val}", text)
    else:
        text = text.rstrip() + f"\n{env_name}={val}\n"
    changed.append((env_name, len(val)))

p.write_text(text, encoding="utf-8")

if skipped:
    print("\n이미 설정되어 건너뜀:")
    for name, ln in skipped:
        print(f"  {name}: {ln}자")

print("\n이번에 저장됨 (값 아닌 길이만 표시):")
if changed:
    for name, ln in changed:
        print(f"  {name}: {ln}자")
else:
    print("  (없음)")

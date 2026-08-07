"""데이터 소스 키들을 .env 에 안전하게 써넣는다 (편집기 우회).
각 키는 화면에 안 보이게 입력받고, 그냥 Enter 치면 건너뛴다(기존 값 유지). 채팅에도 안 남음."""
import getpass
import re
from pathlib import Path

# (env 변수명, 안내, 발급 URL)
FIELDS = [
    ("DART_API_KEY", "한국 DART (공시/재무)", "https://opendart.fss.or.kr"),
    ("ECOS_API_KEY", "한국은행 ECOS (국고채/매크로)", "https://ecos.bok.or.kr/api"),
    ("FRED_API_KEY", "미국 FRED (금리/매크로)", "https://fred.stlouisfed.org/docs/api/api_key.html"),
    ("EDINET_API_KEY", "일본 EDINET (공시)", "https://api.edinet-fsa.go.jp"),
    ("FINMIND_TOKEN", "대만 FinMind (재무/주가)", "https://finmindtrade.com"),
    ("OPENFIGI_API_KEY", "OpenFIGI (식별자 매핑, 선택)", "https://www.openfigi.com/api"),
]

p = Path(".env")
text = p.read_text(encoding="utf-8")
print("각 키를 붙여넣고 Enter. 없거나 건너뛰려면 그냥 Enter.\n")

changed = []
for env_name, label, url in FIELDS:
    val = getpass.getpass(f"[{label}] {env_name} (없으면 Enter): ").strip()
    if not val:
        continue
    if re.search(rf"(?m)^{env_name}=.*$", text):
        text = re.sub(rf"(?m)^{env_name}=.*$", f"{env_name}={val}", text)
    else:
        text = text.rstrip() + f"\n{env_name}={val}\n"
    changed.append((env_name, len(val)))

p.write_text(text, encoding="utf-8")
print("\n저장 완료 (값 아닌 길이만 표시):")
if changed:
    for name, ln in changed:
        print(f"  {name}: {ln}자")
else:
    print("  (변경 없음)")

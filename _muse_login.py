"""텔레그램 로그인 — Market Muse 수집에 쓸 세션을 한 번 만든다.

왜 이 과정이 필요한가: 채널 글은 **봇 토큰으로 읽을 수 없다**(봇은 자신이 속한 대화만 본다).
사람 계정으로 로그인해야 하고, 그 로그인은 전화로 오는 인증코드를 넣어야 해서 서버에서
자동화할 수 없다. 그래서 이 PC 에서 한 번만 로그인하고, 그 결과인 '세션 문자열' 만
서버로 넘긴다.

실행: `텔레그램 로그인.bat` 을 더블클릭하거나
      .venv\\Scripts\\python.exe _muse_login.py

끝나면 .env 의 TG_SESSION_STRING 을 **이 스크립트가 직접 채운다** — 긴 문자열을 손으로
복사하다 한 글자 빠뜨리는 일을 없애려는 것이다.
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows 콘솔 기본 코드페이지(cp949)에서는 '—' 같은 문자가 그대로 죽는다.
# .bat 에서 chcp 65001 을 하더라도 파이썬 쪽 스트림을 따로 맞춰야 안전하다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 구형 파이썬/리다이렉트 환경
        pass

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
load_dotenv(ENV)


def die(msg: str) -> None:
    print()
    print("[중단] " + msg)
    print()
    input("Enter 를 누르면 창이 닫힙니다...")
    sys.exit(1)


try:
    # telethon.sync 를 import 해야 메서드가 '기다려주는' 형태가 된다. 그냥 telethon 에서
    # 가져오면 connect()/sign_in() 이 코루틴만 만들고 아무 일도 하지 않는다(실측 확인).
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import (ApiIdInvalidError, PhoneNumberInvalidError,
                                 PhoneCodeInvalidError, SessionPasswordNeededError)
except ImportError:
    die("telethon 이 설치되지 않았습니다.\n"
        "     먼저 이걸 실행하세요:  .venv\\Scripts\\python.exe -m pip install -r requirements.txt")

api_id = (os.getenv("TG_API_ID") or "").strip()
api_hash = (os.getenv("TG_API_HASH") or "").strip()
if not api_id.isdigit() or not api_hash:
    die("먼저 .env 에 TG_API_ID(숫자)와 TG_API_HASH 를 넣으세요.\n"
        "     https://my.telegram.org → API development tools")

print("=" * 62)
print("  텔레그램 로그인 — Market Muse 채널 수집용")
print("=" * 62)
print()
print("  · 전화번호는 국가번호를 붙여 입력하세요.")
print("      예) 010-1234-5678  →  +821012345678")
print("  · 인증코드는 문자(SMS)가 아니라 **텔레그램 앱**으로 옵니다.")
print("      폰에서 Telegram 을 열어 'Telegram' 공식 채팅을 확인하세요.")
print("  · 2단계 인증을 쓰신다면 비밀번호도 한 번 더 물어봅니다.")
print()

client = TelegramClient(StringSession(), int(api_id), api_hash)

try:
    client.connect()
    if not client.is_connected():
        raise ConnectionError("연결이 수립되지 않았습니다")
except Exception as e:  # noqa: BLE001
    # 실측(사내망): TCP 443 은 열리는데 MTProto 가 시작되자마자 끊긴다
    # ("0 bytes read on a total of N expected bytes"). 방화벽/DPI 가 텔레그램 고유
    # 프로토콜을 걸러내는 전형적인 모습이라, 연결 방식을 바꿔도 전부 막힌다.
    blocked = isinstance(e, (ConnectionError, OSError)) or "bytes read" in str(e)
    if blocked:
        die("텔레그램 서버에 연결하지 못했습니다.\n\n"
            "     지금 쓰는 네트워크가 텔레그램 프로토콜(MTProto)을 막고 있을 가능성이\n"
            "     높습니다. 사내망에서 흔한 일입니다.\n\n"
            "     해결: **휴대폰 핫스팟**이나 집 네트워크에 연결한 뒤 다시 실행하세요.\n"
            "           로그인은 한 번만 하면 되고, 그 뒤에는 이 PC 가 텔레그램에\n"
            "           접속할 일이 없습니다(수집은 서버에서 돕니다).\n\n"
            f"     (원인: {type(e).__name__}: {str(e)[:80]})")
    die(f"텔레그램에 연결하지 못했습니다: {type(e).__name__}: {e}")

try:
    phone = input("전화번호 (+82...): ").strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        print("  ↳ '+' 로 시작해야 합니다. 앞에 +82 를 붙여 다시 입력하세요.")
        phone = input("전화번호 (+82...): ").strip().replace(" ", "").replace("-", "")

    client.send_code_request(phone)
    print()
    print("  텔레그램 앱으로 코드를 보냈습니다.")
    code = input("받은 인증코드 (숫자): ").strip()

    try:
        client.sign_in(phone, code)
    except SessionPasswordNeededError:
        print()
        print("  2단계 인증이 켜져 있습니다.")
        import getpass

        pw = getpass.getpass("텔레그램 2단계 비밀번호 (화면에 안 보임): ")
        client.sign_in(password=pw)

except PhoneNumberInvalidError:
    die("전화번호 형식이 올바르지 않습니다. 국가번호를 붙이고 0 은 빼세요 (+821012345678).")
except PhoneCodeInvalidError:
    die("인증코드가 틀렸습니다. 다시 실행해 새 코드를 받으세요.")
except ApiIdInvalidError:
    die("TG_API_ID / TG_API_HASH 가 올바르지 않습니다. my.telegram.org 값을 다시 확인하세요.")
except KeyboardInterrupt:
    die("사용자가 취소했습니다.")
except Exception as e:  # noqa: BLE001
    die(f"{type(e).__name__}: {e}")

me = client.get_me()
session = client.session.save()
client.disconnect()

# .env 에 직접 써 넣는다 — 긴 문자열을 손으로 옮기다 잘리는 사고를 없앤다.
text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
line = f"TG_SESSION_STRING={session}"
if re.search(r"^TG_SESSION_STRING=.*$", text, flags=re.M):
    text = re.sub(r"^TG_SESSION_STRING=.*$", line, text, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
ENV.write_text(text, encoding="utf-8")

print()
print("=" * 62)
print(f"  로그인 완료: {me.first_name or ''} (@{me.username or '-'})")
print(f"  .env 에 TG_SESSION_STRING 을 저장했습니다. (길이 {len(session)})")
print("=" * 62)
print()
print("  다음: 앱을 실행하고 왼쪽 사이드바의 'Market Muse' 를 열면")
print("        채널 수집이 자동으로 시작됩니다 (처음엔 2~5분).")
print()
print("  ⚠️ 이 세션 문자열은 계정 접근 권한 그 자체입니다.")
print("     채팅·메일에 붙여넣지 마세요. 유출되면 텔레그램 앱의")
print("     [설정 → 기기] 에서 해당 세션을 종료하면 무효화됩니다.")
print()
input("Enter 를 누르면 창이 닫힙니다...")

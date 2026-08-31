"""텔레그램 세션 문자열을 한 번 만든다 (Market Muse 수집용).

왜 필요한가: 채널 글은 **봇 토큰으로 읽을 수 없다**(봇은 자신이 속한 대화만 본다).
사람 계정으로 로그인해야 하고, 그 로그인은 전화로 오는 인증코드를 넣어야 해서
서버에서 자동화할 수 없다. 그래서 이 PC 에서 한 번 로그인해 '세션 문자열' 을 얻고,
그 문자열만 서버 환경변수로 넘긴다.

  1) https://my.telegram.org → API development tools → api_id / api_hash 발급
  2) .env 에 TG_API_ID, TG_API_HASH 를 넣는다
  3) python _muse_login.py   ← 전화번호와 인증코드 입력
  4) 출력된 한 줄을 .env 의 TG_SESSION_STRING 에 넣는다 (Railway 에도 같은 값)

⚠️ 이 문자열은 **계정 접근 권한 그 자체**다. 채팅·메일·저장소에 붙이지 말고,
   유출되면 텔레그램 앱의 '설정 → 기기'에서 해당 세션을 종료하라.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("telethon 이 설치되지 않았습니다:  pip install -r requirements.txt")

api_id = (os.getenv("TG_API_ID") or "").strip()
api_hash = (os.getenv("TG_API_HASH") or "").strip()
if not api_id.isdigit() or not api_hash:
    sys.exit("먼저 .env 에 TG_API_ID(숫자)와 TG_API_HASH 를 넣으세요. "
             "https://my.telegram.org → API development tools")

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    me = client.get_me()
    print()
    print(f"로그인 완료: {me.first_name or ''} (@{me.username or '-'})")
    print()
    print("아래 한 줄을 .env 의 TG_SESSION_STRING 에 넣으세요 "
          "(공백 없이 통째로, 채팅에 붙여넣지 마세요):")
    print()
    print(client.session.save())
    print()

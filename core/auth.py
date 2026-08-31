"""로그인 게이트 — 지정한 소수만 앱에 들어오게 한다. (웹 프레임워크 비의존)

호스팅(Railway)엔 앱을 감싸주는 로그인 장치가 없다 → URL 을 아는 사람은 누구나 들어오므로
앱이 직접 게이트를 걸어야 한다. 이 모듈은 **검증과 토큰 서명만** 담당하고, 쿠키를 굽고 401 을
내는 것은 `server/main.py` 가 한다(테스트하기 쉽고, UI 프레임워크에 묶이지 않는다).

설정:
- `APP_USERS="이름:비번,이름2:비번2"` — 사람별 계정. 대화기록도 사람별로 격리된다(권장).
- `APP_PASSWORD="..."` — 공용 비밀번호 1개. 간단하지만 대화기록을 모두가 공유한다.
- 둘 다 없으면 게이트 없음(로컬 개발). 단 `DEPLOY_MODE=1` 이면 앱이 열리지 않는다(fail-closed).
- `SESSION_SECRET` — 로그인 쿠키 서명 키. 배포에선 반드시 지정(없으면 프로세스마다 랜덤 →
  재배포·재시작 때 전원 로그아웃).
- `SESSION_TTL_DAYS` — 로그인 유지 기간(기본 7일).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import NamedTuple

COOKIE_NAME = "assoc_session"


class Viewer(NamedTuple):
    key: str      # 세션 파일 격리용 키 (core.history 의 user_key)
    label: str    # 화면에 보여줄 이름


LOCAL_VIEWER = Viewer(key="local", label="local")


def _hash(s: str) -> str:
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()[:16]


# ── 설정 ─────────────────────────────────────────────────────────
def _env(name: str) -> str:
    """환경변수를 읽되 **값을 감싼 따옴표를 벗긴다.**

    .env 파일에서는 값을 따옴표로 감싸는 게 관행이고 dotenv 가 알아서 벗겨준다. 그런데
    호스팅(Railway 등)의 변수 편집기는 적은 문자를 그대로 넘기므로 따옴표가 값에 남는다.
    실측 사고: APP_USERS="sanghwa:pw,team:pw2" 로 넣자 아이디가 '"sanghwa' 가 되고 마지막
    비밀번호가 'pw2"' 가 되어 **아무도 로그인할 수 없었다.** 자기 앱에서 잠기는 실패는
    관대하게 받아주는 편이 맞다.
    """
    v = (os.getenv(name) or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def users() -> dict[str, str]:
    """{이름(소문자): 비밀번호}. APP_USERS 가 우선, 없으면 APP_PASSWORD 공용 1개(이름="")."""
    out: dict[str, str] = {}
    for pair in _env("APP_USERS").split(","):
        name, sep, pw = pair.partition(":")
        if sep and name.strip() and pw.strip():
            out[name.strip().lower()] = pw.strip()
    if out:
        return out
    shared = _env("APP_PASSWORD")
    return {"": shared} if shared else {}


def is_configured() -> bool:
    """로그인 게이트가 켜져 있는지."""
    return bool(users())


def user_key_for(name: str) -> str:
    """이름 → 대화기록 폴더 키. `authenticate()` 가 만드는 키와 반드시 같아야 한다
    (관리자 페이지가 이름으로 남의 세션 폴더를 찾을 때 쓴다)."""
    label = (name or "").strip().lower() or "member"
    return _hash(f"pw:{label}")


def admins() -> set[str]:
    """관리자 이름 집합. `ADMIN_USERS` 로 지정하고, 없으면 `APP_USERS` 의 **첫 번째** 계정.

    공용 비밀번호(APP_PASSWORD)만 쓰는 설정은 사용자를 구분할 수 없으므로 관리자도 없다."""
    raw = _env("ADMIN_USERS")
    names = {n.strip().lower() for n in raw.replace(";", ",").split(",") if n.strip()}
    if names:
        return names
    table = users()
    if not table or list(table) == [""]:
        return set()
    return {next(iter(table))}


def is_admin(v: Viewer) -> bool:
    return bool(v) and v.label.strip().lower() in admins()


def needs_name() -> bool:
    """로그인 화면에 이름 칸을 보여줄지 (공용 비밀번호 1개면 불필요)."""
    return list(users()) != [""]


def ttl_seconds() -> int:
    try:
        days = float(os.getenv("SESSION_TTL_DAYS", "7"))
    except ValueError:
        days = 7.0
    return int(max(days, 0.01) * 86400)


_FALLBACK_SECRET = secrets.token_hex(32)  # SESSION_SECRET 미설정 시 프로세스 한정


def _secret() -> bytes:
    return (_env("SESSION_SECRET") or _FALLBACK_SECRET).encode("utf-8")


def secret_is_ephemeral() -> bool:
    """SESSION_SECRET 이 없어 재시작 시 전원 로그아웃되는 상태인지 (기동 로그 경고용)."""
    return not _env("SESSION_SECRET")


# ── 비밀번호 검증 ────────────────────────────────────────────────
def authenticate(name: str, password: str) -> Viewer | None:
    """맞으면 Viewer, 틀리면 None. 이름 없는 설정(공용 비번)에선 name 을 무시한다."""
    table = users()
    if not table:
        return None
    key = "" if list(table) == [""] else (name or "").strip().lower()
    expected = table.get(key)
    if not expected:
        return None
    # 비ASCII 비밀번호에서 compare_digest 가 TypeError 를 내지 않도록 bytes 로 비교.
    if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        return None
    label = key or "member"
    return Viewer(key=_hash(f"pw:{label}"), label=label)


# ── 로그인 쿠키 (서명 토큰) ──────────────────────────────────────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(v: Viewer, now: float | None = None) -> str:
    """`payload.signature` — 서버만 서명할 수 있으므로 위조 불가(내용 자체는 비밀이 아니다)."""
    exp = int((now if now is not None else time.time()) + ttl_seconds())
    payload = _b64e(json.dumps({"k": v.key, "l": v.label, "e": exp},
                               separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def parse_token(token: str | None, now: float | None = None) -> Viewer | None:
    """서명·만료가 유효하면 Viewer, 아니면 None."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    expected = _b64e(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(payload))
        key, label, exp = str(data["k"]), str(data["l"]), int(data["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now if now is not None else time.time()) > exp:
        return None
    # 계정 목록에서 지워진 사람은 남아있는 쿠키로도 못 들어오게 한다.
    table = users()
    if table and label != "member" and label not in table:
        return None
    return Viewer(key=key, label=label)

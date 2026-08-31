"""공용 HTTP 계층 — 재시도 + 디스크 캐시.

Damodaran xlsx 처럼 크고 갱신이 드문 파일은 캐시해서 매 질문마다 다시 받지 않는다.
API 호출도 짧은 TTL 로 캐시해 rate-limit 을 피한다.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import CACHE_DIR

_session: Optional[requests.Session] = None

# API 키/토큰은 대부분 provider 가 URL 쿼리 파라미터로 넘긴다(crtfc_key, Subscription-Key,
# token, key 등). requests.HTTPError 의 기본 메시지는 응답 URL 전체(쿼리 포함)를 담으므로,
# 에러 하나 잘못 print/로그하면 그대로 비밀값이 노출된다 — 여기서 한 번에 마스킹한다.
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)\b(token|key|crtfc_key|subscription-key|api_key|apikey)=[^&\s]+"
)


def _sanitize(text: str) -> str:
    return _SENSITIVE_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)


def _content_type(r: "requests.Response") -> str:
    return (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()


# 재시도 — 이 모듈은 오래 전부터 설명문에 '재시도' 를 달고 있었지만 실제 코드는 없었다.
# 실측(2026-08-31): SEC 전체검색이 500 을 한 번 뱉어 도구 호출이 '데이터 조회 실패' 로
# 끝났는데, 같은 요청을 바로 다시 보내니 200 이었다. 공개 API 는 이런 순간적 흔들림이
# 정상 범위이므로 한 번의 blip 이 사용자에게 실패로 보이면 안 된다.
# 400/403/404 처럼 **우리 요청이 틀린** 경우는 다시 보내도 같은 답이라 즉시 올린다.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_ATTEMPTS = 3
_BACKOFF_SEC = 0.6


def _request(method: str, url: str, **kw):
    """일시적 실패(연결 끊김·타임아웃·429/5xx)만 지수 백오프로 재시도한다.

    session().request(...) 가 아니라 .get/.post 를 그대로 부른다 — 호출 형태를 바꾸면
    세션을 감싸거나 대역으로 바꿔 쓰는 쪽이 조용히 깨진다.
    """
    last_exc = None
    last_resp = None
    call = getattr(session(), method.lower())
    for attempt in range(_ATTEMPTS):
        try:
            r = call(url, **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc, last_resp = e, None
        else:
            if r.status_code not in _RETRY_STATUSES:
                return r
            last_exc, last_resp = None, r
        if attempt < _ATTEMPTS - 1:
            time.sleep(_BACKOFF_SEC * (2 ** attempt))
    if last_resp is not None:
        return last_resp   # 호출부의 raise_for_status 가 기존과 같은 메시지로 처리한다
    raise last_exc


def _reject_error_page(r: "requests.Response", url: str) -> None:
    """HTTP 200 인데 HTML 을 준 응답을 **캐시에 넣기 전에** 걸러낸다.

    200 이라고 성공이 아니다. 실측 사고(2026-08): EDINET 이 API 를 다른 호스트로 옮긴 뒤
    구 호스트가 200 + HTML 에러페이지("規定外操作が行われました")를 돌려주기 시작했다.
    바이너리 경로는 그 HTML 을 30일 캐시에 저장했고, 호출부는 한참 뒤 zipfile 단계에서
    'BadZipFile: File is not a zip file' 로 터졌다 — 죽은 URL 과 아무 관계 없어 보이는
    메시지라 진단이 오래 걸렸고, 캐시 때문에 재배포해도 계속 실패했다.
    """
    if _content_type(r) != "text/html":
        return
    from .schema import DataError

    raise DataError(
        f"바이너리 응답을 기대했지만 HTML 페이지를 받았습니다(HTTP {r.status_code}). "
        f"엔드포인트가 이전·폐지됐거나 인증이 거부됐을 가능성이 높습니다. "
        f"URL: {_sanitize(url)}")


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "sksq-agent/0.1"})
        _session = s
    return _session


def _cache_path(url: str, suffix: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{h}{suffix}"


def get_bytes(url: str, ttl_hours: float = 24 * 7, headers: dict | None = None,
              params: dict | None = None, timeout: int = 30) -> bytes:
    """캐시된 GET(바이너리). 큰 파일(엑셀/XBRL)용."""
    full = url
    if params:
        full += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cp = _cache_path(full, ".bin")
    if cp.exists() and (time.time() - cp.stat().st_mtime) < ttl_hours * 3600:
        return cp.read_bytes()
    r = _request("GET", url, headers=headers, params=params, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
    _reject_error_page(r, full)   # 오염된 응답을 캐시에 굳히기 전에 막는다
    cp.write_bytes(r.content)
    return r.content


def get_json(url: str, ttl_hours: float = 6, headers: dict | None = None,
             params: dict | None = None, timeout: int = 30,
             is_empty=None, empty_ttl_hours: float = 1.0) -> dict:
    """캐시된 GET(JSON). API 호출용.

    is_empty: 응답이 '데이터 없음' 인지 판정하는 콜백. 빈 응답은 **짧게만**(empty_ttl_hours)
      캐시한다. 왜 필요한가 — DART 는 아직 접수되지 않은 사업연도를 물으면
      {"status":"013","message":"조회된 데이타가 없습니다"} 를 준다. 이걸 3일 캐시하면
      보고서가 접수된 뒤에도 최대 3일간 "그 연도 데이터 없음" 을 계속 믿게 되고,
      '최신 사업연도' 판정이 그만큼 늦어진다(리노공업 FY2025 사례).
    """
    import json

    full = url
    if params:
        full += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cp = _cache_path(full, ".json")
    if cp.exists():
        age = time.time() - cp.stat().st_mtime
        cached = None
        if age < max(ttl_hours, empty_ttl_hours) * 3600:
            cached = json.loads(cp.read_text(encoding="utf-8"))
        if cached is not None:
            limit = ttl_hours
            if is_empty is not None:
                try:
                    if is_empty(cached):
                        limit = empty_ttl_hours
                except Exception:  # noqa: BLE001 — 판정 실패는 일반 TTL 로 처리
                    pass
            if age < limit * 3600:
                return cached
    r = _request("GET", url, headers=headers, params=params, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
    try:
        data = r.json()
    except ValueError:
        # 예전엔 여기서 raw JSONDecodeError("Expecting value: line 1 column 1")가 그대로
        # 올라가 '무엇이 잘못됐는지' 를 전혀 알 수 없었다 — 죽은 엔드포인트가 200+HTML 을
        # 주는 흔한 경우를 이름 붙여 알려준다.
        from .schema import DataError

        raise DataError(
            f"JSON 응답을 기대했지만 {_content_type(r) or '알 수 없는 형식'} 을 받았습니다"
            f"(HTTP {r.status_code}). 엔드포인트가 이전·폐지됐거나 인증이 거부됐을 수 "
            f"있습니다. URL: {_sanitize(full)}") from None
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def post_json(url: str, json_body, ttl_hours: float = 24 * 7, headers: dict | None = None,
              timeout: int = 30):
    """캐시된 POST(JSON body → JSON). 매핑류 API(OpenFIGI 등)처럼 조회가 POST인 경우용.
    캐시 키는 URL + 요청 바디(정렬된 JSON) 기준."""
    import json

    body_key = json.dumps(json_body, sort_keys=True, ensure_ascii=False)
    cp = _cache_path(url + "|" + body_key, ".json")
    if cp.exists() and (time.time() - cp.stat().st_mtime) < ttl_hours * 3600:
        return json.loads(cp.read_text(encoding="utf-8"))
    r = _request("POST", url, json=json_body, headers=headers, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
    data = r.json()
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data

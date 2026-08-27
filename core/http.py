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
    r = session().get(url, headers=headers, params=params, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
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
    r = session().get(url, headers=headers, params=params, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
    data = r.json()
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
    r = session().post(url, json=json_body, headers=headers, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise requests.exceptions.HTTPError(_sanitize(str(e)), response=r) from None
    data = r.json()
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data

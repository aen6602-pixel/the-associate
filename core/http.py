"""공용 HTTP 계층 — 재시도 + 디스크 캐시.

Damodaran xlsx 처럼 크고 갱신이 드문 파일은 캐시해서 매 질문마다 다시 받지 않는다.
API 호출도 짧은 TTL 로 캐시해 rate-limit 을 피한다.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

import requests

from .config import CACHE_DIR

_session: Optional[requests.Session] = None


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
    r.raise_for_status()
    cp.write_bytes(r.content)
    return r.content


def get_json(url: str, ttl_hours: float = 6, headers: dict | None = None,
             params: dict | None = None, timeout: int = 30) -> dict:
    """캐시된 GET(JSON). API 호출용."""
    import json

    full = url
    if params:
        full += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cp = _cache_path(full, ".json")
    if cp.exists() and (time.time() - cp.stat().st_mtime) < ttl_hours * 3600:
        return json.loads(cp.read_text(encoding="utf-8"))
    r = session().get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data

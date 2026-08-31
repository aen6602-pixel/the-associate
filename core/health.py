"""데이터 소스 헬스체크 — "키가 있다" 가 아니라 "실제로 응답한다" 를 확인한다.

왜 필요한가(실측 2026-08-31): EDINET 이 API 호스트를 옮긴 뒤 구 호스트가 HTTP 200 + HTML
에러페이지를 주기 시작했고, 일본 기업 조회가 통째로 실패하는 동안에도 사이드바는 계속
'✅ 연결' 이었다. 그 표시가 본 것은 **환경변수에 키가 있는지**뿐이었기 때문이다.
키 존재는 소스가 살아있다는 근거가 아니다.

설계상 지켜야 할 것:
  · **캐시를 우회한다.** provider 의 일반 경로는 디스크 캐시를 먼저 보므로, 엔드포인트가
    죽어도 캐시에 남은 옛 성공 응답 때문에 계속 정상으로 보인다(위 사고가 정확히 그랬다).
    각 provider 의 ping() 은 core.http.probe 로 매번 실제 호출을 한다.
  · **병렬로 돌린다.** 12개를 순차로 부르면 20초가 넘어 화면이 그만큼 멈춘다.
  · **결과를 캐시한다.** 매 접속마다 전 소스를 두드리면 그 자체가 rate-limit 유발이다.
  · **키 없음과 고장을 구분한다.** 키를 안 넣은 것은 장애가 아니다.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from core import config, sources
from core.cache import ttl_cache

log = logging.getLogger("associate")

# 소스 카탈로그의 name → 그 소스의 ping(). sources.SOURCES 와 이름으로 맞춘다.
_PROBES: dict = {}


def _load_probes() -> dict:
    """provider import 를 모듈 로드 시점이 아니라 첫 호출 때 한다 — health 를 import 하는
    것만으로 12개 provider 가 딸려 올라오면 서버 기동이 느려지고 실패 지점도 늘어난다."""
    global _PROBES
    if _PROBES:
        return _PROBES
    from providers import (damodaran, dart, ecos, edinet, finmind, fred, fx, mops,
                           naver, openfigi, sec, yahoo)

    _PROBES = {
        "DART": dart.ping,
        "ECOS": ecos.ping,
        "FRED": fred.ping,
        "Damodaran": damodaran.ping,
        "ECB": fx.ping,
        "Naver 금융": naver.ping,
        "Yahoo Finance": yahoo.ping,
        "SEC EDGAR": sec.ping,
        "EDINET": edinet.ping,
        "FinMind": finmind.ping,
        "OpenFIGI": openfigi.ping,
        "MOPS": mops.ping,
    }
    return _PROBES


# 결과 캐시 수명. 짧으면 접속마다 전 소스를 두드리는 것과 다를 바 없고, 길면 장애를 늦게
# 안다. 사용자는 '다시 확인' 으로 언제든 강제 갱신할 수 있으므로 넉넉히 잡는다.
TTL_HEALTH = 10 * 60.0
_PROBE_TIMEOUT = 20.0     # 개별 ping 하나가 전체를 잡아두지 않게


def _run_one(name: str, fn) -> dict:
    t = time.monotonic()
    try:
        detail = fn()
        return {"name": name, "state": "up", "detail": str(detail),
                "ms": int((time.monotonic() - t) * 1000)}
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 '이 소스가 죽었다' 로 보고한다
        return {"name": name, "state": "down",
                "detail": f"{type(e).__name__}: {e}"[:300],
                "ms": int((time.monotonic() - t) * 1000)}


def check_all() -> list[dict]:
    """전 소스를 병렬로 확인한다(캐시 없음). state: up | down | nokey | planned."""
    probes = _load_probes()
    by_name = {s["name"]: s for s in sources.SOURCES}
    out: list[dict] = []
    to_probe: dict = {}

    for name, fn in probes.items():
        s = by_name.get(name)
        if s is not None:
            code, _ = sources.status(s)
            if code != "live":     # 키 미설정·미연동은 장애가 아니다 — 두드리지 않는다
                out.append({"name": name, "state": "nokey" if code == "nokey" else "planned",
                            "detail": "키가 설정되지 않았습니다" if code == "nokey"
                                      else "아직 연동되지 않았습니다", "ms": 0})
                continue
        to_probe[name] = fn

    # 어느 소스가 죽었을 때 '무엇이' 안 되는지까지 알려준다 — 이름만 붉게 띄우면
    # 전면 장애처럼 읽히지만, 실제로는 그 소스를 쓰는 기능만 막힌다.
    used_by = {s["name"]: s.get("used_by") for s in sources.SOURCES}

    if to_probe:
        with ThreadPoolExecutor(max_workers=min(8, len(to_probe))) as pool:
            futures = {pool.submit(_run_one, n, f): n for n, f in to_probe.items()}
            for fut, name in futures.items():
                try:
                    out.append(fut.result(timeout=_PROBE_TIMEOUT))
                except FuturesTimeout:
                    out.append({"name": name, "state": "down",
                                "detail": f"{_PROBE_TIMEOUT:.0f}초 안에 응답이 없습니다",
                                "ms": int(_PROBE_TIMEOUT * 1000)})

    order = list(probes)
    out.sort(key=lambda r: order.index(r["name"]) if r["name"] in order else 99)
    for r in out:
        r["used_by"] = used_by.get(r["name"])

    # 실패는 서버 로그에도 남긴다 — 사이드바 문구는 그 순간 화면을 본 사람만 보고,
    # 배포 환경에서만 나는 실패는 나중에 로그로 되짚는 수밖에 없다.
    for r in out:
        if r["state"] == "down":
            log.warning("source down: %s (%dms) %s", r["name"], r["ms"], r["detail"])
    return out


@ttl_cache(TTL_HEALTH, maxsize=1)
def _cached() -> dict:
    rows = check_all()
    return {"checked_at": time.time(), "ttl_seconds": TTL_HEALTH, "sources": rows,
            "down": [r["name"] for r in rows if r["state"] == "down"]}


def snapshot(force: bool = False) -> dict:
    """캐시된 결과. force=True 면 즉시 다시 확인한다('다시 확인' 버튼)."""
    if force:
        _cached.cache_clear()
    return _cached()

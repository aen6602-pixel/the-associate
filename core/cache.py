"""시간 제한이 있는 메모이제이션.

`functools.lru_cache` 는 **프로세스가 사는 동안 절대 만료되지 않는다.** 로컬 CLI 에서는
문제가 안 되지만 배포된 서버(uvicorn 프로세스가 몇 주~몇 달 유지)에서는 이것이 곧
"새로 올라온 공시를 영원히 못 보는" 버그가 된다.

실측된 사고(2026-08): 리노공업 최근 3개년을 물었더니 FY2022~FY2024 가 나왔다. 리노공업의
FY2025 사업보고서는 2026-03-18 에 접수됐는데, 서버 프로세스가 그 이전에
`dart._latest_year(리노공업) = 2024` 를 한 번 계산해 lru_cache 에 넣어둔 뒤로는 재시작
없이는 절대 2025 를 보지 못했다. 디스크 HTTP 캐시(3일 TTL)를 아무리 짧게 잡아도
그 위의 lru_cache 가 무기한이면 의미가 없다.

그래서 **"무엇이 최신인가" 를 판단하는 함수에는 반드시 TTL 을 건다.** 종목 매핑처럼
자주 안 바뀌는 것은 길게(24h), 최신 연도·재무 시계열처럼 공시로 갱신되는 것은 짧게(6h).
"""
from __future__ import annotations

import functools
import threading
import time
from typing import Callable

HOUR = 3600.0

# 이 프로젝트에서 쓰는 기본 TTL — 의미를 이름으로 고정해 호출부에서 숫자를 고민하지 않게 한다.
TTL_FRESH = 6 * HOUR      # 공시로 갱신되는 것(최신 사업연도, 재무 시계열)
TTL_INDEX = 24 * HOUR     # 종목·기업 매핑, 참조 데이터셋


def ttl_cache(seconds: float, maxsize: int = 256) -> Callable:
    """lru_cache 와 같은 사용법이지만 seconds 가 지나면 만료된다.

    - 예외는 캐시하지 않는다(lru_cache 와 동일). 조회 실패가 굳어버리면 안 된다.
    - 인자는 hashable 이어야 한다.
    - `.cache_clear()` 로 즉시 비울 수 있다(테스트·관리자용).
    """
    if seconds <= 0:
        raise ValueError("ttl_cache 의 seconds 는 0 보다 커야 합니다")

    def decorator(fn: Callable) -> Callable:
        store: dict = {}
        lock = threading.Lock()
        stats = {"hits": 0, "misses": 0}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items()))) if kwargs else args
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit is not None and hit[0] > now:
                    stats["hits"] += 1
                    return hit[1]
                stats["misses"] += 1
            value = fn(*args, **kwargs)      # 락 밖에서 실행 — 느린 API 가 다른 키를 막지 않게
            with lock:
                if len(store) >= maxsize:
                    # 만료가 임박한 것부터 버린다(대략적 LRU 대신 TTL 순).
                    for k in sorted(store, key=lambda k: store[k][0])[: max(1, maxsize // 4)]:
                        store.pop(k, None)
                store[key] = (time.monotonic() + seconds, value)
            return value

        def cache_clear() -> None:
            with lock:
                store.clear()
                stats["hits"] = stats["misses"] = 0

        def cache_info() -> dict:
            with lock:
                return {"size": len(store), "maxsize": maxsize,
                        "ttl_seconds": seconds, **stats}

        wrapper.cache_clear = cache_clear      # type: ignore[attr-defined]
        wrapper.cache_info = cache_info        # type: ignore[attr-defined]
        return wrapper

    return decorator


def clear_all() -> int:
    """등록된 provider/engine 캐시를 전부 비운다 → 비운 개수.

    "지금 막 올라온 공시를 당장 보고 싶다" 는 경우의 탈출구다. 디스크 HTTP 캐시는
    core.http 가 TTL 로 관리하므로 여기서는 메모리 캐시만 다룬다.
    """
    import importlib

    targets = [
        ("providers.dart", ("_latest_year", "_corp_index")),
        ("providers.dart_audit", ("_report_text", "_audit_reports")),
        ("providers.sec", ("_ticker_index", "_company_facts")),
        ("providers.edinet", ("_company_index", "_doc_rows")),
        ("providers.finmind", ("_company_index", "_income_rows", "_balance_rows",
                               "_cashflow_rows")),
        ("providers.damodaran", ("_load",)),
        ("engines.business_mix", ("_cached",)),
    ]
    cleared = 0
    for mod_name, fn_names in targets:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 — 키가 없어 import 가 실패하는 provider 는 건너뛴다
            continue
        for fn_name in fn_names:
            fn = getattr(mod, fn_name, None)
            clear = getattr(fn, "cache_clear", None)
            if callable(clear):
                clear()
                cleared += 1
    return cleared

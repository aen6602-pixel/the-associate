"""데이터 소스 실측 점검.

사이드바의 '✅ 연결' 은 원래 **환경변수에 키가 있는지**만 봤다. EDINET 이 API 호스트를 옮겨
일본 조회가 통째로 죽은 동안에도 계속 '연결' 이었다(2026-08 실측). 키 존재는 소스가
살아있다는 근거가 아니다 — 여기서 고정하는 것은 그 구분이다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import health, sources
from core.schema import DataError
from server.main import app


@pytest.fixture
def probes(monkeypatch):
    """provider 를 타지 않는 가짜 ping 세트로 교체한다(네트워크 없음).

    conftest 가 모든 API 키를 비우므로 기본값이면 전부 'nokey' 로 빠져 두드리지 않는다 —
    up/down 로직을 보려면 '키는 있다' 상태를 만들어야 한다(키 없음 경로는 별도 테스트).
    """
    def _set(mapping, live=True):
        monkeypatch.setattr(health, "_PROBES", dict(mapping))
        monkeypatch.setattr(health, "_load_probes", lambda: dict(mapping))
        if live:
            monkeypatch.setattr(sources, "status", lambda s: ("live", "✅ 연결"))
        health._cached.cache_clear()
    yield _set
    health._cached.cache_clear()


def test_a_dead_source_is_reported_down_not_connected(probes):
    probes({
        "DART": lambda: "OK",
        "EDINET": lambda: (_ for _ in ()).throw(DataError("데이터 대신 HTML 페이지를 받았습니다")),
    })
    snap = health.snapshot(force=True)
    by = {r["name"]: r for r in snap["sources"]}
    assert by["DART"]["state"] == "up"
    assert by["EDINET"]["state"] == "down"
    assert "HTML" in by["EDINET"]["detail"], "왜 죽었는지가 결과에 남아야 한다"
    assert snap["down"] == ["EDINET"]


def test_all_healthy_reports_nothing_down(probes):
    probes({"DART": lambda: "OK", "EDINET": lambda: "OK"})
    assert health.snapshot(force=True)["down"] == []


def test_one_slow_source_does_not_hide_the_others(monkeypatch, probes):
    """개별 ping 이 멈춰도 나머지 결과는 나와야 한다."""
    import time

    monkeypatch.setattr(health, "_PROBE_TIMEOUT", 0.3)
    probes({"DART": lambda: "OK", "EDINET": lambda: time.sleep(5)})
    by = {r["name"]: r for r in health.snapshot(force=True)["sources"]}
    assert by["DART"]["state"] == "up"
    assert by["EDINET"]["state"] == "down"
    assert "응답이 없습니다" in by["EDINET"]["detail"]


def test_missing_key_is_not_a_failure(monkeypatch, probes):
    """키를 안 넣은 것과 고장난 것은 다르다 — 키가 없으면 두드리지도 않는다."""
    called = []
    probes({"EDINET": lambda: called.append(1) or "OK"}, live=False)
    monkeypatch.setattr(sources, "status", lambda s: ("nokey", "⬜ 키 필요"))
    by = {r["name"]: r for r in health.snapshot(force=True)["sources"]}
    assert by["EDINET"]["state"] == "nokey"
    assert not called, "키가 없으면 네트워크를 두드리지 않아야 한다"


def test_results_are_cached_until_forced(probes):
    calls = []
    probes({"DART": lambda: calls.append(1) or "OK"})
    health.snapshot(force=True)
    health.snapshot()
    health.snapshot()
    assert len(calls) == 1, "매 접속마다 전 소스를 두드리면 그 자체가 rate-limit 유발이다"
    health.snapshot(force=True)
    assert len(calls) == 2, "'다시 확인' 은 캐시를 무시해야 한다"


# ── provider 계약 ────────────────────────────────────────────────
def test_every_wired_source_has_a_probe():
    """카탈로그에 연동됐다고 적힌 소스는 점검 방법이 있어야 한다 — 없으면 그 소스는
    영원히 '확인되지 않은 채 초록불' 이 된다."""
    probes = health._load_probes()
    missing = [s["name"] for s in sources.SOURCES
               if s.get("wired") and s["name"] not in probes]
    assert not missing, f"ping() 이 없는 연동 소스: {missing}"


def test_probes_do_not_go_through_the_disk_cache():
    """ping 은 get_json/get_bytes(캐시 우선)가 아니라 probe(캐시 우회)를 써야 한다.
    캐시를 타면 엔드포인트가 죽어도 옛 성공 응답 때문에 계속 정상으로 보인다."""
    import inspect

    from providers import (damodaran, dart, ecos, edinet, finmind, fred, fx, mops,
                           naver, openfigi, sec, yahoo)

    offenders = []
    for m in (damodaran, dart, ecos, edinet, finmind, fred, fx, mops,
              naver, openfigi, sec, yahoo):
        src = inspect.getsource(m.ping)
        if "probe(" not in src:
            offenders.append(m.__name__)
        if "get_json(" in src or "get_bytes(" in src:
            offenders.append(f"{m.__name__}(캐시 경로 사용)")
    assert not offenders, offenders


# ── HTTP 경계 ────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "alice", "password": "pw-alice"}).status_code == 200
    return c


def test_endpoint_returns_states(client, probes):
    probes({"DART": lambda: "OK",
            "EDINET": lambda: (_ for _ in ()).throw(DataError("죽음"))})
    d = client.get("/api/health/sources?refresh=true").json()
    assert d["down"] == ["EDINET"]
    assert {r["name"]: r["state"] for r in d["sources"]} == {"DART": "up", "EDINET": "down"}


def test_endpoint_requires_login(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")
    anon = TestClient(app)
    assert anon.get("/api/health/sources").status_code == 401

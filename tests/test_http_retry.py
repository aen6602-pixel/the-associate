"""일시적 네트워크 실패 재시도.

core.http 는 오래 전부터 설명문에 '재시도' 를 달고 있었지만 실제 코드는 없었다. 실측
(2026-08-31): SEC 전체검색이 500 을 한 번 뱉어 도구 호출이 '데이터 조회 실패' 로 끝났는데,
같은 요청을 곧바로 다시 보내니 200(hits=40) 이었다. 공개 API 의 순간적 흔들림이 사용자에게
실패로 보이면 안 된다 — 반대로 우리가 잘못 부른 요청(4xx)까지 재시도하면 느려지기만 한다.
"""
from __future__ import annotations

import pytest
import requests

from core import http as core_http


class _Resp:
    def __init__(self, status: int, body: bytes = b'{"ok": 1}',
                 ctype: str = "application/json"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = body
        self.text = body.decode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        import json

        return json.loads(self.content.decode())


class _FakeSession:
    """정해진 순서대로 응답/예외를 돌려주고 호출 횟수를 센다."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def _next(self, url, **kw):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.script_default
        if isinstance(item, Exception):
            raise item
        return item

    get = _next
    post = _next
    script_default = _Resp(200)


@pytest.fixture
def no_sleep(monkeypatch):
    """백오프 대기를 건너뛴다 — 테스트가 실제로 잠들 이유는 없다."""
    monkeypatch.setattr(core_http.time, "sleep", lambda s: None)


def _use(monkeypatch, script) -> _FakeSession:
    fake = _FakeSession(script)
    monkeypatch.setattr(core_http, "session", lambda: fake)
    return fake


@pytest.fixture
def url(request):
    """테스트마다 다른 URL — get_json/get_bytes 는 URL 해시로 디스크 캐시를 타므로,
    URL 을 공유하면 앞 테스트가 캐시에 남긴 성공 응답 때문에 세션이 호출되지도 않는다."""
    safe = "".join(c if c.isalnum() else "-" for c in request.node.name)
    return f"https://x.example/{safe}"


@pytest.mark.parametrize("transient", [
    _Resp(500), _Resp(502), _Resp(503), _Resp(504), _Resp(429),
    requests.exceptions.ConnectionError("connection reset"),
    requests.exceptions.Timeout("timed out"),
])
def test_transient_failure_is_retried_then_succeeds(monkeypatch, no_sleep, url, transient):
    fake = _use(monkeypatch, [transient, _Resp(200, b'{"hits": 40}')])
    assert core_http.get_json(url)["hits"] == 40
    assert fake.calls == 2, "일시적 실패는 한 번 더 시도해야 한다"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(monkeypatch, no_sleep, url, status):
    """우리 요청이 틀린 경우는 다시 보내도 같은 답이다 — 즉시 올린다."""
    fake = _use(monkeypatch, [_Resp(status, b"nope", "text/plain")])
    with pytest.raises(requests.exceptions.HTTPError):
        core_http.get_json(url)
    assert fake.calls == 1


def test_persistent_failure_gives_up_and_reports(monkeypatch, no_sleep, url):
    fake = _use(monkeypatch, [_Resp(503), _Resp(503), _Resp(503)])
    with pytest.raises(requests.exceptions.HTTPError):
        core_http.get_json(url)
    assert fake.calls == core_http._ATTEMPTS


def test_persistent_connection_error_raises_the_original(monkeypatch, no_sleep, url):
    boom = requests.exceptions.ConnectionError("dns failure")
    fake = _use(monkeypatch, [boom, boom, boom])
    with pytest.raises(requests.exceptions.ConnectionError, match="dns failure"):
        core_http.get_json(url)
    assert fake.calls == core_http._ATTEMPTS


def test_success_on_first_try_does_not_retry(monkeypatch, no_sleep, url):
    fake = _use(monkeypatch, [_Resp(200, b'{"v": 1}')])
    assert core_http.get_json(url)["v"] == 1
    assert fake.calls == 1


def test_binary_path_retries_too(monkeypatch, no_sleep, url):
    fake = _use(monkeypatch, [_Resp(500),
                              _Resp(200, b"PK\x03\x04zip", "application/octet-stream")])
    assert core_http.get_bytes(url).startswith(b"PK")
    assert fake.calls == 2

"""EDINET 엔드포인트 이전 사고(2026-08-31) 회귀 테스트.

증상: 일본 기업 조회가 통째로 실패. 화면에는 서로 무관해 보이는 오류 4종이 찍혔다 —
`JSONDecodeError` / `BadZipFile` / `ValueError: invalid literal for int(): '1-'` /
`EDINET 에서 기업을 못 찾음: '285A0'`. 원인은 하나가 아니라 다섯 겹이었고 각각을 여기서 고정한다.

  1) API 호스트가 disclosure.edinet-fsa.go.jp → **api.edinet-fsa.go.jp** 로 이전됐는데,
     구 호스트가 404 가 아니라 **HTTP 200 + HTML 에러페이지**를 준다(規定外操作が行われました).
  2) core.http 가 그 HTML 을 성공으로 받아 **30일 캐시에 저장**했다 → 재배포해도 계속 실패하고,
     터지는 지점은 zipfile/json 단계라 죽은 URL 과 무관해 보이는 메시지만 남았다.
  3) 일본 증권코드는 2024년부터 **영숫자**('285A' 키옥시아)인데 `q.isdigit()` 로 걸러버렸다.
  4) 일문 사명이 완전일치만 허용돼 '株式会社' 를 뺀 일반 표기가 전부 실패했다.
  5) YYYYMMDD 파싱이 하이픈 섞인 ISO 표기('2026-01-01')에 크래시했다.
"""
from __future__ import annotations

import csv
import io
import zipfile

import pytest

from core import http as core_http
from core.schema import DataError
from providers import edinet


# ── 1) 죽은 호스트로 되돌아가지 않는다 ──────────────────────────────
def test_base_url_points_at_the_live_api_host():
    assert edinet._BASE.startswith("https://api.edinet-fsa.go.jp/"), (
        "disclosure.edinet-fsa.go.jp/api/v2 는 폐지됐고 HTTP 200 + HTML 에러페이지를 준다")


# ── 2) 200 인데 HTML 인 응답을 캐시에 굳히지 않는다 ─────────────────
class _FakeResp:
    def __init__(self, ctype: str, body: bytes, status: int = 200):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def raise_for_status(self):
        return None

    def json(self):
        import json

        return json.loads(self.content.decode("utf-8"))


def _stub_session(monkeypatch, resp):
    class _S:
        def get(self, *a, **k):
            return resp

    monkeypatch.setattr(core_http, "session", lambda: _S())


_ERROR_PAGE = "<!DOCTYPE html><html><body>規定外操作が行われました。</body></html>".encode("utf-8")


@pytest.mark.parametrize("fn, url", [
    (core_http.get_bytes, "https://dead.example/api/v2/documents/S1"),
    (core_http.get_json, "https://dead.example/api/v2/documents.json"),
])
def test_html_error_page_is_rejected_and_never_cached(monkeypatch, fn, url):
    _stub_session(monkeypatch, _FakeResp("text/html; charset=utf-8", _ERROR_PAGE))
    with pytest.raises(DataError) as ei:
        fn(url, params={"Subscription-Key": "SECRET123"})
    msg = str(ei.value)
    assert "HTML" in msg or "text/html" in msg
    assert "SECRET123" not in msg, "에러 메시지에 API 키가 새면 안 된다"

    # 두 번째 호출도 같은 오류여야 한다 — 캐시에 굳었다면 응답을 안 보고 그 쓰레기를 돌려준다.
    with pytest.raises(DataError):
        fn(url, params={"Subscription-Key": "SECRET123"})


def test_valid_responses_still_cache(monkeypatch):
    """방어가 정상 응답까지 막으면 안 된다."""
    _stub_session(monkeypatch, _FakeResp("application/json", b'{"results": [1, 2]}'))
    assert core_http.get_json("https://ok.example/a.json")["results"] == [1, 2]
    _stub_session(monkeypatch, _FakeResp("application/octet-stream", b"PK\x03\x04zip"))
    assert core_http.get_bytes("https://ok.example/a.zip").startswith(b"PK")


# ── 3~4) 회사 식별: 영숫자 증권코드 · 티커 접미사 · 일문 약식 표기 ──
def _codelist_zip() -> bytes:
    """실제 EdinetcodeDlInfo.csv 구조 그대로 — 첫 줄은 헤더가 아니라 안내문이다."""
    header = ["ＥＤＩＮＥＴコード", "提出者名", "提出者名（英字）", "証券コード", "決算日"]
    rows = [
        ["E35948", "キオクシアホールディングス株式会社", "Kioxia Holdings Corporation", "285A0", "3月末日"],
        ["E02144", "トヨタ自動車株式会社", "TOYOTA MOTOR CORPORATION", "72030", "3月末日"],
        ["E01777", "ソニーグループ株式会社", "SONY GROUP CORPORATION", "67580", "3月末日"],
    ]
    buf = io.StringIO()
    buf.write("これはEDINETコードリストです\n")   # 안내문 한 줄
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("EdinetcodeDlInfo.csv", buf.getvalue().encode("cp932"))
    return zbuf.getvalue()


@pytest.fixture
def codelist(monkeypatch):
    monkeypatch.setattr(edinet, "get_bytes", lambda *a, **k: _codelist_zip())
    edinet._company_index.cache_clear()
    yield
    edinet._company_index.cache_clear()


@pytest.mark.parametrize("query, want_code", [
    ("285A0", "E35948"),   # 5자리형(4자리 + 0)
    ("285A", "E35948"),    # 4자리 영숫자 — 2024년 이후 신규상장은 전부 이 형태다
    ("285a", "E35948"),    # 대소문자 무시
    ("285A.T", "E35948"),  # Yahoo 티커 접미사
    ("72030", "E02144"),
    ("7203", "E02144"),
    ("7203.T", "E02144"),
])
def test_alphanumeric_securities_codes_resolve(codelist, query, want_code):
    assert edinet.resolve(query)["edinet_code"] == want_code


@pytest.mark.parametrize("query", [
    "キオクシアホールディングス株式会社",   # 등록명 그대로
    "キオクシアホールディングス",           # 株式会社 를 뺀 일반 표기
    "Kioxia",
    "KIOXIA HOLDINGS",
])
def test_company_names_resolve_with_and_without_legal_suffix(codelist, query):
    assert edinet.resolve(query)["edinet_code"] == "E35948"


def test_code_shaped_name_falls_through_to_name_lookup(codelist):
    """'SONY' 는 증권코드 모양(4자 영숫자)이지만 코드가 아니다. 예전엔 코드 조회 실패 즉시
    raise 해서, 이름으로는 찾을 수 있는 회사를 놓쳤다."""
    assert edinet.resolve("SONY")["edinet_code"] == "E01777"


def test_unknown_company_still_errors_clearly(codelist):
    with pytest.raises(DataError, match="못 찾음"):
        edinet.resolve("전혀없는회사이름xyz")


# ── 5) 날짜 파싱 ────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["20260101", "2026-01-01", "2026/01/01"])
def test_ymd_accepts_iso_and_compact(raw):
    from datetime import date

    assert edinet._ymd(raw, "bgn_de") == date(2026, 1, 1)


@pytest.mark.parametrize("bad", ["yesterday", "2026-01", "", "202601011"])
def test_ymd_rejects_garbage_with_a_readable_message(bad):
    with pytest.raises(DataError, match="YYYYMMDD"):
        edinet._ymd(bad, "bgn_de")

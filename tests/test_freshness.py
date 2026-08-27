"""최신 회계연도 신선도 — 리노공업 FY2022~2024 사고 대응.

증상: "최근 3개년" 을 물었더니 FY2025 사업보고서(2026-03-18 접수)가 있는데도 FY2022~2024 가
나왔다. 원인이 세 겹이었고 각각을 여기서 고정한다.

  1) 도구 스키마가 `예: 2024` 를 하드코딩하고 기본값을 '직전연도' 라고 잘못 설명 →
     LLM 이 year=2024 를 넣게 유도. 그러면 3개년이 정확히 2022~2024 가 된다.
  2) 다년치 조회 도구가 없어서 연도별 단건 호출을 반복해야 했고, 그 과정에서 연도를 직접 찍음.
  3) `functools.lru_cache` 가 프로세스 수명 동안 만료되지 않아, 배포 서버가 3월 이전에
     계산한 '최신=2024' 를 재시작 전까지 계속 반환.
"""
from __future__ import annotations

import time

import pytest

from core import cache as cache_mod
from core.cache import ttl_cache
from core.schema import DataError


# ── (3) TTL 캐시 ──────────────────────────────────────────────────────
def test_ttl_cache_expires():
    calls = []

    @ttl_cache(0.05)
    def f(x):
        calls.append(x)
        return len(calls)

    assert f(1) == 1
    assert f(1) == 1, "TTL 안에서는 캐시"
    assert len(calls) == 1
    time.sleep(0.07)
    assert f(1) == 2, "TTL 이 지나면 다시 호출해야 한다"
    assert len(calls) == 2


def test_ttl_cache_does_not_cache_exceptions():
    """조회 실패가 굳어버리면 복구되지 않는다."""
    state = {"fail": True}

    @ttl_cache(60)
    def f():
        if state["fail"]:
            raise DataError("일시 장애")
        return "ok"

    with pytest.raises(DataError):
        f()
    state["fail"] = False
    assert f() == "ok"


def test_ttl_cache_clear_and_info():
    @ttl_cache(60)
    def f(x):
        return x * 2

    f(1)
    f(2)
    assert f.cache_info()["size"] == 2
    f.cache_clear()
    assert f.cache_info()["size"] == 0


def test_ttl_cache_evicts_at_maxsize():
    @ttl_cache(60, maxsize=4)
    def f(x):
        return x

    for i in range(12):
        f(i)
    assert f.cache_info()["size"] <= 4


def test_ttl_cache_rejects_nonpositive_ttl():
    with pytest.raises(ValueError):
        ttl_cache(0)


def test_freshness_critical_caches_have_ttl():
    """'무엇이 최신인가' 를 판정하는 함수에 무기한 캐시를 다시 붙이지 못하게 한다."""
    from engines import business_mix
    from providers import dart, finmind, sec

    for fn, label in ((dart._latest_year, "dart._latest_year"),
                      (dart._corp_index, "dart._corp_index"),
                      (sec._company_facts, "sec._company_facts"),
                      (sec._ticker_index, "sec._ticker_index"),
                      (finmind._income_rows, "finmind._income_rows"),
                      (business_mix._cached, "business_mix._cached")):
        info = getattr(fn, "cache_info", None)
        assert callable(info), f"{label}: 캐시 정보가 없다"
        assert "ttl_seconds" in info(), f"{label}: TTL 없는 캐시(lru_cache)를 쓰고 있다"


def test_latest_year_ttl_is_short_enough_to_pick_up_new_filings():
    """사업보고서는 3월에 몰려 접수된다 — 하루보다 짧아야 그날 안에 반영된다."""
    from providers import dart

    assert dart._latest_year.cache_info()["ttl_seconds"] <= 12 * 3600


def test_clear_all_returns_count():
    assert cache_mod.clear_all() > 0


# ── (1) 스키마가 연도를 유도하지 않는다 ────────────────────────────────
def _schema(name):
    from agent import registry

    return next(s for s in registry.tool_schemas() if s["name"] == name)


_YEAR_TOOLS = ("get_financial_item", "get_financial_item_us", "get_financial_item_jp",
               "get_financial_item_tw", "compute_dcf", "evaluate_sangjeung_value")


@pytest.mark.parametrize("tool", _YEAR_TOOLS)
def test_year_description_does_not_hardcode_a_year(tool):
    """`예: 2024` 같은 예시는 LLM 을 그 연도로 앵커링한다 — 실제로 그렇게 틀렸다."""
    import re

    desc = _schema(tool)["input_schema"]["properties"]["year"]["description"]
    assert not re.search(r"(19|20)\d{2}", desc), f"{tool}: year 설명에 연도가 박혀 있다 -> {desc}"


@pytest.mark.parametrize("tool", _YEAR_TOOLS)
def test_year_description_says_omit_for_latest(tool):
    desc = _schema(tool)["input_schema"]["properties"]["year"]["description"]
    assert "생략" in desc, f"{tool}: '생략하면 최신' 을 말하지 않는다 -> {desc}"
    assert "직전연도" not in desc, f"{tool}: 기본값을 '직전연도' 라고 잘못 설명한다"


def test_year_is_never_required():
    for tool in _YEAR_TOOLS:
        assert "year" not in _schema(tool)["input_schema"].get("required", [])


# ── (2) 다년치 도구 ───────────────────────────────────────────────────
def test_history_tool_is_registered_without_a_year_argument():
    s = _schema("get_financial_history")["input_schema"]
    assert s["required"] == ["company", "item"]
    assert "year" not in s["properties"], "연도를 받으면 다시 찍게 된다"
    assert "years" in s["properties"], "가져올 '연수' 만 받는다"


def test_history_tool_description_points_away_from_per_year_calls():
    desc = _schema("get_financial_history")["description"]
    assert "최근" in desc and "get_financial_item" in desc
    assert "리노공업" in desc, "실측 사고를 근거로 남겨 회귀를 막는다"


# ── 시장별 history: 최신연도부터 역순 ─────────────────────────────────
def test_kr_history_anchors_on_latest_year(monkeypatch):
    """dart.financial_item_nyear 는 _latest_year 에서 시작해야 한다."""
    from engines import market_data as md
    from providers import dart

    seen = {}

    def fake_nyear(company, item, n=5, year=None, report="annual", prefer="CFS"):
        seen["year_arg"] = year
        return {"corp_name": "리노공업", "stock_code": "058470", "corp_code": "1",
                "item": item, "rcept": "20260318000182", "filing_date": "2026-03-18",
                "series": [{"year": 2025, "period": "FY2025", "amount": 372_534_084_608},
                           {"year": 2024, "period": "FY2024", "amount": 278_186_189_427},
                           {"year": 2023, "period": "FY2023", "amount": 255_573_034_656}]}

    monkeypatch.setattr(dart, "financial_item_nyear", fake_nyear)
    h = md.history({"market": "KR", "name": "리노공업", "native_id": "058470"}, "revenue", 3)
    assert seen["year_arg"] is None, "연도를 고정하지 않고 provider 가 최신을 찾게 한다"
    assert [r["year"] for r in h["rows"]] == [2025, 2024, 2023]
    assert 2022 not in [r["year"] for r in h["rows"]]


def test_history_rows_are_descending_and_drop_missing(monkeypatch):
    from engines import market_data as md
    from providers import dart

    monkeypatch.setattr(dart, "financial_item_nyear", lambda *a, **k: {
        "corp_name": "X", "stock_code": "1", "corp_code": "1", "item": "revenue",
        "rcept": "1", "filing_date": None,
        "series": [{"year": 2025, "period": "FY2025", "amount": 30},
                   {"year": 2024, "period": "FY2024", "amount": None},
                   {"year": 2023, "period": "FY2023", "amount": 10}]})
    rows = md.history({"market": "KR", "name": "X", "native_id": "1"}, "revenue", 3)["rows"]
    assert [r["year"] for r in rows] == [2025, 2023], "값 없는 연도는 버린다"


def test_us_history_uses_n_not_hardcoded_three(monkeypatch):
    """sec.financial_item_multiyear 가 3개년에 고정돼 있었다."""
    from providers import sec

    rows = [{"end": f"{y}-08-28", "val": y, "accn": "a", "filed": f"{y}-10-01"}
            for y in range(2025, 2018, -1)]
    monkeypatch.setattr(sec, "resolve",
                        lambda c: {"cik": "1", "ticker": "MU", "title": "MICRON"})
    monkeypatch.setattr(sec, "_company_facts", lambda cik: {})
    monkeypatch.setattr(sec, "_find_series", lambda facts, item: (rows, "Revenues"))
    d = sec.financial_item_multiyear("MU", "revenue", None, 5)
    assert [r["year"] for r in d["series"]] == [2025, 2024, 2023, 2022, 2021]


def test_tw_history_uses_n(monkeypatch):
    from providers import finmind

    monkeypatch.setattr(finmind, "resolve",
                        lambda c: {"stock_id": "2330", "stock_name": "台積電"})
    monkeypatch.setattr(finmind, "_latest_complete_year", lambda *a, **k: 2025)
    monkeypatch.setattr(finmind, "_sum_income",
                        lambda sid, y, cands: (float(y), "Revenue", 4))
    d = finmind.financial_item_multiyear("2330", "revenue", None, 4)
    assert [r["year"] for r in d["series"]] == [2025, 2024, 2023, 2022]


def test_jp_history_is_capped_at_report_capacity(monkeypatch):
    """유가증권보고서 한 건에는 5개년(当期~四期前)까지만 담긴다."""
    from providers import edinet

    monkeypatch.setattr(edinet, "resolve", lambda c: {
        "edinet_code": "E02144", "name_en": "TOYOTA", "name_ja": "トヨタ",
        "sec_code": "72030", "fiscal_year_end": "3月31日"})
    monkeypatch.setattr(edinet, "_find_annual_doc",
                        lambda *a, **k: ({"docID": "S1", "submitDateTime": "2026-06-18 00:00"},
                                         2026))
    monkeypatch.setattr(edinet, "_doc_rows", lambda docid: ())
    monkeypatch.setattr(edinet, "_find_value",
                        lambda rows, tags, kind, i=0: ({"val": 100 - i}, "x:RevenueIFRS", "연결"))
    d = edinet.financial_item_multiyear("7203", "revenue", None, 9)
    assert len(d["series"]) == 5, "5개년 상한을 넘겨 만들어내면 안 된다"
    assert [r["year"] for r in d["series"]] == [2026, 2025, 2024, 2023, 2022]


# ── 빈 응답 캐시 ──────────────────────────────────────────────────────
def test_empty_response_is_cached_only_briefly(tmp_path, monkeypatch):
    """DART 는 아직 접수 안 된 연도에 status=013 을 준다 — 길게 캐시하면 최신 판정이 늦어진다."""
    import json

    from core import http as http_mod

    monkeypatch.setattr(http_mod, "CACHE_DIR", tmp_path)
    cp = tmp_path / "x.json"
    monkeypatch.setattr(http_mod, "_cache_path", lambda url, suffix: cp)
    cp.write_text(json.dumps({"status": "013", "message": "조회된 데이타가 없습니다"}),
                  encoding="utf-8")

    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            calls.append(1)
            return {"status": "000", "list": [{"account_nm": "매출액"}]}

    monkeypatch.setattr(http_mod, "session", lambda: type("S", (), {
        "get": lambda self, *a, **k: _Resp()})())

    # 빈 응답이므로 ttl_hours=72 여도 empty_ttl_hours(기본 1h) 로 만료 판정 → 재조회
    import os

    old = time.time() - 2 * 3600
    os.utime(cp, (old, old))
    out = http_mod.get_json("http://x", ttl_hours=72,
                            is_empty=lambda d: d.get("status") != "000")
    assert out["status"] == "000", "빈 응답은 1시간 뒤 다시 가져와야 한다"
    assert calls, "네트워크를 다시 타야 한다"


def test_nonempty_response_keeps_the_long_ttl(tmp_path, monkeypatch):
    import json
    import os

    from core import http as http_mod

    cp = tmp_path / "y.json"
    monkeypatch.setattr(http_mod, "_cache_path", lambda url, suffix: cp)
    cp.write_text(json.dumps({"status": "000", "list": [1]}), encoding="utf-8")
    old = time.time() - 5 * 3600
    os.utime(cp, (old, old))
    monkeypatch.setattr(http_mod, "session", lambda: pytest.fail(
        "정상 응답은 TTL 안에서 재조회하면 안 된다"))
    out = http_mod.get_json("http://y", ttl_hours=72,
                            is_empty=lambda d: d.get("status") != "000")
    assert out["list"] == [1]


def test_dart_statement_fetch_declares_empty_predicate():
    """_fetch_all 이 is_empty 를 넘기지 않으면 013 이 3일간 굳는다."""
    import inspect

    from providers import dart

    src = inspect.getsource(dart._fetch_all)
    assert "is_empty" in src


# ── SEC 태그 변경 이어붙이기 ──────────────────────────────────────────
def test_sec_merges_series_across_a_tag_change(monkeypatch):
    """회사가 도중에 태그를 바꾸면 '첫 태그' 를 고르면 낡은 값이 나온다.

    실측(2026-08-27, NVIDIA): RevenueFromContractWithCustomerExcludingAssessedTax 는
    2022-01-30 에서 끊기고 Revenues 가 2026-01-25 까지 이어진다. 1순위 태그를 그대로
    쓰면 4년 묵은 매출이 '최신' 으로 반환됐다.
    """
    from providers import sec

    old_tag = [{"end": "2022-01-30", "val": 26_914, "accn": "a", "filed": "2022-03-01"},
               {"end": "2021-01-31", "val": 16_675, "accn": "a", "filed": "2021-03-01"}]
    new_tag = [{"end": "2026-01-25", "val": 300_000, "accn": "b", "filed": "2026-02-20"},
               {"end": "2025-01-26", "val": 130_497, "accn": "b", "filed": "2025-02-20"},
               {"end": "2023-01-29", "val": 26_974, "accn": "b", "filed": "2023-02-20"}]

    def fake_annual(facts, taxonomy, tag, kind):
        return {"RevenueFromContractWithCustomerExcludingAssessedTax": old_tag,
                "Revenues": new_tag}.get(tag, [])

    monkeypatch.setattr(sec, "_annual_rows", fake_annual)
    rows, tag = sec._find_series({}, "revenue")
    assert rows[0]["end"] == "2026-01-25", "최신 기간이 앞에 와야 한다"
    assert rows[0]["_tag"] == "Revenues"
    ends = [r["end"] for r in rows]
    assert "2022-01-30" in ends, "태그가 바뀐 이전 구간도 이어붙여야 한다"
    assert "+" in tag, "병합에 쓴 태그를 전부 남긴다"


def test_sec_prefers_priority_tag_on_the_same_period(monkeypatch):
    from providers import sec

    def fake_annual(facts, taxonomy, tag, kind):
        row = {"end": "2025-12-31", "accn": "x", "filed": "2026-02-01"}
        return {"RevenueFromContractWithCustomerExcludingAssessedTax": [{**row, "val": 100}],
                "Revenues": [{**row, "val": 999}]}.get(tag, [])

    monkeypatch.setattr(sec, "_annual_rows", fake_annual)
    rows, _ = sec._find_series({}, "revenue")
    assert rows[0]["val"] == 100, "같은 기간이면 우선순위 태그를 쓴다"


# ── 비상장 KR: 감사보고서 폴백 ────────────────────────────────────────
def test_unlisted_kr_history_falls_back_to_audit_report(monkeypatch):
    """비상장 외감법인은 정기보고서 API 에 데이터가 없다(013) — 시계열이 통째로 실패했다."""
    from engines import market_data as md
    from providers import dart

    monkeypatch.setattr(dart, "financial_item_nyear", lambda *a, **k: {
        "corp_name": "에스케이트리켐", "stock_code": "", "corp_code": "1", "item": "revenue",
        "rcept": None, "filing_date": None,
        "series": [{"year": 2025, "period": "FY2025", "amount": None},
                   {"year": 2024, "period": "FY2024", "amount": None}]})
    monkeypatch.setattr(dart, "financial_item_multiyear", lambda *a, **k: {
        "corp_name": "에스케이트리켐", "stock_code": "", "corp_code": "1",
        "fs_label": "감사보고서(별도, 파싱)", "rcept": "20260320000111",
        "filing_date": "2026-03-20", "item": "revenue",
        "series": [{"period": "FY2025", "amount": 154_798_222_775},
                   {"period": "FY2024", "amount": 154_888_727_400},
                   {"period": "FY2023", "amount": 144_150_761_914}]})

    h = md.history({"market": "KR", "name": "에스케이트리켐", "native_id": "1"}, "revenue", 3)
    assert [r["year"] for r in h["rows"]] == [2025, 2024, 2023]
    assert "감사보고서" in h["basis"]


def test_resolve_financials_allows_unlisted_kr(monkeypatch):
    """resolve() 는 시가총액을 전제해 비상장을 거절한다 — 재무 조회는 거절하면 안 된다."""
    from engines import market_data as md
    from providers import dart

    monkeypatch.setattr(dart, "resolve", lambda c: {
        "corp_code": "00123456", "corp_name": "에스케이트리켐", "stock_code": ""})
    spec = md.resolve_financials("에스케이트리켐", "KR")
    assert spec["listed"] is False and spec["native_id"] == "00123456"
    with pytest.raises(DataError, match="비상장"):
        md.resolve("에스케이트리켐", "KR")

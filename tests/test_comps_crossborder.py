"""크로스보더 comps — 기준 정렬과 '포기하지 않기' 를 고정한다.

여기 있는 테스트는 전부 **실측에서 실제로 틀렸던 것**에 대응한다. 네트워크는 타지 않고
provider 경계를 스텁한다(conftest 가 키를 비우므로 진짜 호출은 어차피 실패한다).
"""
from __future__ import annotations

import pytest

from core.schema import DataError, Provenance, SourceType, Value
from engines import comps, market_data as md
from providers import dart, fx, sec


def _v(value, unit="KRW", label="x", as_of=None, source="테스트"):
    return Value(value, unit, label=label,
                 provenance=Provenance(source=source, source_type=SourceType.AUTHORITATIVE,
                                       source_url="", as_of=as_of))


# ── DART 분기 코드 (조용히 연간을 돌려주던 버그) ────────────────────────────
def test_dart_q1_report_code_is_11013():
    """q1 이 13013 이면 DART 가 오류 대신 **사업보고서(연간)** 를 돌려준다.

    실측 2026-08-27, 삼성전자 2025: 13013 → thstrm_nm='제 57 기'(연간 43.6조),
    11013 → '제 57 기 1분기'(6.69조). report='q1' 조회가 전부 연간값이었다.
    """
    assert dart.REPRT["q1"] == "11013"
    assert "13013" not in dart.REPRT.values()


def test_dart_quarter_order_covers_three_quarters():
    months = [m for _, _, m in dart._QUARTER_ORDER]
    assert months == [9, 6, 3], "최근 분기부터 역순이어야 LTM 이 가장 최신 누적을 잡는다"


# ── 기준기간 판정 ──────────────────────────────────────────────────────────
def test_basis_comes_from_provenance_not_label(monkeypatch):
    """연간 폴백 라벨에 'LTM 아님' 이 들어 있어 label 부분문자열 검사가 통과해 버렸다.

    → 그 결과 EBIT=LTM / D&A=연간 혼용이 표에 'LTM' 으로 표시됐다(실측 확인).
    """
    fallback = _v(100, label="삼성전자 D&A (FY2025 연간 — LTM 아님)", as_of="FY2025")
    monkeypatch.setattr(dart, "ltm_da", lambda *a, **k: fallback)
    spec = {"market": "KR", "name": "삼성전자", "native_id": "005930", "currency": "KRW"}
    _, basis = md.ltm(spec, "da")
    assert basis == "FY"


def test_basis_ltm_is_recognized(monkeypatch):
    ltm = _v(100, label="삼성전자 D&A (LTM, 2026 half 기준)", as_of="LTM~2026 half")
    monkeypatch.setattr(dart, "ltm_da", lambda *a, **k: ltm)
    spec = {"market": "KR", "name": "삼성전자", "native_id": "005930", "currency": "KRW"}
    _, basis = md.ltm(spec, "da")
    assert basis == "LTM"


# ── 시장·회사 지정 파싱 ────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expect", [
    ("MU:US", ("MU", "US")),
    ("US:MU", ("MU", "US")),
    ("2330:TW", ("2330", "TW")),
    ("삼성전자", ("삼성전자", "KR")),
    ("  MU:us  ", ("MU", "US")),
])
def test_parse_spec(raw, expect):
    assert comps._parse_spec(raw, "KR") == expect


def test_parse_spec_rejects_unknown_market():
    with pytest.raises(DataError, match="시장 코드"):
        comps._parse_spec("MU:XX", "KR")


# ── 셀 단위 실패: 표를 죽이지 않는다 ────────────────────────────────────────
def _stub_market(monkeypatch, fail_net_debt_for=()):
    """4개사 최소 모델을 만드는 스텁. fail_net_debt_for 에 든 회사만 순부채 실패."""
    data = {
        "삼성전자": dict(market="KR", currency="KRW", mc=1000.0, nd=-100.0,
                      ebit=100.0, da=50.0, ni=80.0, rev=500.0, eq=400.0),
        "SK하이닉스": dict(market="KR", currency="KRW", mc=800.0, nd=50.0,
                        ebit=90.0, da=30.0, ni=70.0, rev=400.0, eq=200.0),
        "MU": dict(market="US", currency="USD", mc=600.0, nd=20.0,
                   ebit=60.0, da=40.0, ni=-10.0, rev=300.0, eq=150.0),
        "2330": dict(market="TW", currency="TWD", mc=400.0, nd=-30.0,
                     ebit=40.0, da=20.0, ni=30.0, rev=200.0, eq=100.0),
    }

    def resolve(company, market):
        d = data[company]
        return {"name": company, "market": d["market"], "currency": d["currency"],
                "symbol": company, "native_id": company, "extra": {}}

    def market_cap(spec, as_of=None):
        return _v(data[spec["name"]]["mc"], spec["currency"], as_of="20260826")

    def net_debt(spec, include_lease=True):
        if spec["name"] in fail_net_debt_for:
            raise DataError("차입금 태그를 찾지 못함")
        return _v(data[spec["name"]]["nd"], spec["currency"], as_of="20260630")

    def ltm(spec, item):
        key = {"operating_income": "ebit", "da": "da",
               "net_income": "ni", "revenue": "rev"}[item]
        return _v(data[spec["name"]][key], spec["currency"], as_of="LTM~2026-06-30"), "LTM"

    def point(spec, item):
        assert item == "total_equity"
        return _v(data[spec["name"]]["eq"], spec["currency"], as_of="2026-06-30")

    monkeypatch.setattr(md, "resolve", resolve)
    monkeypatch.setattr(md, "market_cap", market_cap)
    monkeypatch.setattr(md, "net_debt", net_debt)
    monkeypatch.setattr(md, "ltm", ltm)
    monkeypatch.setattr(md, "point", point)
    monkeypatch.setattr(md, "shares", lambda spec: _v(10.0, "주"))
    monkeypatch.setattr(md, "common_trading_date",
                        lambda specs: ("20260826", {s["name"]: "20260826" for s in specs}))
    monkeypatch.setattr(comps.fx, "fx_rate",
                        lambda b, q, d=None: _v(1.0, f"{q}/{b}", as_of=d))
    return data


def test_one_missing_cell_does_not_kill_the_table(monkeypatch):
    """TSMC 순부채를 못 구했다고 삼성전자·SK하이닉스 배수까지 버리면 안 된다.

    실측 실패가 정확히 이것이었다 — 모듈 하나가 없어서 4개사 표 전체를 '산출 불가' 로 냈다.
    """
    _stub_market(monkeypatch, fail_net_debt_for={"2330"})
    m = comps.build_model(["삼성전자", "SK하이닉스", "MU:US", "2330:TW"])

    assert len(m["rows"]) == 4, "실패한 회사도 행에서 빠지지 않는다"
    tsmc = next(r for r in m["rows"] if r["name"] == "2330")
    assert "net_debt" in tsmc["missing"]
    assert tsmc["multiples"]["ev_ebitda"] is None, "EV 를 못 만들었으니 EV 배수는 없다"
    assert tsmc["multiples"]["pbr"] is not None, "EV 와 무관한 자기자본배수는 살아있어야 한다"

    samsung = next(r for r in m["rows"] if r["name"] == "삼성전자")
    assert samsung["multiples"]["ev_ebitda"] == pytest.approx(900 / 150)
    assert m["stats"]["ev_ebitda"]["n"] == 3, "산출 가능한 것만으로 median 을 낸다"
    assert m["stats"]["pbr"]["n"] == 4


def test_negative_denominator_is_nm_and_excluded_from_median(monkeypatch):
    """적자 순이익의 P/E 는 음수 배수를 표에 올리면 안 된다(median 오염)."""
    _stub_market(monkeypatch)
    m = comps.build_model(["삼성전자", "SK하이닉스", "MU:US", "2330:TW"])
    mu = next(r for r in m["rows"] if r["name"] == "MU")
    assert mu["multiples"]["per"] is None
    assert "per" in mu["nm"] and "NM" in mu["nm"]["per"]
    assert m["stats"]["per"]["n"] == 3
    assert all(v > 0 for v in (m["stats"]["per"]["min"], m["stats"]["per"]["max"]))


def test_ebitda_is_ebit_plus_da_and_ev_is_mc_plus_net_debt(monkeypatch):
    _stub_market(monkeypatch)
    m = comps.build_model(["삼성전자"])
    r = m["rows"][0]
    assert r["derived_ebitda"] == 150.0
    assert r["derived_ev"] == 900.0
    assert r["multiples"]["ev_ebit"] == pytest.approx(9.0)


def test_target_is_optional(monkeypatch):
    """'이 4개사 표 만들어줘' 는 타깃 평가가 아니다."""
    _stub_market(monkeypatch)
    m = comps.build_model(["삼성전자", "SK하이닉스"])
    assert m["target"] is None
    v = comps.evaluate(["삼성전자", "SK하이닉스"])
    assert v.unit == "개 비교기업" and v.value == 2


def test_target_application_uses_median_and_bridges_ev_to_equity(monkeypatch):
    _stub_market(monkeypatch)
    m = comps.build_model(["삼성전자", "SK하이닉스", "2330:TW"], target="MU:US")
    imp = m["target"]["implied"]["ev_ebitda"]
    med = m["stats"]["ev_ebitda"]["median"]
    assert imp["multiple"] == med
    assert imp["ev"] == pytest.approx(med * 100)          # MU EBITDA = 60+40
    assert imp["equity_value"] == pytest.approx(imp["ev"] - 20)   # − 순부채
    assert imp["per_share"] == pytest.approx(imp["equity_value"] / 10)


def test_multiples_are_not_currency_converted(monkeypatch):
    """배수는 통화중립 — 환산은 절대금액에만 적용한다."""
    _stub_market(monkeypatch)
    monkeypatch.setattr(comps.fx, "fx_rate", lambda b, q, d=None: _v(2.0, f"{q}/{b}", as_of=d))
    m = comps.build_model(["삼성전자", "MU:US"], display_currency="USD")
    kr = next(r for r in m["rows"] if r["name"] == "삼성전자")
    assert kr["multiples"]["ev_ebitda"] == pytest.approx(6.0), "배수에 환율이 곱해지면 안 된다"
    assert kr["display"]["ev"] == pytest.approx(1800.0), "절대금액은 환산된다"


def test_warnings_surface_basis_mixing(monkeypatch):
    data = _stub_market(monkeypatch)

    def ltm(spec, item):
        key = {"operating_income": "ebit", "da": "da",
               "net_income": "ni", "revenue": "rev"}[item]
        basis = "FY" if (item == "da" and spec["market"] == "KR") else "LTM"
        as_of = "FY2025" if basis == "FY" else "LTM~2026-06-30"
        return _v(data[spec["name"]][key], spec["currency"], as_of=as_of), basis

    monkeypatch.setattr(md, "ltm", ltm)
    m = comps.build_model(["삼성전자", "MU:US"])
    kr = next(r for r in m["rows"] if r["name"] == "삼성전자")
    assert "혼용" in kr["basis"]["ebitda"]
    assert any("기준기간 혼용" in w for w in m["warnings"])
    assert any("회계기준 차이" in w for w in m["warnings"])


def test_fx_failure_does_not_break_the_table(monkeypatch):
    _stub_market(monkeypatch)

    def boom(base, quote, date=None):
        raise DataError("환율을 찾지 못함")

    monkeypatch.setattr(comps.fx, "fx_rate", boom)
    m = comps.build_model(["삼성전자", "MU:US"], display_currency="USD")
    assert m["fx_errors"], "환산 실패는 기록된다"
    assert any("환산 실패" in w for w in m["warnings"])
    kr = next(r for r in m["rows"] if r["name"] == "삼성전자")
    assert kr["multiples"]["ev_ebitda"] is not None, "환산 실패가 배수를 죽이면 안 된다"


def test_common_trading_date_picks_the_earliest_latest(monkeypatch):
    monkeypatch.setattr(md, "_latest_trading_date",
                        lambda spec: {"A": "20260827", "B": "20260826"}[spec["name"]])
    common, latest = md.common_trading_date([{"name": "A"}, {"name": "B"}])
    assert common == "20260826"
    assert latest == {"A": "20260827", "B": "20260826"}


# ── 한국 시가총액: DART 발행주식총수를 쓰면 안 된다 ─────────────────────────
def test_kr_shares_uses_krx_implied_not_dart_total(monkeypatch):
    """실측: DART 발행주식총수는 삼성전자 +53.8%, SK하이닉스 +683% 과대였다
    (우선주·누적발행분 포함). 시가총액 계산에 쓰면 배수가 조용히 틀린다."""
    called = {}
    monkeypatch.setattr(md.naver, "implied_common_shares",
                        lambda code, name=None: called.setdefault("naver", True) or _v(1.0, "주"))
    monkeypatch.setattr(md.dart, "shares_outstanding",
                        lambda *a, **k: pytest.fail("DART 발행주식총수를 시총에 쓰면 안 된다"))
    md.shares({"market": "KR", "native_id": "005930", "name": "삼성전자"})
    assert called.get("naver")


# ── DART 주식수: 누적 발행 총수를 쓰면 안 된다 ──────────────────────────────
_STOCK_ROWS = {   # 실측 2026-08-27 SK하이닉스 FY2025 (stockTotqySttus)
    "list": [
        {"se": "보통주", "isu_stock_totqy": "9,000,000,000",
         "now_to_isu_stock_totqy": "5,721,980,209",
         "distb_stock_co": "701,691,520", "tesstk_co": "26,310,845", "rcept_no": "20260316000001"},
        {"se": "우선주", "isu_stock_totqy": "-", "now_to_isu_stock_totqy": "-",
         "distb_stock_co": "-", "tesstk_co": "-"},
        {"se": "합계", "isu_stock_totqy": "9,000,000,000",
         "now_to_isu_stock_totqy": "5,721,980,209",
         "distb_stock_co": "701,691,520", "tesstk_co": "26,310,845"},
    ],
    "status": "000",
}


@pytest.fixture
def stock_totqy(monkeypatch):
    monkeypatch.setattr(dart.config, "require", lambda *a, **k: "key")
    monkeypatch.setattr(dart, "resolve",
                        lambda c: {"corp_name": "SK하이닉스", "corp_code": "00164779",
                                   "stock_code": "000660"})
    monkeypatch.setattr(dart, "_latest_year", lambda *a, **k: 2025)
    monkeypatch.setattr(dart, "get_json", lambda *a, **k: _STOCK_ROWS)


def test_shares_outstanding_ignores_cumulative_issued_field(stock_totqy):
    """now_to_isu_stock_totqy 는 '현재까지 발행한' 누적값(소각분 포함)이다.

    실측: SK하이닉스가 그 필드로 5,721,980,209주 → 실제 발행 728,002,365주의 7.86배.
    이 값이 DCF·상증법의 분모라서 주당가치가 8분의 1로 나왔다.
    """
    v = dart.shares_outstanding("SK하이닉스")
    assert v.value == 701_691_520 + 26_310_845 == 728_002_365
    assert v.value != 5_721_980_209, "누적 발행 총수를 발행주식총수로 쓰면 안 된다"
    assert "distb_stock_co" in v.provenance.original_field


def test_shares_outstanding_basis_outstanding_excludes_treasury(stock_totqy):
    v = dart.shares_outstanding("SK하이닉스", basis="outstanding")
    assert v.value == 701_691_520
    assert "유통주식수" in v.label


def test_shares_outstanding_exposes_treasury_in_extras(stock_totqy):
    v = dart.shares_outstanding("SK하이닉스")
    assert v.extras["treasury"].value == 26_310_845
    assert v.extras["outstanding"].value == 701_691_520


# ── SEC LTM: 4분기 합산이 아니라 YTD ───────────────────────────────────────
def _sec_facts(rows):
    return {"us-gaap": {"OperatingIncomeLoss": {"units": {"USD": rows}}}}


def test_sec_ltm_uses_ytd_because_q4_is_never_tagged(monkeypatch):
    """미국 발행인은 Q4 단독 기간을 10-Q 에 태깅하지 않는다.

    실측(Micron): 90일 관측치가 Q1·Q2·Q3 만 있어 '최근 4개 분기 합' 은 전년 Q3 를 끌어와
    57.8bn 을 냈고, 올바른 값(연간 + YTD − 전년YTD)은 59.2bn 이었다.
    """
    rows = [
        # 직전 회계연도 연간
        dict(start="2024-08-30", end="2025-08-28", val=9770, form="10-K", fp="FY", filed="2025-10-01", accn="a"),
        # 당해 YTD(3분기 누적) + 전년 동기 YTD
        dict(start="2025-08-29", end="2026-05-28", val=55589, form="10-Q", fp="Q3", filed="2026-06-25", accn="b"),
        dict(start="2024-08-30", end="2025-05-29", val=6116, form="10-Q", fp="Q3", filed="2025-06-25", accn="c"),
        # 분기 단독들 — Q4 는 없다(실제 EDGAR 와 동일한 구멍)
        dict(start="2026-02-27", end="2026-05-28", val=33318, form="10-Q", fp="Q3", filed="2026-06-25", accn="b"),
        dict(start="2025-11-28", end="2026-02-26", val=16135, form="10-Q", fp="Q2", filed="2026-03-25", accn="d"),
        dict(start="2025-08-29", end="2025-11-27", val=6136, form="10-Q", fp="Q1", filed="2025-12-20", accn="e"),
        dict(start="2025-02-28", end="2025-05-29", val=2169, form="10-Q", fp="Q3", filed="2025-06-25", accn="c"),
    ]
    monkeypatch.setattr(sec, "resolve",
                        lambda c: {"cik": "0000723125", "ticker": "MU", "title": "MICRON"})
    monkeypatch.setattr(sec, "_company_facts", lambda cik: _sec_facts(rows))
    v = sec.ltm_item("MU", "operating_income")
    assert v.value == 9770 + 55589 - 6116
    assert v.value != 33318 + 16135 + 6136 + 2169, "4분기 합산은 12개월이 아니다"
    assert v.provenance.as_of.startswith("LTM~")


def test_sec_ltm_falls_back_to_annual_and_says_so(monkeypatch):
    """새 회계연도 분기보고서가 없으면 연간을 쓰되 'LTM 아님' 을 as_of 로 구분한다."""
    rows = [dict(start="2024-08-30", end="2025-08-28", val=9770, form="10-K", fp="FY",
                 filed="2025-10-01", accn="a")]
    monkeypatch.setattr(sec, "resolve",
                        lambda c: {"cik": "0000723125", "ticker": "MU", "title": "MICRON"})
    monkeypatch.setattr(sec, "_company_facts", lambda cik: _sec_facts(rows))
    v = sec.ltm_item("MU", "operating_income")
    assert v.value == 9770
    assert v.provenance.as_of == "FY2025" and not v.provenance.as_of.startswith("LTM")
    assert "LTM 아님" in v.label


# ── 환율: ECB 미고시 통화 ──────────────────────────────────────────────────
def test_twd_fx_goes_to_yahoo_without_calling_ecb(monkeypatch):
    """frankfurter(ECB)는 TWD 를 고시하지 않는다(실측: USD->TWD 404)."""
    monkeypatch.setattr(fx, "get_json",
                        lambda *a, **k: pytest.fail("ECB 를 먼저 부르면 404 로 낭비한다"))
    from providers import yahoo

    monkeypatch.setattr(yahoo, "fx_rate", lambda b, q: _v(31.8, f"{q}/{b}", source="Yahoo"))
    v = fx.fx_rate("USD", "TWD")
    assert v.value == 31.8


def test_ecb_still_used_for_krw(monkeypatch):
    seen = {}

    def get_json(url, ttl_hours=12, params=None, **k):
        seen["params"] = params
        return {"rates": {"KRW": 1383.49}, "date": "2026-08-26"}

    monkeypatch.setattr(fx, "get_json", get_json)
    v = fx.fx_rate("USD", "KRW")
    assert v.value == 1383.49
    assert v.provenance.source_type == SourceType.AUTHORITATIVE
    assert seen["params"] == {"from": "USD", "to": "KRW"}


# ── 도구 스키마가 실패 경로를 안내하는지 ────────────────────────────────────
def _schema(name):
    from agent import registry

    return next(s for s in registry.tool_schemas() if s["name"] == name)


def test_figi_schema_documents_taiwan_exchange_code():
    """실측: 대만을 TW 로 찍어 실패한 뒤 '종목 식별 실패' 로 결론냈다. 정답은 TT."""
    desc = _schema("get_figi")["description"]
    assert "TT" in desc and "TW 가 아니라" in desc
    assert "밸류에이션 불가 사유가 아니다" in desc


def test_comps_schema_requires_only_companies():
    s = _schema("compute_comps")["input_schema"]
    assert s["required"] == ["companies"]
    assert "target" in s["properties"]


def test_market_cap_and_ebitda_and_net_debt_are_registered():
    for name in ("get_market_cap", "get_ebitda"):
        assert _schema(name)["input_schema"]["required"] == ["company"]
    assert "market" in _schema("get_net_debt")["input_schema"]["properties"]


def test_da_is_reachable_as_an_item():
    """D&A 는 EBITDA 의 분모인데 어떤 도구로도 노출돼 있지 않았다."""
    for tool in ("get_financial_item", "get_financial_item_us", "get_financial_item_tw"):
        assert "da" in _schema(tool)["input_schema"]["properties"]["item"]["enum"], tool


def test_jp_schema_does_not_advertise_unsupported_items():
    """스키마가 지원하지 않는 항목을 광고하면 LLM 이 '데이터 없음' 으로 오해한다."""
    from providers import edinet

    enum = _schema("get_financial_item_jp")["input_schema"]["properties"]["item"]["enum"]
    assert set(enum) <= set(edinet.ITEM_MAP), "EDINET 이 매핑하지 않은 항목이 스키마에 있다"


def test_us_and_tw_schemas_match_their_item_maps():
    from providers import finmind

    for tool, mod in (("get_financial_item_us", sec), ("get_financial_item_tw", finmind)):
        enum = _schema(tool)["input_schema"]["properties"]["item"]["enum"]
        missing = set(enum) - set(mod.ITEM_MAP)
        assert not missing, f"{tool} 이 구현에 없는 항목을 광고한다: {missing}"


def test_kr_schema_matches_dart_item_map():
    enum = _schema("get_financial_item")["input_schema"]["properties"]["item"]["enum"]
    # da 는 ITEM_MAP 이 아니라 da_best() 경로로 처리된다 — 그 외는 전부 매핑돼 있어야 한다.
    missing = set(enum) - set(dart.ITEM_MAP) - {"da"}
    assert not missing, f"get_financial_item 이 구현에 없는 항목을 광고한다: {missing}"

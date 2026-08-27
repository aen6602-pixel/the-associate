"""시장 중립 데이터 계층 — "어느 나라 회사냐"를 여기서만 분기한다.

크로스보더 comps 가 못 만들어졌던 이유는 데이터가 없어서가 아니라 **provider 마다 호출
규약이 달라 조립하는 층이 없었기** 때문이다(실측: 시가총액은 국내만, 순부채는 DART 전용,
D&A 는 아무 도구로도 노출 안 됨). 이 모듈은 시장별 provider 를 하나의 인터페이스로 덮어
comps 엔진이 "회사 + 시장" 만 알면 되게 한다.

시장별 원천:
  KR  DART(재무) + 네이버(KRX 시세)
  US  SEC EDGAR XBRL(재무) + Yahoo(시세)
  JP  EDINET(재무, 연간만) + Yahoo(시세)
  TW  FinMind(재무) + Yahoo(시세)

기준일 정렬(comps 의 생명):
  - 분자(시가총액): `common_trading_date()` 로 4개 시장의 **공통 거래일**을 먼저 정하고
    각 종목에서 그 날짜 이하의 종가를 쓴다. 시장별 휴장일·시차 때문에 "최신 종가" 는
    같은 날이 아니다(실측 2026-08-27: KRX 08-27, 나스닥·TWSE 08-26).
  - 분모(손익): 각 시장의 LTM. LTM 을 만들 수 없으면 연간값을 쓰되 `basis` 에 그 사실을
    실어 보내 표에 드러나게 한다 — 조용히 연간을 LTM 이라고 부르지 않는다.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from providers import dart, sec, edinet, finmind, naver, yahoo

MARKETS = ("KR", "US", "JP", "TW")
CURRENCY = {"KR": "KRW", "US": "USD", "JP": "JPY", "TW": "TWD"}

# LTM 을 만들 수 있는 시장. JP(EDINET)는 유가증권보고서(연간)만 파싱하므로 연간 기준이다.
_LTM_MARKETS = ("KR", "US", "TW")


def normalize_market(market: str | None, default: str = "KR") -> str:
    m = (market or default).strip().upper()
    if m not in MARKETS:
        raise DataError(f"지원하지 않는 시장: {market} (지원: {', '.join(MARKETS)})")
    return m


def resolve(company: str, market: str) -> dict:
    """회사 식별 → {name, market, currency, symbol, native_id}.

    symbol 은 Yahoo 티커다. 사용자가 안 줘도 각 시장 provider 의 종목코드에서 유도한다 —
    예전에는 LLM 이 `symbol` 을 직접 넣어야 했고, 안 넣으면 KOSPI 와 회귀되거나 시세를
    못 구했다.
    """
    m = normalize_market(market)
    if m == "KR":
        ent = dart.resolve(company)
        if not ent.get("stock_code"):
            raise DataError(f"{ent['corp_name']}: 비상장(KRX 시세 없음) → 시가총액을 구할 수 없습니다")
        return {"name": ent["corp_name"], "market": m, "currency": "KRW",
                "symbol": f"{ent['stock_code']}.KS", "native_id": ent["stock_code"],
                "extra": {"corp_code": ent["corp_code"]}}
    if m == "US":
        ent = sec.resolve(company)
        return {"name": ent["title"], "market": m, "currency": "USD",
                "symbol": ent["ticker"], "native_id": ent["ticker"],
                "extra": {"cik": ent["cik"]}}
    if m == "TW":
        ent = finmind.resolve(company)
        return {"name": ent["stock_name"], "market": m, "currency": "TWD",
                "symbol": f"{ent['stock_id']}.TW", "native_id": ent["stock_id"], "extra": {}}
    ent = edinet.resolve(company)
    code = (ent.get("sec_code") or "").strip()
    if not code:
        raise DataError(f"{ent.get('name_en') or ent.get('name_ja')}: 증권코드가 없어 "
                        f"Yahoo 시세를 조회할 수 없습니다(비상장 가능성)")
    return {"name": ent.get("name_en") or ent.get("name_ja"), "market": m, "currency": "JPY",
            "symbol": f"{code[:4]}.T", "native_id": code, "extra": {}}


def resolve_financials(company: str, market: str) -> dict:
    """재무 조회용 식별 — **상장 여부를 요구하지 않는다.**

    resolve() 는 시가총액을 전제로 하므로 KR 비상장사를 거절한다. 그런데 재무 시계열·
    상증법 평가는 비상장사가 오히려 주 대상이다(감사보고서 파싱 경로가 있다). 그래서
    시세가 필요 없는 호출부는 이쪽을 쓴다.
    """
    m = normalize_market(market)
    if m != "KR":
        return resolve(company, m)
    ent = dart.resolve(company)
    return {"name": ent["corp_name"], "market": m, "currency": "KRW",
            "symbol": (f"{ent['stock_code']}.KS" if ent.get("stock_code") else None),
            "native_id": ent.get("stock_code") or ent["corp_code"],
            "listed": bool(ent.get("stock_code")),
            "extra": {"corp_code": ent["corp_code"]}}


# ── 시세·기준일 ────────────────────────────────────────────────────
def _latest_trading_date(spec: dict) -> str:
    """그 종목의 가장 최근 거래일(YYYYMMDD)."""
    if spec["market"] == "KR":
        return naver.close_on_or_before(spec["native_id"], None,
                                        spec["name"]).provenance.as_of
    return yahoo.close_on_or_before(spec["symbol"]).provenance.as_of


def common_trading_date(specs: list[dict]) -> tuple[str, dict]:
    """여러 종목의 **공통 거래일** = 각자 최신 거래일 중 가장 이른 날.

    → (공통기준일, {회사명: 그 종목의 최신거래일}). 두 번째 값은 "왜 이 날짜인가" 를
    표에 적기 위한 것이다.
    """
    latest = {}
    for s in specs:
        latest[s["name"]] = _latest_trading_date(s)
    if not latest:
        raise DataError("기준일을 정할 종목이 없습니다")
    return min(latest.values()), latest


def close(spec: dict, as_of: str | None = None) -> Value:
    if spec["market"] == "KR":
        return naver.close_on_or_before(spec["native_id"], as_of, spec["name"])
    return yahoo.close_on_or_before(spec["symbol"], as_of)


def shares(spec: dict) -> Value:
    """유통 보통주식수.

    KR 은 DART '발행주식총수' 를 쓰지 않는다 — 그것은 우선주·누적발행분을 포함해
    시가총액과 맞지 않는다(실측: 삼성전자 +53.8%, SK하이닉스 +683% 과대). 대신 KRX
    시가총액 ÷ 종가로 역산한다(providers.naver.implied_common_shares).
    """
    m = spec["market"]
    if m == "KR":
        return naver.implied_common_shares(spec["native_id"], spec["name"])
    if m == "US":
        return sec.shares_outstanding(spec["native_id"])
    if m == "TW":
        return finmind.shares_outstanding(spec["native_id"])
    return edinet.shares_outstanding(spec["native_id"])


def market_cap(spec: dict, as_of: str | None = None) -> Value:
    """시가총액 = 유통 보통주식수 × 기준일 종가. 현지통화.

    전 시장 동일 산식이라 4개사를 같은 방식으로 비교할 수 있다. KR 은 주식수 자체가
    KRX 시가총액에서 역산된 값이므로, as_of 가 최신 거래일이면 결과가 KRX 공시 시가총액과
    일치한다(자기일관).
    """
    px = close(spec, as_of)
    sh = shares(spec)
    mc = px.value * sh.value
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    return Value(
        mc, spec["currency"], label=f"{spec['name']} 시가총액 ({px.provenance.as_of})",
        provenance=Provenance(
            source=f"계산({px.provenance.source} 종가 × {sh.provenance.source} 주식수)",
            source_type=SourceType.COMPUTED, source_url=px.provenance.source_url,
            original_field="close × shares_outstanding", as_of=px.provenance.as_of,
            note=(f"종가 {px.value:,} {spec['currency']} ({px.provenance.as_of}) × "
                  f"주식수 {f(sh.value)} = {f(mc)} {spec['currency']}"),
        ),
        extras={"price": px, "shares": sh},
    )


# ── 재무 ───────────────────────────────────────────────────────────
def ltm(spec: dict, item: str) -> tuple[Value, str]:
    """LTM 손익 항목 → (Value, basis). basis 는 "LTM" 또는 "FY"(연간 폴백).

    basis 를 값과 함께 돌려주는 게 핵심이다 — comps 표는 기준기간이 섞였는지를
    셀 단위로 표시해야 하고, 그 판단을 호출부가 note 문자열을 파싱해서 하게 만들면 안 된다.
    """
    m = spec["market"]
    if m == "KR":
        v = dart.ltm_da(spec["name"]) if item == "da" else dart.ltm_item(spec["name"], item)
    elif m == "US":
        v = sec.ltm_item(spec["native_id"], item)
    elif m == "TW":
        v = finmind.ltm_item(spec["native_id"], item)
    else:
        v = edinet.financial_item(spec["native_id"], item)
    # basis 는 label 문자열을 훑어서 정하면 안 된다 — 연간 폴백 라벨이 "…(FY2025 연간 —
    # LTM 아님)" 이라서 'LTM' 부분문자열 검사가 그대로 통과해 버린다(실제로 통과했다).
    # provider 들이 일관되게 채우는 provenance.as_of 규약("LTM~…" vs "FY…")으로 판정한다.
    basis = "LTM" if (v.provenance.as_of or "").startswith("LTM") else "FY"
    return v, basis


def history(spec: dict, item: str, n: int = 3) -> dict:
    """최근 n개 회계연도 시계열 → {rows: [{year, period, amount}], source, ...}.

    **연도를 호출부가 지정하지 않는다.** 각 시장 provider 가 "실제로 데이터가 있는 최신
    회계연도" 를 스스로 찾고 거기서 내려온다. 예전에는 이 도구가 없어서 LLM 이
    get_financial_item 을 연도별로 여러 번 부를 수밖에 없었고, 그때 연도를 직접 찍다가
    낡은 연도를 넣는 사고가 났다(리노공업 FY2022~2024).
    """
    m = spec["market"]
    n = max(1, min(int(n), 10))
    if m == "KR":
        d = dart.financial_item_nyear(spec["name"], item, n)
        rows = [r for r in d["series"] if r.get("amount") is not None]
        basis = "정기보고서(연결/별도 자동)"
        if not rows:
            # 비상장 외감법인은 정기보고서 API 에 데이터가 없다(013). financial_item_nyear 에는
            # 감사보고서 폴백이 없고 financial_item_multiyear 에만 있어서, 비상장사 시계열이
            # 통째로 실패했다(실측: 에스케이트리켐).
            d = dart.financial_item_multiyear(spec["name"], item)
            rows = []
            for r in d["series"][:n]:
                period = r.get("period") or ""
                yr = int(period[2:6]) if period.startswith("FY") and period[2:6].isdigit() else None
                rows.append({"year": yr, "period": period, "amount": r["amount"]})
            basis = d.get("fs_label") or "감사보고서(별도, 파싱)"
        return {"rows": rows, "source": "DART (금융감독원)", "basis": basis,
                "filing_date": d.get("filing_date"),
                "source_url": (f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['rcept']}"
                               if d.get("rcept") else "https://dart.fss.or.kr"),
                "currency": "KRW"}
    if m == "US":
        d = sec.financial_item_multiyear(spec["native_id"], item, None, n)
        return {"rows": d["series"], "source": "SEC EDGAR (XBRL)",
                "basis": f"10-K / us-gaap:{d.get('tag')}",
                "filing_date": d.get("filing_date"),
                "source_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                              f"&CIK={d['cik']}&type=10-K", "currency": "USD"}
    if m == "TW":
        d = finmind.financial_item_multiyear(spec["native_id"], item, None, n)
        return {"rows": d["series"], "source": "FinMind (대만 시장데이터, 2차)",
                "basis": "분기 4개 합산", "filing_date": None,
                "source_url": finmind.mops_url(spec["native_id"]), "currency": "TWD"}
    d = edinet.financial_item_multiyear(spec["native_id"], item, None, n)
    return {"rows": d["series"], "source": "EDINET (일본 금융청)",
            "basis": f"유가증권보고서 · {d.get('basis')}",
            "filing_date": d.get("filing_date"),
            "source_url": f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{d['docid']}",
            "currency": "JPY"}


def point(spec: dict, item: str) -> Value:
    """시점(재무상태표) 항목 — 자본총계·현금 등."""
    m = spec["market"]
    if m == "KR":
        return dart.financial_item(spec["name"], item)
    if m == "US":
        return sec.financial_item(spec["native_id"], item)
    if m == "TW":
        return finmind.financial_item(spec["native_id"], item)
    return edinet.financial_item(spec["native_id"], item)


def net_debt(spec: dict, include_lease: bool = True) -> Value:
    """순부채 = 이자발생부채 − 현금및현금성자산. 현지통화.

    시장별 정의 차이는 없앨 수 없으므로(대만 공시에는 리스부채 계정이 아예 없다)
    각 provider 의 note 에 정의를 남기고, comps 표가 그것을 나란히 보여준다.
    """
    m = spec["market"]
    if m == "KR":
        from engines import dcf_inputs

        return dcf_inputs.net_debt(spec["name"], None, include_lease)
    if m == "US":
        return sec.net_debt(spec["native_id"], include_lease)
    if m == "TW":
        return finmind.net_debt(spec["native_id"])
    raise DataError(f"{spec['name']}: 일본(EDINET)은 차입금 계정 자동추출이 없어 순부채를 "
                    f"계산할 수 없습니다 — EV 배수 대신 자기자본배수(P/E·P/B)를 쓰거나 "
                    f"순부채를 직접 지정해야 합니다.")


def supports_ltm(market: str) -> bool:
    return normalize_market(market) in _LTM_MARKETS

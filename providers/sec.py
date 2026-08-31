"""SEC EDGAR provider (美 증권거래위원회) — 기업 매핑 + XBRL 재무제표. authoritative.

- company_tickers.json: 티커/회사명 ↔ CIK 매핑 (키 불필요, User-Agent 로 연락처만 요구)
- XBRL companyfacts API: us-gaap(재무) + dei(발행주식수) taxonomy 전체 concept

실측 확인(2026-08): 매출 태그는 회사·시기별로 다름(ASC606 전환 등) →
RevenueFromContractWithCustomerExcludingAssessedTax(단수 Contract) 우선, 없으면 Revenues/SalesRevenueNet 순.
재무상태표 항목(Assets 등)은 'instant'(시점) 값이라 duration(매출 등)과 필터 기준이 다르다.
"""
from __future__ import annotations

import re
import difflib
from datetime import date
from core.cache import TTL_FRESH, TTL_INDEX, ttl_cache

from core.schema import Provenance, Value, DataError, SourceType
from core.http import session, get_json
from core import config

_BASE_WWW = "https://www.sec.gov"
_BASE_DATA = "https://data.sec.gov"

# item 키 → (kind: duration(기간)|instant(시점), [XBRL us-gaap 태그 후보, 우선순위순])
ITEM_MAP: dict[str, tuple] = {
    "revenue": ("duration", ["RevenueFromContractWithCustomerExcludingAssessedTax",
                             "Revenues", "SalesRevenueNet"]),
    "operating_income": ("duration", ["OperatingIncomeLoss"]),
    "net_income": ("duration", ["NetIncomeLoss", "ProfitLoss"]),
    "total_assets": ("instant", ["Assets"]),
    "total_liabilities": ("instant", ["Liabilities"]),
    "total_equity": ("instant", ["StockholdersEquity",
                                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    # 아래 항목들은 get_financial_item_us 의 스키마가 예전부터 광고하고 있었으나 실제
    # 매핑이 없어 전부 "지원하지 않는 항목" 으로 떨어졌다(도구 설명 ↔ 구현 불일치).
    "cash": ("instant", ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "ppe": ("instant", ["PropertyPlantAndEquipmentNet"]),
    "inventories": ("instant", ["InventoryNet"]),
    "trade_receivables": ("instant", ["AccountsReceivableNetCurrent",
                                      "ReceivablesNetCurrent"]),
    "trade_payables": ("instant", ["AccountsPayableCurrent",
                                   "AccountsPayableTradeCurrent"]),
    # D&A: EBITDA 의 분모. 회사별 표기가 갈려(실측 2026-08, Micron 은
    # DepreciationDepletionAndAmortization 만 존재하고 DepreciationAmortizationAndAccretionNet
    # 은 404) 후보를 순서대로 시도한다.
    "da": ("duration", ["DepreciationDepletionAndAmortization",
                        "DepreciationAmortizationAndAccretionNet",
                        "DepreciationAndAmortization",
                        "DepreciationDepletionAndAmortizationExcludingAmortizationOfDeferredCharges"]),
}
ITEM_LABEL = {
    "revenue": "Revenue", "operating_income": "Operating Income", "net_income": "Net Income",
    "total_assets": "Total Assets", "total_liabilities": "Total Liabilities",
    "total_equity": "Total Stockholders' Equity", "cash": "Cash and Cash Equivalents",
    "ppe": "Property, Plant and Equipment (net)", "inventories": "Inventories",
    "trade_receivables": "Accounts Receivable", "trade_payables": "Accounts Payable",
    "da": "Depreciation & Amortization",
}

# 순부채용 차입금 태그. LongTermDebt 는 유동·비유동 합계로 공시되는 게 보통이고,
# LongTermDebtNoncurrent/Current 는 회사·시기에 따라 중단된다(실측 2026-08: Micron 의
# LongTermDebtNoncurrent 마지막 10-K 관측치가 FY2012 — 이 태그만 믿으면 13년 묵은 값을
# 순부채로 쓰게 된다). 그래서 합계 태그를 먼저 보고, 없을 때만 분해 태그를 더한다.
_DEBT_TOTAL_TAGS = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]
_DEBT_PART_TAGS = ["LongTermDebtNoncurrent", "LongTermDebtCurrent",
                   "ShortTermBorrowings", "OtherShortTermBorrowings"]
_LEASE_TAGS = ["OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiabilityCurrent",
               "FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityCurrent"]


def _headers() -> dict:
    return {"User-Agent": config.require(config.SEC_USER_AGENT, "SEC_USER_AGENT")}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# ── 기업 매핑 ────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = re.sub(r"\b(inc|corp|corporation|co|ltd|llc|company|the|plc)\b\.?", "", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


@ttl_cache(TTL_INDEX, maxsize=1)
def _ticker_index() -> tuple[dict, dict]:
    """company_tickers.json → (ticker→entry, norm_name→[entry])."""
    j = get_json(f"{_BASE_WWW}/files/company_tickers.json", ttl_hours=24 * 7, headers=_headers())
    by_ticker: dict[str, dict] = {}
    by_norm: dict[str, list] = {}
    for it in j.values():
        entry = {"cik": str(it["cik_str"]).zfill(10), "ticker": it["ticker"], "title": it["title"]}
        by_ticker[it["ticker"].upper()] = entry
        by_norm.setdefault(_norm(it["title"]), []).append(entry)
    return by_ticker, by_norm


def resolve(company: str) -> dict:
    """티커(예: AAPL) 또는 회사명 → {cik, ticker, title}."""
    q = company.strip()
    by_ticker, by_norm = _ticker_index()
    if q.upper() in by_ticker:
        return by_ticker[q.upper()]
    qn = _norm(q)
    if qn in by_norm:
        return by_norm[qn][0]
    cands = [e for norm, lst in by_norm.items() if qn and qn in norm for e in lst]
    if cands:
        return sorted(cands, key=lambda e: len(e["title"]))[0]
    close = difflib.get_close_matches(qn, list(by_norm.keys()), n=1, cutoff=0.8)
    if close:
        return by_norm[close[0]][0]
    raise DataError(f"SEC EDGAR 에서 기업을 못 찾음: '{company}' (티커로 시도해보세요, 예: AAPL)")


# ── XBRL 재무제표 ─────────────────────────────────────────────────
# 10-K/10-Q 가 접수되면 companyfacts 가 갱신된다 → 프로세스 수명 캐시를 쓰면 새 분기가
# 영원히 안 보인다(LTM 계산이 직접 영향받는다).
@ttl_cache(TTL_FRESH, maxsize=128)
def _company_facts(cik: str) -> dict:
    j = get_json(f"{_BASE_DATA}/api/xbrl/companyfacts/CIK{cik}.json",
                ttl_hours=24, headers=_headers())
    return j.get("facts", {})


def _annual_rows(facts: dict, taxonomy: str, tag: str, kind: str) -> list[dict]:
    """해당 concept 의 연간(10-K) 관측치, end 기준 최신 filed 우선, 최근연도 순."""
    node = facts.get(taxonomy, {}).get(tag)
    if not node:
        return []
    usd = node.get("units", {}).get("USD") or node.get("units", {}).get("shares") or []
    rows = []
    for x in usd:
        if x.get("form") != "10-K":
            continue
        if kind == "duration":
            if x.get("fp") != "FY" or not x.get("start"):
                continue
            if not (300 <= _days(x["start"], x["end"]) <= 380):
                continue
        rows.append(x)
    by_end: dict[str, dict] = {}
    for x in rows:
        if x["end"] not in by_end or x["filed"] > by_end[x["end"]]["filed"]:
            by_end[x["end"]] = x
    return sorted(by_end.values(), key=lambda x: x["end"], reverse=True)  # 최근 먼저


def _find_series(facts: dict, item: str) -> tuple[list[dict], str]:
    """후보 태그들을 **기간(end)별로 병합**한 연간 시계열, 최근연도 먼저.

    ⚠️ 예전에는 "값이 있는 첫 태그" 를 그대로 반환했다. 그러면 회사가 도중에 태그를 바꿨을 때
    낡은 태그의 시리즈가 그대로 최신값으로 쓰인다. 실측(2026-08-27, NVIDIA CIK 0001045810):
        RevenueFromContractWithCustomerExcludingAssessedTax → 최신 end 2022-01-30 (6건)
        Revenues                                            → 최신 end 2026-01-25 (18건)
    1순위 태그가 FY2022 에서 끊겼는데도 그것을 골라 **4년 묵은 매출**을 반환했다.
    같은 end 가 여러 태그에 있으면 우선순위가 높은(먼저 적힌) 태그를 쓰고, 태그가 바뀐
    구간은 다음 후보로 이어붙인다. 각 행에 실제 태그를 `_tag` 로 남겨 출처를 정확히 적는다.
    """
    kind, tags = ITEM_MAP[item]
    by_end: dict[str, dict] = {}
    used: list[str] = []
    for pri, tag in enumerate(tags):
        rows = _annual_rows(facts, "us-gaap", tag, kind)
        if rows:
            used.append(tag)
        for r in rows:
            prev = by_end.get(r["end"])
            if prev is None or prev["_pri"] > pri:
                by_end[r["end"]] = {**r, "_tag": tag, "_pri": pri}
    merged = sorted(by_end.values(), key=lambda x: x["end"], reverse=True)
    return merged, "+".join(used)


def _filing_url(accn: str, cik: str) -> str:
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K"


def financial_item(company: str, item: str, year: int | None = None) -> Value:
    """단일 재무 항목(연간, 10-K 기준). item ∈ ITEM_MAP. 값 단위 USD."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    ent = resolve(company)
    facts = _company_facts(ent["cik"])
    rows, tag = _find_series(facts, item)
    if not rows:
        raise DataError(f"{ent['title']}: '{ITEM_LABEL[item]}' 연간 데이터를 못 찾음")
    row = rows[0] if year is None else next(
        (r for r in rows if r["end"][:4] == str(year) or r.get("fy") == year), None)
    if row is None:
        raise DataError(f"{ent['title']} {year}년 '{ITEM_LABEL[item]}' 데이터 없음 "
                        f"(가용: {[r['end'][:4] for r in rows[:5]]})")
    return Value(
        value=row["val"], unit="USD",
        label=f"{ent['title']} {ITEM_LABEL[item]} (FY{row['end'][:4]})",
        provenance=Provenance(
            source="SEC EDGAR (XBRL)", source_type=SourceType.AUTHORITATIVE,
            source_url=_filing_url(row["accn"], ent["cik"]),
            original_field=f"us-gaap:{row.get('_tag') or tag}",
            as_of=f"FY{row['end'][:4]}", filing_date=row.get("filed"),
            note=(f"CIK={ent['cik']}, ticker={ent['ticker']}, accn={row['accn']}"
                  + (f". 이 항목은 회사가 기간에 따라 태그를 바꿔 후보를 병합했다({tag})"
                     if "+" in (tag or "") else "")),
        ),
    )


def financial_item_multiyear(company: str, item: str, year: int | None = None,
                             n: int = 3) -> dict:
    """엔진용: 최근 n개년 시리즈(최근연도 먼저).

    year 를 주지 않으면 **companyfacts 에 실제로 있는 최신 회계연도**부터 내려온다.
    _annual_rows 가 end 내림차순으로 정렬해 주므로 앞에서 n 개를 자르면 된다.
    """
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    ent = resolve(company)
    facts = _company_facts(ent["cik"])
    rows, tag = _find_series(facts, item)
    if not rows:
        raise DataError(f"{ent['title']}: '{ITEM_LABEL[item]}' 연간 데이터를 못 찾음")
    if year is not None:
        rows = [r for r in rows if int(r["end"][:4]) <= year]
    picked = rows[:max(1, int(n))]
    series = [{"period": f"FY{r['end'][:4]}", "year": int(r["end"][:4]),
               "amount": r["val"], "period_end": r["end"],
               "tag": r.get("_tag")} for r in picked]
    return {"corp_name": ent["title"], "ticker": ent["ticker"], "cik": ent["cik"],
            "rcept": picked[0]["accn"] if picked else "",
            "filing_date": picked[0].get("filed") if picked else None,
            "tag": tag, "item": item, "series": series}


def _quarter_rows(facts: dict, taxonomy: str, tag: str) -> list[dict]:
    """해당 concept 의 **분기 단독** 관측치(80~100일 duration), 최근 end 먼저.

    10-Q 는 분기 단독과 YTD 누적을 함께 태깅하므로 기간 길이로 분기만 골라낸다.
    같은 (start,end) 가 여러 번 정정 공시되면 filed 가 가장 늦은 것을 쓴다.
    """
    node = facts.get(taxonomy, {}).get(tag)
    if not node:
        return []
    rows = node.get("units", {}).get("USD") or []
    by_period: dict[tuple, dict] = {}
    for x in rows:
        if not x.get("start") or not x.get("end"):
            continue
        if not (80 <= _days(x["start"], x["end"]) <= 100):
            continue
        k = (x["start"], x["end"])
        if k not in by_period or x["filed"] > by_period[k]["filed"]:
            by_period[k] = x
    return sorted(by_period.values(), key=lambda x: x["end"], reverse=True)


def _duration_rows(facts: dict, tag: str) -> list[dict]:
    """(start,end) 중복은 최신 filed 로 접은 전체 기간 관측치, 최근 end 먼저."""
    node = facts.get("us-gaap", {}).get(tag)
    if not node:
        return []
    by_period: dict[tuple, dict] = {}
    for x in node.get("units", {}).get("USD") or []:
        if not x.get("start") or not x.get("end"):
            continue
        k = (x["start"], x["end"])
        if k not in by_period or x["filed"] > by_period[k]["filed"]:
            by_period[k] = x
    return sorted(by_period.values(), key=lambda x: x["end"], reverse=True)


def ltm_item(company: str, item: str) -> Value:
    """LTM(최근 12개월) 손익 항목. 단위 USD.

    LTM = 직전 회계연도 + 당해 누적(YTD) − 전년 동기 누적.
    **4개 분기를 더하는 방식은 쓰지 않는다** — 미국 발행인은 4분기(Q4) 단독 기간을 10-Q 에
    태깅하지 않아 분기 시계열에 매년 구멍이 난다(실측 2026-08, Micron: 90일 관측치가
    Q1·Q2·Q3 만 있고 Q4 가 없어서, 최근 4개를 그냥 더하면 전년 Q3 가 섞여 12개월이 아닌
    기간을 LTM 이라 부르게 된다 — 실제로 57.8bn 이 나왔고 올바른 값은 59.2bn 이었다).
    YTD 관측치는 10-Q 에 그대로 있어(272일·181일) 이쪽이 정확하다.
    """
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    if ITEM_MAP[item][0] != "duration":
        raise DataError(f"'{ITEM_LABEL[item]}' 은 시점 항목이라 LTM 개념이 없습니다.")
    ent = resolve(company)
    facts = _company_facts(ent["cik"])

    for tag in ITEM_MAP[item][1]:
        rows = _duration_rows(facts, tag)
        annuals = [x for x in rows if 300 <= _days(x["start"], x["end"]) <= 380]
        if not annuals:
            continue
        fy = annuals[0]
        ytds = [x for x in rows
                if abs(_days(fy["end"], x["start"])) <= 10
                and 80 <= _days(x["start"], x["end"]) <= 330]
        if not ytds:
            # 새 회계연도의 분기보고서가 아직 없음 → 연간값을 그대로, 라벨에 명시.
            f = lambda x: f"{x:,.0f}"  # noqa: E731
            return Value(
                fy["val"], "USD",
                label=f"{ent['title']} {ITEM_LABEL[item]} (FY{fy['end'][:4]} 연간 — LTM 아님)",
                provenance=Provenance(
                    source="SEC EDGAR (XBRL)", source_type=SourceType.AUTHORITATIVE,
                    source_url=_filing_url(fy["accn"], ent["cik"]),
                    original_field=f"us-gaap:{tag}", as_of=f"FY{fy['end'][:4]}",
                    filing_date=fy.get("filed"),
                    note=(f"{fy['end']} 이후 분기 누적 공시가 없어 LTM 을 만들 수 없습니다 → "
                          f"FY 연간값 {f(fy['val'])}. 기준기간을 비교표에 표시해야 합니다."),
                ),
            )
        cur = max(ytds, key=lambda x: (x["end"], _days(x["start"], x["end"])))
        cur_len = _days(cur["start"], cur["end"])
        prevs = [x for x in rows
                 if abs(_days(fy["start"], x["start"])) <= 10
                 and abs(_days(x["start"], x["end"]) - cur_len) <= 12]
        if not prevs:
            raise DataError(
                f"{ent['title']}: 전년 동기({cur_len}일) 누적 관측치가 없어 LTM 을 만들 수 "
                f"없습니다 (당해 누적 {cur['start']}~{cur['end']} 는 존재).")
        prev = max(prevs, key=lambda x: x["end"])
        ltm = fy["val"] + cur["val"] - prev["val"]
        f = lambda x: f"{x:,.0f}"  # noqa: E731
        return Value(
            ltm, "USD", label=f"{ent['title']} {ITEM_LABEL[item]} (LTM, ~{cur['end']})",
            provenance=Provenance(
                source="SEC EDGAR (XBRL)", source_type=SourceType.AUTHORITATIVE,
                source_url=_filing_url(cur["accn"], ent["cik"]),
                original_field=f"us-gaap:{tag} (연간 + YTD − 전년 YTD)",
                as_of=f"LTM~{cur['end']}", filing_date=cur.get("filed"),
                note=(f"LTM = FY({fy['start']}~{fy['end']}) {f(fy['val'])} "
                      f"+ YTD({cur['start']}~{cur['end']}, {cur_len}일) {f(cur['val'])} "
                      f"− 전년YTD({prev['start']}~{prev['end']}) {f(prev['val'])} "
                      f"= {f(ltm)}. CIK={ent['cik']}, ticker={ent['ticker']}"),
            ),
            extras={},
        )
    raise DataError(f"{ent['title']}: '{ITEM_LABEL[item]}' 의 기간 관측치를 찾지 못해 "
                    f"LTM 을 만들 수 없습니다 (시도한 태그: {ITEM_MAP[item][1]}).")


def _instant_series(facts: dict, tag: str) -> dict[str, float]:
    """시점 항목의 {end: val}. 같은 end 가 정정 공시되면 최신 filed 를 쓴다."""
    node = facts.get("us-gaap", {}).get(tag)
    if not node:
        return {}
    by_end: dict[str, dict] = {}
    for x in node.get("units", {}).get("USD") or []:
        if not x.get("end") or x.get("start"):     # start 가 있으면 기간 항목이다
            continue
        if x["end"] not in by_end or x["filed"] > by_end[x["end"]]["filed"]:
            by_end[x["end"]] = x
    return {end: row["val"] for end, row in by_end.items()}


def _instant_at(facts: dict, tags: list[str],
                on_or_before: str | None = None) -> tuple[float, str, str] | None:
    """후보 태그 중 첫 번째로 값이 있는 것 → (값, 태그, end). on_or_before 지정 시 그 날짜
    이하의 가장 늦은 관측치. 1순위 태그가 있으면 그것을 쓰고, 없을 때만 다음 후보로 간다."""
    for tag in tags:
        series = _instant_series(facts, tag)
        ends = [e for e in series if not on_or_before or e <= on_or_before]
        if not ends:
            continue
        end = max(ends)
        return series[end], tag, end
    return None


def _latest_end(facts: dict, tags: list[str]) -> str | None:
    for tag in tags:
        series = _instant_series(facts, tag)
        if series:
            return max(series)
    return None


def net_debt(company: str, include_lease: bool = True) -> Value:
    """순부채 = 이자발생부채 − 현금및현금성자산. 단위 USD.

    한국(DART) 정의와 같은 산식을 쓴다 — 단기투자자산·유가증권은 차감하지 않는다.
    시장마다 정의가 갈리면 배수 비교가 무의미해지므로 기준을 하나로 맞춘다.
    """
    ent = resolve(company)
    facts = _company_facts(ent["cik"])

    # 차입금과 현금의 최신 관측 시점이 다를 수 있다(실측 2026-08, Micron: LongTermDebt 는
    # 2025-11-27 이 마지막인데 현금은 2026-05-28 까지 있다 — 그대로 빼면 6개월 어긋난
    # 재무상태표를 섞어 순부채를 만든다). **공통 기준시점**을 먼저 정한다.
    debt_end_latest = _latest_end(facts, _DEBT_TOTAL_TAGS) or _latest_end(facts, _DEBT_PART_TAGS)
    cash_end_latest = _latest_end(facts, ITEM_MAP["cash"][1])
    if debt_end_latest is None:
        raise DataError(f"{ent['title']}: 차입금 태그를 찾지 못해 순부채를 계산할 수 없습니다 "
                        f"(시도: {_DEBT_TOTAL_TAGS + _DEBT_PART_TAGS})")
    if cash_end_latest is None:
        raise DataError(f"{ent['title']}: 현금및현금성자산을 찾지 못해 순부채를 계산할 수 없습니다.")
    asof = min(debt_end_latest, cash_end_latest)

    total = _instant_at(facts, _DEBT_TOTAL_TAGS, asof)
    parts: list[tuple[float, str, str]] = []
    if total is None:
        for tag in _DEBT_PART_TAGS:
            got = _instant_at(facts, [tag], asof)
            if got:
                parts.append(got)
        if not parts:
            raise DataError(f"{ent['title']}: {asof} 시점의 차입금 관측치가 없습니다.")
    debt = total[0] if total else sum(v for v, _, _ in parts)
    debt_src = (f"us-gaap:{total[1]}" if total
                else " + ".join(f"us-gaap:{t}" for _, t, _ in parts))
    debt_end = total[2] if total else max(e for _, _, e in parts)

    lease, lease_src = 0.0, ""
    if include_lease:
        found = []
        for tag in _LEASE_TAGS:
            got = _instant_at(facts, [tag], asof)
            # 같은 기준시점의 관측치만 더한다 — 폐지된 태그에 남은 옛 값이 섞이면 부채가
            # 조용히 과대계상된다(Micron 의 LongTermDebtNoncurrent 가 FY2012 에 멈춘 것처럼).
            if got and got[2] == debt_end:
                found.append((got[0], tag))
        lease = sum(v for v, _ in found)
        lease_src = " + ".join(f"us-gaap:{t}" for _, t in found)

    cash_got = _instant_at(facts, ITEM_MAP["cash"][1], asof)
    if cash_got is None:
        raise DataError(f"{ent['title']}: {asof} 시점의 현금및현금성자산 관측치가 없습니다.")
    cash, cash_tag, cash_end = cash_got

    ibd = debt + lease
    nd = ibd - cash
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    mismatch = ("" if debt_end == cash_end else
                f" ⚠️ 차입금({debt_end})과 현금({cash_end})의 기준시점이 다릅니다.")
    return Value(
        nd, "USD", label=f"{ent['title']} 순부채 ({debt_end})",
        provenance=Provenance(
            source="SEC EDGAR (XBRL)", source_type=SourceType.COMPUTED,
            source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ent['cik']}",
            original_field=f"{debt_src}{' + ' + lease_src if lease_src else ''} − us-gaap:{cash_tag}",
            as_of=debt_end,
            note=(f"IBD {f(ibd)} (차입금 {f(debt)}"
                  + (f" + 리스부채 {f(lease)}" if lease else "")
                  + f") − 현금 {f(cash)} = 순부채 {f(nd)}"
                  + (" → 순현금" if nd < 0 else "")
                  + f". 기준시점 {debt_end}. "
                  + "단기투자자산은 차감하지 않음(한국·대만과 정의 통일)." + mismatch),
        ),
        extras={},
    )


def shares_outstanding(company: str, year: int | None = None) -> Value:
    """발행주식수 (dei:EntityCommonStockSharesOutstanding). 단위 'shares'."""
    ent = resolve(company)
    facts = _company_facts(ent["cik"])
    node = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not node:
        raise DataError(f"{ent['title']}: 발행주식수 데이터를 못 찾음")
    rows = [x for x in node.get("units", {}).get("shares", []) if x.get("form") == "10-K"]
    if not rows:
        raise DataError(f"{ent['title']}: 발행주식수(10-K) 데이터를 못 찾음")
    rows.sort(key=lambda x: x["end"], reverse=True)
    row = rows[0] if year is None else next((r for r in rows if r["end"][:4] == str(year)), rows[0])
    return Value(
        value=row["val"], unit="shares", label=f"{ent['title']} 발행주식수 ({row['end'][:4]})",
        provenance=Provenance(
            source="SEC EDGAR (XBRL)", source_type=SourceType.AUTHORITATIVE,
            source_url=_filing_url(row["accn"], ent["cik"]),
            original_field="dei:EntityCommonStockSharesOutstanding",
            as_of=f"FY{row['end'][:4]}", filing_date=row.get("filed"),
            note=f"CIK={ent['cik']}, ticker={ent['ticker']}",
        ),
    )


# ── 공시 검색·원문 조회 (fallback: XBRL 로 안 잡히는 계열사·연관회사 조사용) ──────
# 실측 확인(2026-08): efts.sec.gov 는 회사 지정 없이도 전체 공시 대상 키워드 검색이
# 되는 keyless 통합검색 — DART 의 회사별 list.json 보다 오히려 더 넓게 찾을 수 있다.
_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def search_filings(keyword: str, company: str | None = None, forms: str | None = None,
                   start_date: str | None = None, end_date: str | None = None,
                   max_results: int = 20) -> list[dict]:
    """EDGAR 전체회사 통합 원문검색. keyword 는 정확한 문구를 찾으려면 직접 겹따옴표로
    감싸서 넘겨라(예: '"FICT Limited"'). company 지정 시 그 회사의 CIK 로 좁힌다.
    forms: 쉼표구분 문서유형(예: '10-K,10-Q'). start_date/end_date: YYYY-MM-DD."""
    params: dict = {"q": keyword}
    if company:
        params["ciks"] = resolve(company)["cik"]
    if forms:
        params["forms"] = forms
    if start_date:
        params["startdt"] = start_date
    if end_date:
        params["enddt"] = end_date
    j = get_json(_SEARCH_URL, ttl_hours=6, params=params, headers=_headers())
    hits = ((j.get("hits") or {}).get("hits")) or []
    out = []
    for h in hits[:max_results]:
        src = h.get("_source", {})
        accession, _, filename = (h.get("_id") or "").partition(":")
        out.append({
            "cik": (src.get("ciks") or [None])[0], "accession": accession, "filename": filename,
            "form": src.get("form"), "file_date": src.get("file_date"),
            "display_name": (src.get("display_names") or [None])[0],
        })
    return out


def filing_text(cik: str, accession: str, filename: str, keyword: str | None = None,
                context_chars: int = 200, max_chars: int = 8000, max_matches: int = 20) -> dict:
    """search_filings 결과의 cik/accession/filename 으로 실제 공시 문서(HTML)를 받아
    텍스트로. keyword 지정 시 등장 부분만 앞뒤 context_chars 와 함께 반환(전체가 아님)."""
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
          f"{accession.replace('-', '')}/{filename}")
    r = session().get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    plain = _SCRIPT_RE.sub(" ", r.text)
    plain = _TAG_RE.sub(" ", plain)
    plain = plain.replace("&nbsp;", " ")
    plain = re.sub(r"&[a-z]+;", " ", plain)
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n\s*\n+", "\n", plain).strip()

    if keyword:
        # 영문 문서라 대소문자 표기가 제각각(예: "Supply Chain" vs "supply chain") —
        # 대소문자 무시로 위치를 찾고, 발췌는 원문 표기 그대로 보여준다.
        plain_lower, kw_lower = plain.lower(), keyword.lower()
        idxs, start = [], 0
        while True:
            idx = plain_lower.find(kw_lower, start)
            if idx == -1:
                break
            idxs.append(idx)
            start = idx + 1
        excerpts = []
        for idx in idxs[:max_matches]:
            s, e = max(0, idx - context_chars), min(len(plain), idx + len(keyword) + context_chars)
            excerpts.append(plain[s:e].replace("\n", " "))
        return {
            "url": url, "total_chars": len(plain), "keyword": keyword,
            "matches": len(idxs), "excerpts": excerpts,
            "note": None if idxs else "키워드가 문서에 없음(정확한 표기를 다시 확인하라)",
        }

    truncated = len(plain) > max_chars
    return {
        "url": url, "total_chars": len(plain), "text": plain[:max_chars],
        "truncated": truncated,
        "note": ("문서가 길어 앞부분만 반환됨. 특정 회사명/키워드를 찾으려면 keyword 인자를 지정하라."
                if truncated else None),
    }


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    j = probe("GET", f"{_BASE_WWW}/files/company_tickers.json", headers=_headers()).json()
    if not j:
        raise DataError("company_tickers.json 이 비어 있습니다")
    return f"티커 매핑 {len(j):,}건"

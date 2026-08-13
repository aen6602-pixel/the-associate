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
from functools import lru_cache

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
}
ITEM_LABEL = {
    "revenue": "Revenue", "operating_income": "Operating Income", "net_income": "Net Income",
    "total_assets": "Total Assets", "total_liabilities": "Total Liabilities",
    "total_equity": "Total Stockholders' Equity",
}


def _headers() -> dict:
    return {"User-Agent": config.require(config.SEC_USER_AGENT, "SEC_USER_AGENT")}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# ── 기업 매핑 ────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = re.sub(r"\b(inc|corp|corporation|co|ltd|llc|company|the|plc)\b\.?", "", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


@lru_cache(maxsize=1)
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
@lru_cache(maxsize=128)
def _company_facts(cik: str) -> dict:
    j = get_json(f"{_BASE_DATA}/api/xbrl/companyfacts/CIK{cik}.json",
                ttl_hours=24 * 3, headers=_headers())
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
    kind, tags = ITEM_MAP[item]
    for tag in tags:
        rows = _annual_rows(facts, "us-gaap", tag, kind)
        if rows:
            return rows, tag
    return [], ""


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
            original_field=f"us-gaap:{tag}",
            as_of=f"FY{row['end'][:4]}", filing_date=row.get("filed"),
            note=f"CIK={ent['cik']}, ticker={ent['ticker']}, accn={row['accn']}",
        ),
    )


def financial_item_multiyear(company: str, item: str, year: int | None = None) -> dict:
    """엔진용: 최근 3개년 시리즈(최근연도 먼저)."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    ent = resolve(company)
    facts = _company_facts(ent["cik"])
    rows, tag = _find_series(facts, item)
    if not rows:
        raise DataError(f"{ent['title']}: '{ITEM_LABEL[item]}' 연간 데이터를 못 찾음")
    if year is not None:
        rows = [r for r in rows if int(r["end"][:4]) <= year]
    top3 = rows[:3]
    series = [{"period": f"FY{r['end'][:4]}", "amount": r["val"]} for r in top3]
    return {"corp_name": ent["title"], "ticker": ent["ticker"], "cik": ent["cik"],
            "rcept": top3[0]["accn"] if top3 else "", "filing_date": top3[0].get("filed") if top3 else None,
            "item": item, "series": series}


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

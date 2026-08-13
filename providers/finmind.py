"""FinMind provider (대만 상장사 재무·주가) — 종목 매핑 + 재무제표. reference.

- TaiwanStockInfo: 종목코드(4자리) ↔ 회사명(중국어) 매핑
- TaiwanStockFinancialStatements: 손익 항목. 분기 단위(누적 아님) → 연간 = 4개 분기 합산
- TaiwanStockBalanceSheet: 재무상태표 항목. 분기말 시점값 → 연말(12/31) 스냅샷 사용
- TaiwanStockShareholding: 발행주식총수(NumberOfSharesIssued, 集保 기준 주간 갱신)

실측 확인(2026-08, 대만적체전로제조(TSMC) 2330): 2023년 4개 분기 Revenue 합계가
공개된 실제 2023년 연간매출(약 2조 1,617억 TWD)과 정확히 일치 → 분기값이 "당분기" 단독
금액(YTD 누적이 아님)임을 확인. 재무상태표는 분기말 스냅샷이라 합산 없이 그대로 사용.

한글/영문 회사명 매핑 데이터가 없어(중국어 stock_name만 제공) 회사명 조회는
정식 중국어 명칭 또는 4자리 종목코드로만 가능하다(예: '台積電' 또는 '2330').
"""
from __future__ import annotations

import difflib
from datetime import date
from functools import lru_cache

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json
from core import config

_BASE = "https://api.finmindtrade.com/api/v4/data"

# item 키 → (kind: income(4분기 합산)|balance(연말 시점), [FinMind type 후보, 우선순위순])
ITEM_MAP: dict[str, tuple] = {
    "revenue": ("income", ["Revenue"]),
    "operating_income": ("income", ["OperatingIncome"]),
    "net_income": ("income", ["IncomeAfterTaxes"]),
    "total_assets": ("balance", ["TotalAssets"]),
    "total_liabilities": ("balance", ["Liabilities"]),
    "total_equity": ("balance", ["Equity", "EquityAttributableToOwnersOfParent"]),
}
ITEM_LABEL = {
    "revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
}


def _token() -> str:
    return config.require(config.Keys.FINMIND, "FINMIND_TOKEN")


def _get(dataset: str, data_id: str | None = None, start_date: str | None = None,
         end_date: str | None = None, ttl_hours: float = 24) -> list[dict]:
    params = {"dataset": dataset, "token": _token()}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    j = get_json(_BASE, ttl_hours=ttl_hours, params=params)
    if j.get("status") != 200:
        raise DataError(f"FinMind 오류: {j.get('status')} {j.get('msg')}")
    return j.get("data", [])


# ── 종목 매핑 ────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _company_index() -> tuple[dict, dict]:
    """TaiwanStockInfo → (종목코드→entry, 회사명(중국어)→entry). 동일 코드 중복 행은 최신 date 우선."""
    rows = _get("TaiwanStockInfo", ttl_hours=24 * 7)
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for r in rows:
        sid = (r.get("stock_id") or "").strip()
        name = (r.get("stock_name") or "").strip()
        if not sid:
            continue
        entry = {"stock_id": sid, "stock_name": name,
                "industry_category": r.get("industry_category"), "market": r.get("type")}
        prev = by_id.get(sid)
        if prev is None or (r.get("date") or "") >= prev.get("_date", ""):
            entry["_date"] = r.get("date") or ""
            by_id[sid] = entry
        if name:
            by_name.setdefault(name, entry)
    return by_id, by_name


def resolve(company: str) -> dict:
    """종목코드(4자리) 또는 정식 중국어 회사명 → {stock_id, stock_name, industry_category, market}.
    영문/한글 회사명 매핑 데이터가 없어 지원하지 않음(예: 'TSMC' 대신 '2330' 또는 '台積電')."""
    q = company.strip()
    by_id, by_name = _company_index()
    if q.isdigit() and len(q) == 4:
        if q in by_id:
            return by_id[q]
        raise DataError(f"종목코드 {q} 를 FinMind 에서 못 찾음")
    if q in by_name:
        return by_name[q]
    cands = [e for name, e in by_name.items() if q in name or name in q]
    if cands:
        return sorted(cands, key=lambda e: len(e["stock_name"]))[0]
    close = difflib.get_close_matches(q, list(by_name.keys()), n=1, cutoff=0.6)
    if close:
        return by_name[close[0]]
    raise DataError(f"FinMind 에서 종목을 못 찾음: '{company}' "
                    "(4자리 종목코드 또는 정식 중국어 명칭으로 시도하세요, 예: 2330)")


# ── 손익(4개 분기 합산) ────────────────────────────────────────────
@lru_cache(maxsize=256)
def _income_rows(stock_id: str, start_year: int, end_year: int) -> tuple[dict, ...]:
    rows = _get("TaiwanStockFinancialStatements", data_id=stock_id,
               start_date=f"{start_year}-01-01", end_date=f"{end_year}-12-31", ttl_hours=24 * 3)
    return tuple(rows)


@lru_cache(maxsize=256)
def _balance_rows(stock_id: str, start_year: int, end_year: int) -> tuple[dict, ...]:
    rows = _get("TaiwanStockBalanceSheet", data_id=stock_id,
               start_date=f"{start_year}-01-01", end_date=f"{end_year}-12-31", ttl_hours=24 * 3)
    return tuple(rows)


def _latest_complete_year(stock_id: str, max_back_years: int = 4) -> int:
    """Revenue 4개 분기가 모두 존재하는 가장 최근 연도. 없으면 데이터가 있는 최신 연도(부분)."""
    this_year = date.today().year
    rows = _income_rows(stock_id, this_year - max_back_years, this_year)
    by_year: dict[int, int] = {}
    for r in rows:
        if r.get("type") == "Revenue":
            yr = int(r["date"][:4])
            by_year[yr] = by_year.get(yr, 0) + 1
    for yr in range(this_year, this_year - max_back_years - 1, -1):
        if by_year.get(yr, 0) >= 4:
            return yr
    if by_year:
        return max(by_year)
    raise DataError(f"stock_id={stock_id}: 손익 데이터를 전혀 찾지 못함")


def _sum_income(stock_id: str, year: int, candidates: list[str]) -> tuple[float, str, int]:
    rows = _income_rows(stock_id, year, year)
    for cand in candidates:
        vals = [r["value"] for r in rows if r.get("type") == cand and r["date"][:4] == str(year)]
        if vals:
            return sum(vals), cand, len(vals)
    return None, None, 0


def _year_end_balance(stock_id: str, year: int, candidates: list[str]) -> tuple[float, str, str]:
    rows = _balance_rows(stock_id, year, year)
    for cand in candidates:
        vals = [(r["date"], r["value"]) for r in rows
               if r.get("type") == cand and r["date"][:4] == str(year)]
        if vals:
            vals.sort(key=lambda x: x[0])
            d, v = vals[-1]  # 해당 연도 내 가장 늦은 시점(통상 12/31) 스냅샷
            return v, cand, d
    return None, None, None


def financial_item(company: str, item: str, year: int | None = None) -> Value:
    """단일 재무 항목(연간). item ∈ ITEM_MAP. 값 단위 TWD."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    kind, candidates = ITEM_MAP[item]
    ent = resolve(company)
    sid = ent["stock_id"]
    yr = year if year is not None else _latest_complete_year(sid)

    if kind == "income":
        val, matched, n_quarters = _sum_income(sid, yr, candidates)
        if val is None:
            raise DataError(f"{ent['stock_name']}({sid}) {yr}년 '{ITEM_LABEL[item]}' 데이터를 못 찾음")
        note = (f"stock_id={sid}, FinMind type={matched}, 분기합산({n_quarters}개 분기)"
                + ("" if n_quarters == 4 else f" — 연간 미완성(4개 중 {n_quarters}개만 존재)"))
        as_of_date = f"{yr}-12-31"
    else:
        val, matched, as_of_date = _year_end_balance(sid, yr, candidates)
        if val is None:
            raise DataError(f"{ent['stock_name']}({sid}) {yr}년 '{ITEM_LABEL[item]}' 데이터를 못 찾음")
        note = f"stock_id={sid}, FinMind type={matched}, 시점={as_of_date}"

    return Value(
        value=val, unit="TWD",
        label=f"{ent['stock_name']}({sid}) {ITEM_LABEL[item]} (FY{yr})",
        provenance=Provenance(
            source="FinMind (대만 시장데이터)", source_type=SourceType.REFERENCE,
            source_url=f"https://finmindtrade.com/analysis/#/data/details?id={('TaiwanStockFinancialStatements' if kind == 'income' else 'TaiwanStockBalanceSheet')}",
            original_field=matched, as_of=f"FY{yr}", filing_date=as_of_date, note=note,
        ),
    )


def financial_item_multiyear(company: str, item: str, year: int | None = None) -> dict:
    """엔진용: 최근 3개년 시리즈(최근연도 먼저)."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    kind, candidates = ITEM_MAP[item]
    ent = resolve(company)
    sid = ent["stock_id"]
    yr = year if year is not None else _latest_complete_year(sid)
    series = []
    for y in (yr, yr - 1, yr - 2):
        if kind == "income":
            val, _, _ = _sum_income(sid, y, candidates)
        else:
            val, _, _ = _year_end_balance(sid, y, candidates)
        if val is not None:
            series.append({"period": f"FY{y}", "amount": val})
    if not series:
        raise DataError(f"{ent['stock_name']}({sid}): '{ITEM_LABEL[item]}' 연간 데이터를 못 찾음")
    return {"corp_name": ent["stock_name"], "stock_id": sid, "item": item, "series": series}


# ── 발행주식총수 ─────────────────────────────────────────────────
def shares_outstanding(company: str, year: int | None = None) -> Value:
    """발행주식총수(NumberOfSharesIssued, 集保 기준). 단위 '股'(주)."""
    ent = resolve(company)
    sid = ent["stock_id"]
    yr = year if year is not None else date.today().year
    rows = _get("TaiwanStockShareholding", data_id=sid,
               start_date=f"{yr}-01-01", end_date=f"{yr}-12-31", ttl_hours=24 * 3)
    vals = [(r["date"], r["NumberOfSharesIssued"]) for r in rows if r.get("NumberOfSharesIssued")]
    if not vals:
        raise DataError(f"{ent['stock_name']}({sid}) {yr}년 발행주식총수를 못 찾음")
    vals.sort(key=lambda x: x[0])
    d, shares = vals[-1]
    return Value(
        value=shares, unit="股", label=f"{ent['stock_name']}({sid}) 발행주식총수 ({d})",
        provenance=Provenance(
            source="FinMind (대만 시장데이터)", source_type=SourceType.REFERENCE,
            source_url="https://finmindtrade.com/analysis/#/data/details?id=TaiwanStockShareholding",
            original_field="NumberOfSharesIssued", as_of=d, filing_date=d,
            note=f"stock_id={sid}, 集保(예탁결제) 기준 주간 갱신",
        ),
    )

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
from core.cache import TTL_FRESH, TTL_INDEX, ttl_cache

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json
from core import config

_BASE = "https://api.finmindtrade.com/api/v4/data"

# item 키 → (kind, [FinMind type 후보, 우선순위순])
#   kind=income   : 손익. **분기 단독** 금액 → 연간 = 4개 분기 합산
#   kind=balance  : 재무상태표. 분기말 시점값
#   kind=cashflow : 현금흐름표. ⚠️ **연초 누적(YTD)** 이다 — 손익과 의미론이 다르다
#
# 실측 2026-08-27 (TSMC 2330): 손익 OperatingIncome 은 분기별 249/286/360/425(십억TWD)로
# 4개 합이 FY2024 영업이익 1.32조와 일치 → 단독. 반면 CF 의 Depreciation 은 156.7 → 319.6
# → 485.5 → 653.6 으로 단조증가 → YTD 누적. 같은 provider 안에서 두 의미론이 섞여 있으므로
# 합산 방식을 kind 로 갈라야 한다(둘을 같게 다루면 D&A 가 2~4배로 부풀거나 1/4 로 줄어든다).
ITEM_MAP: dict[str, tuple] = {
    "revenue": ("income", ["Revenue"]),
    "operating_income": ("income", ["OperatingIncome"]),
    "net_income": ("income", ["IncomeAfterTaxes"]),
    "total_assets": ("balance", ["TotalAssets"]),
    "total_liabilities": ("balance", ["Liabilities"]),
    "total_equity": ("balance", ["Equity", "EquityAttributableToOwnersOfParent"]),
    "cash": ("balance", ["CashAndCashEquivalents"]),
    "ppe": ("balance", ["PropertyPlantAndEquipment"]),
    "inventories": ("balance", ["Inventories"]),
    "trade_receivables": ("balance", ["AccountsReceivableNet"]),
    "trade_payables": ("balance", ["AccountsPayable"]),
    "da": ("cashflow", ["Depreciation", "AmortizationExpense"]),   # 두 계정을 **합산**한다
}
ITEM_LABEL = {
    "revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
    "cash": "현금및현금성자산", "ppe": "유형자산", "inventories": "재고자산",
    "trade_receivables": "매출채권", "trade_payables": "매입채무",
    "da": "감가상각비+무형자산상각비(D&A)",
}

# 순부채용 차입금 계정(재무상태표). 대만 공시에는 리스부채가 별도 type 으로 없어
# (RightOfUseAsset 은 자산 쪽에만 존재) IFRS 16 리스부채를 포함할 수 없다 → note 에 명시한다.
_DEBT_TYPES = ["ShorttermBorrowings", "LongtermBorrowings", "BondsPayable"]

# MOPS(公開資訊觀測站) 회사별 공시 페이지. **원문 표를 파싱한 것이 아니라 탐색 진입점**이다 —
# 숫자의 출처는 FinMind(2차)이고, 이 링크는 사용자가 대만 공시 원문으로 건너가기 위한 것이다.
# (t164sb04 등 재무제표 페이지는 POST 폼이라 결정론적 GET 링크가 만들어지지 않고, 이 호스트는
#  사내 프록시에서 DNS 해석이 간헐적으로 실패한다 — 그래서 '검증된 원문 소스' 로 표기하지 않는다.)
MOPS_COMPANY_URL = "https://mopsov.twse.com.tw/mops/web/t05st01?TYPEK=sii&co_id={sid}"


def mops_url(stock_id: str) -> str:
    return MOPS_COMPANY_URL.format(sid=stock_id)


_DATASET_FOR = {"income": "TaiwanStockFinancialStatements",
                "balance": "TaiwanStockBalanceSheet",
                "cashflow": "TaiwanStockCashFlowsStatement"}


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
@ttl_cache(TTL_INDEX, maxsize=1)
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
@ttl_cache(TTL_FRESH, maxsize=256)   # 새 분기 공시를 반영해야 한다
def _income_rows(stock_id: str, start_year: int, end_year: int) -> tuple[dict, ...]:
    rows = _get("TaiwanStockFinancialStatements", data_id=stock_id,
               start_date=f"{start_year}-01-01", end_date=f"{end_year}-12-31", ttl_hours=24)
    return tuple(rows)


@ttl_cache(TTL_FRESH, maxsize=256)   # 새 분기 공시를 반영해야 한다
def _balance_rows(stock_id: str, start_year: int, end_year: int) -> tuple[dict, ...]:
    rows = _get("TaiwanStockBalanceSheet", data_id=stock_id,
               start_date=f"{start_year}-01-01", end_date=f"{end_year}-12-31", ttl_hours=24)
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


@ttl_cache(TTL_FRESH, maxsize=256)   # 새 분기 공시를 반영해야 한다
def _cashflow_rows(stock_id: str, start_year: int, end_year: int) -> tuple[dict, ...]:
    rows = _get("TaiwanStockCashFlowsStatement", data_id=stock_id,
               start_date=f"{start_year}-01-01", end_date=f"{end_year}-12-31", ttl_hours=24)
    return tuple(rows)


def _cf_ytd(stock_id: str, year: int, types: list[str],
            quarter_end: str | None = None) -> tuple[float, str] | None:
    """현금흐름표 YTD 누적. quarter_end 미지정 시 해당 연도의 가장 늦은 시점.
    후보 type 들을 **합산**한다(D&A = 감가상각 + 무형상각)."""
    rows = _cashflow_rows(stock_id, year, year)
    dates = sorted({r["date"] for r in rows
                    if r.get("type") in types and r["date"][:4] == str(year)
                    and (quarter_end is None or r["date"] == quarter_end)})
    if not dates:
        return None
    d = dates[-1]
    total = sum(r["value"] for r in rows if r.get("type") in types and r["date"] == d)
    return total, d


def _quarter_dates(stock_id: str, year: int, candidates: list[str]) -> list[str]:
    rows = _income_rows(stock_id, year, year)
    return sorted({r["date"] for r in rows
                   if r.get("type") in candidates and r["date"][:4] == str(year)})


def ltm_item(company: str, item: str) -> Value:
    """LTM(최근 12개월) 항목. 값 단위 TWD.

    손익은 분기 단독이라 **최근 4개 분기 합**, 현금흐름표는 YTD 누적이라
    **전년 연간 + 당해 누적 − 전년 동기 누적** 으로 각각 다르게 계산한다.
    """
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    kind, candidates = ITEM_MAP[item]
    if kind == "balance":
        raise DataError(f"'{ITEM_LABEL[item]}' 은 시점 항목이라 LTM 개념이 없습니다.")
    ent = resolve(company)
    sid = ent["stock_id"]
    this_year = date.today().year
    f = lambda x: f"{x:,.0f}"  # noqa: E731

    if kind == "income":
        rows = _income_rows(sid, this_year - 2, this_year)
        pts = sorted(((r["date"], r["value"]) for r in rows if r.get("type") in candidates),
                     key=lambda x: x[0])
        # 같은 date 에 여러 type 후보가 잡히면(예: Equity 후보 2개) 첫 후보만 남긴다.
        for cand in candidates:
            got = sorted(((r["date"], r["value"]) for r in rows if r.get("type") == cand),
                         key=lambda x: x[0])
            if got:
                pts = got
                break
        if len(pts) < 4:
            raise DataError(f"{ent['stock_name']}({sid}): '{ITEM_LABEL[item]}' 분기 관측치가 "
                            f"{len(pts)}개뿐이라 LTM(4개 분기)을 만들 수 없습니다.")
        picked = pts[-4:]
        total = sum(v for _, v in picked)
        return Value(
            total, "TWD",
            label=f"{ent['stock_name']}({sid}) {ITEM_LABEL[item]} (LTM, ~{picked[-1][0]})",
            provenance=Provenance(
                source="FinMind (대만 시장데이터)", source_type=SourceType.REFERENCE,
                source_url="https://finmindtrade.com/analysis/#/data/details?id=TaiwanStockFinancialStatements",
                original_field=f"{candidates[0]} (분기 4개 합)", as_of=f"LTM~{picked[-1][0]}",
                filing_date=picked[-1][0],
                note=(f"LTM = " + " + ".join(f"{d} {f(v)}" for d, v in picked)
                      + f" = {f(total)}. 손익은 분기 단독 금액이라 단순 합산. stock_id={sid}"),
            ),
        )

    # cashflow: YTD 누적
    for yr in (this_year, this_year - 1):
        cur = _cf_ytd(sid, yr, candidates)
        if cur is None:
            continue
        cur_val, cur_date = cur
        if cur_date.endswith("-12-31"):
            return Value(
                cur_val, "TWD",
                label=f"{ent['stock_name']}({sid}) {ITEM_LABEL[item]} (FY{yr} 연간)",
                provenance=Provenance(
                    source="FinMind (대만 시장데이터)", source_type=SourceType.REFERENCE,
                    source_url="https://finmindtrade.com/analysis/#/data/details?id=TaiwanStockCashFlowsStatement",
                    original_field="+".join(candidates) + " (연말 YTD 누적)",
                    as_of=f"FY{yr}", filing_date=cur_date,
                    note=(f"연말(12-31) YTD 누적 = FY{yr} 연간 {f(cur_val)}. "
                          f"기준기간이 12개월이므로 LTM 과 동일. stock_id={sid}"),
                ),
            )
        prev_full = _cf_ytd(sid, yr - 1, candidates, f"{yr-1}-12-31")
        prev_same = _cf_ytd(sid, yr - 1, candidates, cur_date.replace(str(yr), str(yr - 1), 1))
        if prev_full is None or prev_same is None:
            raise DataError(f"{ent['stock_name']}({sid}): 전년 누적({yr-1})을 못 찾아 "
                            f"'{ITEM_LABEL[item]}' LTM 을 만들 수 없습니다.")
        ltm = prev_full[0] + cur_val - prev_same[0]
        return Value(
            ltm, "TWD", label=f"{ent['stock_name']}({sid}) {ITEM_LABEL[item]} (LTM, ~{cur_date})",
            provenance=Provenance(
                source="FinMind (대만 시장데이터)", source_type=SourceType.REFERENCE,
                source_url="https://finmindtrade.com/analysis/#/data/details?id=TaiwanStockCashFlowsStatement",
                original_field="+".join(candidates) + " (연간 + YTD − 전년 YTD)",
                as_of=f"LTM~{cur_date}", filing_date=cur_date,
                note=(f"LTM = FY{yr-1} 연간 {f(prev_full[0])} + {cur_date} 누적 {f(cur_val)} "
                      f"− {prev_same[1]} 누적 {f(prev_same[0])} = {f(ltm)}. "
                      f"현금흐름표는 YTD 누적이라 이 방식으로 계산. stock_id={sid}"),
            ),
        )
    raise DataError(f"{ent['stock_name']}({sid}): '{ITEM_LABEL[item]}' 현금흐름표 관측치를 못 찾음")


def net_debt(company: str, year: int | None = None) -> Value:
    """순부채 = 차입금(단기+장기+사채) − 현금및현금성자산. 단위 TWD.

    대만 공시에는 리스부채가 별도 계정으로 없어 IFRS 16 리스부채를 포함할 수 없다 —
    한국·미국(리스 포함)과 정의가 어긋나는 부분이므로 note 에 남긴다.
    """
    ent = resolve(company)
    sid = ent["stock_id"]
    # 연간(12-31)이 아니라 **가장 최근 분기말** 재무상태표를 쓴다 — 순부채는 시점값이고,
    # 분자(시가총액)가 오늘 기준이므로 되도록 최신 시점이어야 한다. 연도를 못 넘어가면
    # 6개월 이상 묵은 순부채로 EV 를 만들게 된다.
    if year is not None:
        rows = _balance_rows(sid, year, year)
    else:
        this_year = date.today().year
        rows = _balance_rows(sid, this_year - 1, this_year)
    wanted = _DEBT_TYPES + ["CashAndCashEquivalents"]
    dates = sorted({r["date"] for r in rows if r.get("type") in wanted})
    if not dates:
        raise DataError(f"{ent['stock_name']}({sid}) 차입금·현금 계정을 못 찾음"
                        + (f" ({year}년)" if year else ""))
    d = dates[-1]      # 같은 분기말 시점으로 맞춘다
    debt_parts = {t: sum(r["value"] for r in rows if r.get("type") == t and r["date"] == d)
                  for t in _DEBT_TYPES}
    debt_parts = {t: v for t, v in debt_parts.items() if v}
    cash_rows = [r["value"] for r in rows
                 if r.get("type") == "CashAndCashEquivalents" and r["date"] == d]
    if not cash_rows:
        raise DataError(f"{ent['stock_name']}({sid}) {d} 현금및현금성자산을 못 찾음")
    cash = sum(cash_rows)
    debt = sum(debt_parts.values())
    nd = debt - cash
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    return Value(
        nd, "TWD", label=f"{ent['stock_name']}({sid}) 순부채 ({d})",
        provenance=Provenance(
            source="FinMind (대만 시장데이터)", source_type=SourceType.COMPUTED,
            source_url="https://finmindtrade.com/analysis/#/data/details?id=TaiwanStockBalanceSheet",
            original_field=" + ".join(debt_parts) + " − CashAndCashEquivalents", as_of=d,
            filing_date=d,
            note=(f"차입금 {f(debt)} ("
                  + " + ".join(f"{t} {f(v)}" for t, v in debt_parts.items())
                  + f") − 현금 {f(cash)} = 순부채 {f(nd)}"
                  + (" → 순현금" if nd < 0 else "")
                  + ". ⚠️ 대만 공시에 리스부채 계정이 없어 IFRS 16 리스부채 미포함"
                    "(한국·미국은 포함) — 정의 차이를 비교표에 표시할 것."),
        ),
    )


def financial_item(company: str, item: str, year: int | None = None) -> Value:
    """단일 재무 항목(연간). item ∈ ITEM_MAP. 값 단위 TWD."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    kind, candidates = ITEM_MAP[item]
    ent = resolve(company)
    sid = ent["stock_id"]
    yr = year if year is not None else _latest_complete_year(sid)

    if kind == "cashflow":
        got = _cf_ytd(sid, yr, candidates, f"{yr}-12-31") or _cf_ytd(sid, yr, candidates)
        if got is None:
            raise DataError(f"{ent['stock_name']}({sid}) {yr}년 '{ITEM_LABEL[item]}' "
                            f"현금흐름표 데이터를 못 찾음")
        val, as_of_date = got
        note = (f"stock_id={sid}, FinMind type={'+'.join(candidates)}, YTD 누적({as_of_date})"
                + ("" if as_of_date.endswith("-12-31") else
                   " — ⚠️ 연말이 아닌 시점의 누적이라 연간이 아니다"))
        matched = "+".join(candidates)
    elif kind == "income":
        val, matched, n_quarters = _sum_income(sid, yr, candidates)
        if val is None:
            raise DataError(f"{ent['stock_name']}({sid}) {yr}년 '{ITEM_LABEL[item]}' 데이터를 못 찾음")
        # 4개 분기가 다 있어야 '연간' 이라고 부를 수 있다. 부족하면 값을 감추지 않되
        # 연간으로 오독되지 않게 경고를 앞세운다(보고서 지적: 분기 합산 정합성 검증).
        note = (f"stock_id={sid}, FinMind type={matched}, 분기합산({n_quarters}개 분기)"
                + ("" if n_quarters == 4 else
                   f" — ⚠️ 연간 미완성: 4개 분기 중 {n_quarters}개만 존재하므로 이 값은 "
                   f"FY{yr} 연간이 아니다. 연간 비교·배수에 쓰면 안 된다."))
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
            source_url=mops_url(sid),
            original_field=matched, as_of=f"FY{yr}", filing_date=as_of_date,
            note=(note + f" 숫자의 출처는 FinMind(2차, dataset={_DATASET_FOR[kind]})이고 "
                         f"source_url 은 MOPS 회사 공시 페이지(원문 탐색 진입점)다 — "
                         f"거래소 원문 표를 직접 파싱한 값이 아니다."),
        ),
    )


def financial_item_multiyear(company: str, item: str, year: int | None = None,
                             n: int = 3) -> dict:
    """엔진용: 최근 n개년 시리즈(최근연도 먼저).

    year 를 주지 않으면 _latest_complete_year(4개 분기가 모두 있는 최신 연도)부터 내려온다.
    """
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    kind, candidates = ITEM_MAP[item]
    ent = resolve(company)
    sid = ent["stock_id"]
    yr = year if year is not None else _latest_complete_year(sid)
    series = []
    for y in range(yr, yr - max(1, int(n)), -1):
        if kind == "income":
            val, _, _ = _sum_income(sid, y, candidates)
        elif kind == "cashflow":
            got = _cf_ytd(sid, y, candidates, f"{y}-12-31")
            val = got[0] if got else None
        else:
            val, _, _ = _year_end_balance(sid, y, candidates)
        if val is not None:
            series.append({"period": f"FY{y}", "year": y, "amount": val})
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


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    j = probe("GET", _BASE, params={"dataset": "TaiwanStockInfo", "data_id": "2330",
                                    "token": _token()}).json()
    if j.get("status") != 200:
        raise DataError(f"FinMind 오류: {j.get('status')} {j.get('msg')}")
    return "대만 종목정보 조회 OK"

"""DART provider (한국 금융감독원 전자공시) — 기업 매핑 + 재무제표. authoritative.

- corpCode.xml: 기업명/종목코드 ↔ corp_code 매핑 (월 1회 갱신 캐시)
- fnlttSinglAcntAll: 전체 재무제표 계정 (3개년: 당기/전기/전전기)

LLM 도구는 단일 항목(get_financial_item)만 노출하고,
상증법/DCF 엔진은 statement()/financial_item() 를 코드로 직접 호출한다.
"""
from __future__ import annotations

import io
import re
import difflib
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from datetime import date

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_bytes, get_json
from core import config

_BASE = "https://opendart.fss.or.kr/api"

# 사업보고서 유형
REPRT = {"annual": "11011", "half": "11012", "q1": "13013", "q3": "11014"}

# item 키 → (sj_div, [account_id 후보], [account_nm 후보])
ITEM_MAP: dict[str, tuple] = {
    # 매출액도 회사에 따라 별도 손익계산서(IS) 없이 포괄손익계산서(CIS)만 공시하는 경우가 있어
    # (예: 에스케이에코플랜트 FY2024) operating_income/net_income 과 동일하게 IS·CIS 둘 다 탐색.
    "revenue": (("IS", "CIS"), ["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
                ["매출액", "수익(매출액)", "영업수익"]),
    "operating_income": (("IS", "CIS"), ["dart_OperatingIncomeLoss",
                                          "ifrs-full_ProfitLossFromOperatingActivities"],
                         ["영업이익", "영업이익(손실)"]),
    # 당기순이익은 단일 포괄손익계산서(CIS)만 있는 회사도 있어 IS·CIS 둘 다 탐색
    "net_income": (("IS", "CIS"), ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "total_assets": ("BS", ["ifrs-full_Assets"], ["자산총계"]),
    "total_liabilities": ("BS", ["ifrs-full_Liabilities"], ["부채총계"]),
    "total_equity": ("BS", ["ifrs-full_Equity",
                            "ifrs-full_EquityAttributableToOwnersOfParent"], ["자본총계"]),
    # DCF 전체모델(5시트 워크북)용 추가 항목 — 실측 확인(2026-08, 삼성전자 기준)된 태그.
    "cogs": (("IS", "CIS"), ["ifrs-full_CostOfSales"], ["매출원가"]),
    "sga": (("IS", "CIS"), ["dart_TotalSellingGeneralAdministrativeExpenses"], ["판매비와관리비"]),
    "interest_expense": (("IS", "CIS"), ["ifrs-full_FinanceCosts"], ["금융비용", "이자비용"]),
    "tax_expense": (("IS", "CIS"), ["ifrs-full_IncomeTaxExpenseContinuingOperations",
                                    "ifrs-full_IncomeTaxExpense"], ["법인세비용"]),
    "ppe": ("BS", ["ifrs-full_PropertyPlantAndEquipment"], ["유형자산"]),
    "cash": ("BS", ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
}
ITEM_LABEL = {
    "revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
    "cogs": "매출원가", "sga": "판매비와관리비", "interest_expense": "이자비용(금융비용)",
    "tax_expense": "법인세비용", "ppe": "유형자산", "cash": "현금및현금성자산",
}


# ── 기업 매핑 ────────────────────────────────────────────────────
# 영문 약칭 ↔ 한글 등록명 정규화 (DART 는 한글 등록명 사용: SK→에스케이 등)
_PREFIX_ALIAS = {
    "에스케이": "sk", "엘지": "lg", "지에스": "gs", "케이티앤지": "ktng", "케이티": "kt",
    "씨제이": "cj", "포스코": "posco", "엘에스": "ls", "케이비": "kb", "엔에이치": "nh",
    "에이치디": "hd", "디엘": "dl", "오씨아이": "oci", "에스디": "sd", "제이비": "jb",
    "네이버": "naver", "카카오": "kakao",  # 영문 등록명 매칭용
    "현대차": "현대자동차", "기아차": "기아",  # 구어 약칭 확장
}


def _norm_name(s: str) -> str:
    """비교용 정규화: 소문자, 법인격/공백/기호 제거, 한글약칭→영문."""
    s = str(s).lower().strip()
    s = re.sub(r"주식회사|\(주\)|㈜|\(유\)|holdings", "", s)
    for ko, en in _PREFIX_ALIAS.items():
        s = s.replace(ko, en)
    s = re.sub(r"[\s.,\-·'&()]", "", s)
    return s


@lru_cache(maxsize=1)
def _corp_index() -> tuple[dict, dict, dict]:
    """corpCode.xml → (name→[entry], stock_code→entry, norm_name→[entry])."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    raw = get_bytes(f"{_BASE}/corpCode.xml", ttl_hours=24 * 30, params={"crtfc_key": key})
    zf = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    by_name: dict[str, list] = {}
    by_stock: dict[str, dict] = {}
    by_norm: dict[str, list] = {}
    for e in root.findall("list"):
        entry = {
            "corp_code": (e.findtext("corp_code") or "").strip(),
            "corp_name": (e.findtext("corp_name") or "").strip(),
            "stock_code": (e.findtext("stock_code") or "").strip(),
        }
        by_name.setdefault(entry["corp_name"], []).append(entry)
        if entry["stock_code"]:
            by_stock[entry["stock_code"]] = entry
        by_norm.setdefault(_norm_name(entry["corp_name"]), []).append(entry)
    return by_name, by_stock, by_norm


def _best(entries: list) -> dict:
    """상장사 우선, 그다음 최단 이름."""
    return sorted(entries, key=lambda e: (not e["stock_code"], len(e["corp_name"])))[0]


def suggest(company: str, n: int = 6) -> list[str]:
    """유사한 기업명 후보(원문 표기)."""
    _, _, by_norm = _corp_index()
    close = difflib.get_close_matches(_norm_name(company), list(by_norm.keys()), n=n, cutoff=0.6)
    return [_best(by_norm[c])["corp_name"] for c in close]


def resolve(company: str) -> dict:
    """회사명(대충 입력 허용) 또는 6자리 종목코드 → {corp_code, corp_name, stock_code}."""
    q = company.strip()
    by_name, by_stock, by_norm = _corp_index()
    if q.isdigit() and len(q) == 6:                       # 종목코드
        if q in by_stock:
            return by_stock[q]
        raise DataError(f"종목코드 {q} 를 DART 에서 못 찾음")
    if q in by_name:                                      # 원문 정확 일치
        return _best(by_name[q])
    qn = _norm_name(q)
    if qn and qn in by_norm:                              # 정규화 정확 일치 (SK↔에스케이 등)
        return _best(by_norm[qn])
    if qn:                                                # 정규화 부분 일치
        cands = []
        for norm, lst in by_norm.items():
            if not norm:
                continue
            if qn in norm:                                # 입력이 정식명의 일부 (좋은 매치)
                cands += [(0, norm, e) for e in lst]
            elif norm in qn and len(norm) >= max(3, int(len(qn) * 0.6)):
                cands += [(1, norm, e) for e in lst]      # 등록명이 입력의 일부(짧은 건 배제)
        if cands:
            # 방향0 우선 → 상장사 우선 → 길이 근접 → 최단명
            cands.sort(key=lambda t: (t[0], not t[2]["stock_code"],
                                      abs(len(t[1]) - len(qn)), len(t[2]["corp_name"])))
            return cands[0][2]
    close = difflib.get_close_matches(qn, list(by_norm.keys()), n=1, cutoff=0.78)  # 오타 허용
    if close:
        return _best(by_norm[close[0]])
    sug = suggest(company)
    hint = f" 혹시 이 회사인가요?: {', '.join(sug)}" if sug else ""
    raise DataError(f"DART 에서 기업을 못 찾음: '{company}'.{hint}")


# ── 재무제표 ─────────────────────────────────────────────────────
def _fetch_all(corp_code: str, year: int, reprt_code: str, fs_div: str) -> tuple[list, str]:
    key = config.require(config.Keys.DART, "DART_API_KEY")
    j = get_json(f"{_BASE}/fnlttSinglAcntAll.json", ttl_hours=24 * 3, params={
        "crtfc_key": key, "corp_code": corp_code, "bsns_year": str(year),
        "reprt_code": reprt_code, "fs_div": fs_div,
    })
    if j.get("status") != "000":
        raise DataError(f"DART 재무제표 오류: {j.get('status')} {j.get('message')}")
    return j.get("list", []), fs_div


def _statement_rows(corp_code: str, year: int, reprt_code: str,
                    prefer: str = "CFS") -> tuple[list, str]:
    """prefer 우선(CFS=연결/OFS=별도), 없으면 다른 쪽으로 fallback."""
    order = ["CFS", "OFS"] if prefer.upper() == "CFS" else ["OFS", "CFS"]
    last_err = None
    for div in order:
        try:
            rows, _ = _fetch_all(corp_code, year, reprt_code, div)
            if rows:
                return rows, ("연결(CFS)" if div == "CFS" else "별도(OFS)")
        except DataError as e:
            last_err = e
    if last_err:
        raise last_err
    raise DataError("재무제표 행이 비어있음")


def _to_int(s) -> int | None:
    s = (s or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _match(rows: list, item: str):
    sj, ids, names = ITEM_MAP[item]
    sjs = sj if isinstance(sj, tuple) else (sj,)
    subset = [r for r in rows if r.get("sj_div") in sjs]
    for r in subset:
        if r.get("account_id") in ids:
            return r
    for r in subset:
        if r.get("account_nm") in names:
            return r
    return None


def _filing_date(rcept: str) -> str | None:
    return f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}" if len(rcept or "") >= 8 else None


@lru_cache(maxsize=512)
def _latest_year(corp_code: str, reprt_code: str, prefer: str = "CFS") -> int:
    """실제로 데이터가 존재하는 가장 최근 사업연도를 탐색한다.
    'date.today().year - 1' 로 무작정 찍으면 아직 그 연도 보고서가 없거나(신규상장 등)
    이미 더 최신 보고서가 나와있는데도 예전 연도를 쓰는 문제가 생긴다 — 그래서 실제 존재 여부를
    이번 연도부터 최대 5년 역순으로 직접 확인한다. 전부 없으면(비상장) 감사보고서 접수연도로 fallback."""
    this_year = date.today().year
    for yr in range(this_year, this_year - 5, -1):
        try:
            rows, _ = _statement_rows(corp_code, yr, reprt_code, prefer)
            if rows:
                return yr
        except DataError:
            continue
    from providers import dart_audit
    latest = dart_audit.latest_audit_year(corp_code)
    if latest is not None:
        return latest
    raise DataError("실제 데이터가 존재하는 사업연도를 찾지 못함(정기보고서·감사보고서 모두 없음)")


def _audit_value(ent: dict, item: str, yr: int, amt: int, rcept: str) -> Value:
    """비상장 감사보고서 파싱 결과를 Value 로."""
    return Value(
        value=amt, unit="KRW",
        label=f"{ent['corp_name']} {ITEM_LABEL[item]} ({yr}, 감사보고서·별도)",
        provenance=Provenance(
            source="DART 감사보고서(원문 파싱)",
            source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
            original_field=f"감사보고서 재무제표 표 파싱: {ITEM_LABEL[item]}",
            as_of=f"FY{yr}", filing_date=_filing_date(rcept),
            note=f"비상장 → 감사보고서 원문 표 파싱(근사). corp_code={ent['corp_code']}",
        ),
    )


def financial_item(company: str, item: str, year: int | None = None,
                   report: str = "annual", prefer: str = "CFS") -> Value:
    """단일 재무 항목 조회. item ∈ ITEM_MAP. 값 단위 KRW.
    정기보고서(fnlttSinglAcntAll) 우선, 없으면(비상장 등) 감사보고서 파싱 fallback."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)

    try:
        rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
        row = _match(rows, item)
        if row is None:
            raise DataError(f"{ent['corp_name']} {yr} 에서 '{ITEM_LABEL[item]}' 계정을 못 찾음")
        amt = _to_int(row.get("thstrm_amount"))
        if amt is None:
            raise DataError(f"{ent['corp_name']} {ITEM_LABEL[item]} 금액이 비어있음")
        rcept = row.get("rcept_no", "")
        return Value(
            value=amt, unit=row.get("currency") or "KRW",
            label=f"{ent['corp_name']} {ITEM_LABEL[item]} ({yr}, {fs_label})",
            provenance=Provenance(
                source="DART (금융감독원)",
                source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field=f"{row.get('sj_div')}/{row.get('account_id')}/{row.get('account_nm')}",
                as_of=f"FY{yr}", filing_date=_filing_date(rcept),
                note=f"corp_code={ent['corp_code']}, 종목={ent['stock_code']}",
            ),
        )
    except DataError as e:
        if report != "annual":                       # 감사보고서는 연간만
            raise
        from providers import dart_audit
        try:
            amt, rcept, ay = dart_audit.year_value(ent["corp_code"], item, yr)
        except DataError:
            raise e                                   # 원래 에러(013 등) 유지
        return _audit_value(ent, item, ay, amt, rcept)


def sga_item(company: str, year: int | None = None, report: str = "annual",
            prefer: str = "CFS") -> Value:
    """판매비와관리비. 통합 계정이 없는 회사(실측 확인: 오리온 — '판매비'+'일반관리비'로
    분리 공시)는 두 계정을 합산해 대신 반환한다."""
    try:
        return financial_item(company, "sga", year, report, prefer)
    except DataError as e:
        reprt_code = REPRT.get(report, "11011")
        ent = resolve(company)
        yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
        rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
        total, rcept = 0, None
        found = False
        for r in rows:
            if r.get("sj_div") not in ("IS", "CIS"):
                continue
            nm = _norm_label(r.get("account_nm") or "")
            if nm in ("판매비", "일반관리비"):
                amt = _to_int(r.get("thstrm_amount"))
                if amt is not None:
                    total += amt
                    rcept = rcept or r.get("rcept_no")
                    found = True
        if not found:
            raise e
        return Value(
            value=total, unit="KRW",
            label=f"{ent['corp_name']} 판매비와관리비 ({yr}, {fs_label}, 판매비+일반관리비 합산)",
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field="IS·CIS/판매비+일반관리비(합산)", as_of=f"FY{yr}",
                filing_date=_filing_date(rcept),
                note=f"통합 판관비 계정이 없어 판매비+일반관리비 합산. corp_code={ent['corp_code']}",
            ),
        )


def statement(company: str, year: int | None = None, report: str = "annual") -> dict:
    """엔진용: 주요 계정 전체를 {item: Value} 로 반환 (상증법/DCF 재료)."""
    out = {}
    for item in ITEM_MAP:
        try:
            out[item] = financial_item(company, item, year, report)
        except DataError:
            out[item] = None
    return out


def financial_item_multiyear(company: str, item: str, year: int | None = None,
                             report: str = "annual", prefer: str = "CFS") -> dict:
    """엔진용: 한 항목의 3개년(당기/전기/전전기) 값을 반환.
    series 는 최근연도 먼저. 상증법 3개년 가중평균에 사용."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    try:
        rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
        row = _match(rows, item)
        if row is None:
            raise DataError(f"{ent['corp_name']} {yr} '{ITEM_LABEL[item]}' 계정을 못 찾음")
        rcept = row.get("rcept_no", "")
        series = []
        for amt_key, nm_key in (("thstrm_amount", "thstrm_nm"),
                                ("frmtrm_amount", "frmtrm_nm"),
                                ("bfefrmtrm_amount", "bfefrmtrm_nm")):
            a = _to_int(row.get(amt_key))
            if a is not None:
                series.append({"period": (row.get(nm_key) or "").strip(), "amount": a})
        return {"corp_name": ent["corp_name"], "stock_code": ent["stock_code"],
                "corp_code": ent["corp_code"], "fs_label": fs_label,
                "rcept": rcept, "filing_date": _filing_date(rcept),
                "item": item, "series": series}
    except DataError as e:
        if report != "annual":
            raise
        from providers import dart_audit
        my = dart_audit.multiyear(ent["corp_code"], item, yr)
        if not my:
            raise e
        series = [{"period": f"FY{x['year']}", "amount": x["amount"]} for x in my]
        return {"corp_name": ent["corp_name"], "stock_code": ent["stock_code"],
                "corp_code": ent["corp_code"], "fs_label": "감사보고서(별도, 파싱)",
                "rcept": my[0]["rcept"], "filing_date": _filing_date(my[0]["rcept"]),
                "item": item, "series": series}


def financial_item_nyear(company: str, item: str, n: int = 5, year: int | None = None,
                         report: str = "annual", prefer: str = "CFS") -> dict:
    """엔진용: n개년(기본 5개년) 시리즈. fnlttSinglAcntAll 한 번 호출로는 당기/전기/전전기
    (3개년)만 나오므로, n>3 이면 (최근연도-2) 기준으로 한 번 더 호출해 이어붙인다.
    5시트 DCF 워크북의 히스토리 P&L 용."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    end_yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    years_needed = list(range(end_yr, end_yr - n, -1))
    amounts: dict[int, int | None] = {}
    rcept = None

    def _collect(anchor_yr: int):
        nonlocal rcept
        try:
            rows, _ = _statement_rows(ent["corp_code"], anchor_yr, reprt_code, prefer)
        except DataError:
            return
        row = _match(rows, item)
        if row is None:
            return
        if rcept is None:
            rcept = row.get("rcept_no", "")
        for amt_key, offset in (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2)):
            y = anchor_yr - offset
            if y in years_needed and amounts.get(y) is None:
                a = _to_int(row.get(amt_key))
                if a is not None:
                    amounts[y] = a

    _collect(end_yr)
    missing = [y for y in years_needed if amounts.get(y) is None]
    if missing and n > 3:
        _collect(min(missing) + 2)  # 그 연도가 frmtrm/bfefrmtrm 으로 나오는 보고서 앵커

    series = [{"year": y, "amount": amounts.get(y)} for y in years_needed]
    return {"corp_name": ent["corp_name"], "stock_code": ent["stock_code"],
            "corp_code": ent["corp_code"], "item": item, "rcept": rcept,
            "filing_date": _filing_date(rcept) if rcept else None, "series": series}


def _norm_label(s: str) -> str:
    """계정명 비교용 정규화 — 회사마다 공백 유무가 달라(실측 확인: 삼성전자 '자산부채' vs
    오리온 '자산 부채') 내부 공백을 전부 제거하고 비교한다."""
    return re.sub(r"\s+", "", s or "")


def sga_item_nyear(company: str, n: int = 5, year: int | None = None,
                   report: str = "annual", prefer: str = "CFS") -> dict:
    """판매비와관리비 n개년. 통합계정이 없는 연도만 sga_item() 의 판매비+일반관리비
    합산 fallback 으로 개별 보완."""
    base = financial_item_nyear(company, "sga", n, year, report, prefer)
    for pt in base["series"]:
        if pt["amount"] is None:
            try:
                pt["amount"] = sga_item(company, pt["year"], report, prefer).value
            except DataError:
                pass
    return base


def cf_extras_nyear(company: str, n: int = 5, year: int | None = None, report: str = "annual",
                    prefer: str = "CFS") -> dict:
    """capex/ocf/nwc_change/da 의 n개년 시리즈. financial_item_nyear 와 동일한 2-앵커
    스티칭(현금흐름표도 전기·전전기 비교공시가 있어 실측 확인됨)."""
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    end_yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    years_needed = list(range(end_yr, end_yr - n, -1))

    def _cf_rows(anchor_yr: int) -> list:
        try:
            rows, _ = _statement_rows(ent["corp_code"], anchor_yr, reprt_code, prefer)
        except DataError:
            return []
        return [r for r in rows if r.get("sj_div") == "CF"]

    anchors = {end_yr: _cf_rows(end_yr)}
    covered = {end_yr, end_yr - 1, end_yr - 2}
    missing = [y for y in years_needed if y not in covered]
    if missing:
        anchor2 = min(missing) + 2
        anchors[anchor2] = _cf_rows(anchor2)

    def _match_row(cf_rows: list, tags: list, all_of: list | None = None,
                   any_of: list | None = None):
        for r in cf_rows:
            if r.get("account_id") in tags:
                return r
        for r in cf_rows:
            nm = _norm_label(r.get("account_nm") or "")
            if all_of and all(x in nm for x in all_of):
                return r
            if any_of and any(x in nm for x in any_of):
                return r
        return None

    def _series(matcher) -> list[dict]:
        out: dict[int, int] = {}
        for anchor_yr, cf_rows in anchors.items():
            row = matcher(cf_rows)
            if row is None:
                continue
            for amt_key, offset in (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2)):
                y = anchor_yr - offset
                if y in years_needed and y not in out:
                    a = _to_int(row.get(amt_key))
                    if a is not None:
                        out[y] = a
        return [{"year": y, "amount": out.get(y)} for y in years_needed]

    capex = _series(lambda rows: _match_row(
        rows, ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        all_of=["유형자산의취득"]))
    ocf = _series(lambda rows: _match_row(
        rows, ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], all_of=["영업활동", "현금흐름"]))
    nwc = _series(lambda rows: _match_row(
        rows, ["dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"],
        any_of=["운전자본", "자산부채"]))

    da_out: dict[int, int] = {}
    for anchor_yr, cf_rows in anchors.items():
        da_rows = [r for r in cf_rows
                  if "감가상각" in (r.get("account_nm") or "") or "상각비" in (r.get("account_nm") or "")]
        for amt_key, offset in (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2)):
            y = anchor_yr - offset
            if y not in years_needed or y in da_out:
                continue
            total, any_found = 0, False
            for r in da_rows:
                a = _to_int(r.get(amt_key))
                if a is not None:
                    total, any_found = total + a, True
            if any_found:
                da_out[y] = total
    da = [{"year": y, "amount": da_out.get(y)} for y in years_needed]

    return {"corp_name": ent["corp_name"], "capex": capex, "ocf": ocf,
            "nwc_change": nwc, "da": da}


def debt_balances(company: str, year: int | None = None, report: str = "annual",
                  prefer: str = "CFS") -> dict:
    """단기·장기 이자부담 차입금 총액(BS). account_id 가 '-표준계정코드 미사용-' 인 경우가
    많고(실측 확인) 계정명도 회사마다 다양(예: 오리온 '유동성 금융기관 차입금(사채 제외)') —
    '차입금'·'사채' 를 포함하는 계정명을 전부 모아 단기/장기로 분류해 합산한다."""
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
    bs_rows = [r for r in rows if r.get("sj_div") == "BS"]

    st_amt, lt_amt, st_rcept, lt_rcept = 0, 0, None, None
    for r in bs_rows:
        nm = _norm_label(r.get("account_nm") or "")
        if "차입금" not in nm and "사채" not in nm:
            continue
        amt = _to_int(r.get("thstrm_amount"))
        if amt is None:
            continue
        is_short = ("단기" in nm) or ("유동성" in nm) or ("유동" in nm and "비유동" not in nm)
        if is_short:
            st_amt += amt
            st_rcept = st_rcept or r.get("rcept_no")
        else:
            lt_amt += amt
            lt_rcept = lt_rcept or r.get("rcept_no")
    rcept = st_rcept or lt_rcept

    def _v(amt: int, label: str) -> Value:
        return Value(
            value=amt, unit="KRW", label=f"{ent['corp_name']} {label} ({yr})",
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}" if rcept else "",
                original_field=f"BS/{label}", as_of=f"FY{yr}",
                filing_date=_filing_date(rcept) if rcept else None,
                note=f"corp_code={ent['corp_code']}, {fs_label}",
            ),
        )
    return {"short_term": _v(st_amt, "단기차입금(유동성장기부채 포함)"),
            "long_term": _v(lt_amt, "장기차입금")}


def cf_extras(company: str, year: int | None = None, report: str = "annual",
              prefer: str = "CFS") -> dict:
    """현금흐름표(CF)에서 capex/ocf/nwc_change/da 를 한 번의 조회로 추출.
    da(감가상각비)는 회사마다 CF 표시 방식이 달라(실측 확인: 오리온처럼 '감가상각비 조정'
    계열로 여러 줄 나오는 회사도, 삼성전자·SK하이닉스처럼 '조정'(기타 비현금조정) 한 줄에
    뭉쳐 분리가 안 되는 회사도 있음) — 계정명에 '감가상각'·'상각비' 포함하는 CF 행을 전부
    합산하고, 하나도 없으면 da=None(가정 필요, 조용히 틀린 값을 채우지 않음)."""
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
    cf_rows = [r for r in rows if r.get("sj_div") == "CF"]

    def _by_tag_or_name(tags: list, all_of: list | None = None,
                        any_of: list | None = None) -> tuple[int | None, str | None]:
        """계정명은 회사마다 표현이 달라도(실측 확인: 삼성전자 '영업활동현금흐름' vs
        SK에코플랜트 '영업활동으로 인한 현금흐름', 삼성전자 '자산부채의 변동' vs
        SK에코플랜트 '운전자본 조정') XBRL 표준계정코드(account_id)는 대부분 동일 —
        태그로 먼저 찾고, 회사 커스텀 태그('-표준계정코드 미사용-')인 경우만 이름으로 보완.
        all_of=전부 포함해야 매칭(예: '영업활동'+'현금흐름' 둘 다 있어야 — 안 그러면 '재무활동
        현금흐름'/'자산부채의 변동' 같은 다른 줄과 헷갈림). any_of=하나만 있어도 매칭."""
        for r in cf_rows:
            if r.get("account_id") in tags:
                return _to_int(r.get("thstrm_amount")), r.get("rcept_no")
        for r in cf_rows:
            nm = _norm_label(r.get("account_nm") or "")
            if all_of and all(n in nm for n in all_of):
                return _to_int(r.get("thstrm_amount")), r.get("rcept_no")
            if any_of and any(n in nm for n in any_of):
                return _to_int(r.get("thstrm_amount")), r.get("rcept_no")
        return None, None

    capex, capex_rcept = _by_tag_or_name(
        ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        all_of=["유형자산의취득"])
    ocf, ocf_rcept = _by_tag_or_name(
        ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], all_of=["영업활동", "현금흐름"])
    nwc, nwc_rcept = _by_tag_or_name(
        ["dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"],
        any_of=["운전자본", "자산부채"])

    da_total, da_rcept = None, None
    for r in cf_rows:
        nm = r.get("account_nm") or ""
        if "감가상각" in nm or "상각비" in nm:
            amt = _to_int(r.get("thstrm_amount"))
            if amt is not None:
                da_total = (da_total or 0) + amt
                da_rcept = da_rcept or r.get("rcept_no")

    rcept = capex_rcept or ocf_rcept or nwc_rcept or da_rcept

    def _v(amt, label: str, note: str | None = None) -> Value | None:
        if amt is None:
            return None
        return Value(
            value=amt, unit="KRW", label=f"{ent['corp_name']} {label} ({yr})",
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}" if rcept else "",
                original_field=f"CF/{label}", as_of=f"FY{yr}",
                filing_date=_filing_date(rcept) if rcept else None,
                note=note or f"corp_code={ent['corp_code']}, {fs_label}",
            ),
        )
    return {
        "capex": _v(capex, "유형자산의 취득(CAPEX)"),
        "ocf": _v(ocf, "영업활동현금흐름"),
        "nwc_change": _v(nwc, "영업활동으로 인한 자산부채의 변동"),
        "da": _v(da_total, "감가상각비(CF 조정 합산)",
                note=f"'감가상각'/'상각비' 포함 CF 행 합산. corp_code={ent['corp_code']}"),
    }


def shares_outstanding(company: str, year: int | None = None, report: str = "annual") -> Value:
    """발행주식총수(합계, 현재까지 발행한 주식의 총수). 단위 '주'.
    note 에 보통주/우선주/자기주식 내역 표기."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    ent = resolve(company)
    reprt_code = REPRT.get(report, "11011")
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, "CFS")
    try:
        j = get_json(f"{_BASE}/stockTotqySttus.json", ttl_hours=24 * 3, params={
            "crtfc_key": key, "corp_code": ent["corp_code"],
            "bsns_year": str(yr), "reprt_code": reprt_code,
        })
        if j.get("status") != "000":
            raise DataError(f"DART 주식총수 오류: {j.get('status')} {j.get('message')}")
        total = common = preferred = treasury = None
        for r in j.get("list", []):
            se = (r.get("se") or "").strip()
            v = _to_int(r.get("now_to_isu_stock_totqy"))
            if se == "합계":
                total, treasury = v, _to_int(r.get("tesstk_co"))
            elif se == "보통주":
                common = v
            elif se == "우선주":
                preferred = v
        shares = total if total else common
        if not shares or shares <= 0:
            raise DataError(f"{ent['corp_name']} 발행주식총수를 못 구함")
        rcept = (j.get("list") or [{}])[0].get("rcept_no", "")
        note = (f"보통주 {common:,} / 우선주 {(preferred or 0):,} / 자기주식 {(treasury or 0):,}"
                if common is not None else None)
        return Value(
            value=shares, unit="주", label=f"{ent['corp_name']} 발행주식총수 ({yr})",
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field="stockTotqySttus/now_to_isu_stock_totqy(합계)",
                as_of=f"FY{yr}", filing_date=_filing_date(rcept), note=note,
            ),
        )
    except DataError as e:
        if report != "annual":
            raise
        from providers import dart_audit
        try:
            n, rcept, ry = dart_audit.shares(ent["corp_code"], yr)
        except DataError as ae:
            raise DataError(
                f"{ent['corp_name']} 발행주식총수 조회 실패: 비상장(정기보고서 주식총수 없음) "
                f"+ 감사보고서 파싱 실패 ({ae})"
            ) from e
        return Value(
            value=n, unit="주", label=f"{ent['corp_name']} 발행주식총수 ({ry}, 감사보고서)",
            provenance=Provenance(
                source="DART 감사보고서(주석 파싱)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field="감사보고서 주석: 발행주식수",
                as_of=f"FY{ry}", filing_date=_filing_date(rcept),
                note=f"비상장 → 감사보고서 주석 정규식 파싱(근사). corp_code={ent['corp_code']}",
            ),
        )


# ── 공시 검색·원문 조회 (fallback: 자체 corp_code 없는 회사 조사용) ─────────────
# resolve() 로 못 찾는 회사(비상장 자회사·해외법인 등)는 그룹 지주사/계열사 공시 본문에
# 언급돼 있는 경우가 많다(예: 'SK에코플랜트' 사업보고서의 '사업의 내용'에 자회사 '에센코어'의
# 매출·영업이익이 서술돼 있음). 이 두 함수는 resolve() 가 실패한 뒤에만 쓰는 fallback 경로.
def _best_decode(buf: bytes) -> str:
    """EUC-KR/UTF-8 자동판별 — 치환문자(깨짐) 적은 쪽 채택. 옛 공시는 EUC-KR가 많다."""
    a = buf.decode("euc-kr", errors="replace")
    b = buf.decode("utf-8", errors="replace")
    return a if a.count("�") <= b.count("�") else b


_HTML_STRIP = [
    (re.compile(r"<style[\s\S]*?</style>", re.I), " "),
    (re.compile(r"<script[\s\S]*?</script>", re.I), " "),
    (re.compile(r"<head[\s\S]*?</head>", re.I), " "),
    (re.compile(r"<!--[\s\S]*?-->"), " "),
    (re.compile(r"<[^>]+>"), " "),
]
_ENTITY_RE = re.compile(r"&[a-zA-Z]+;")


def _strip_html(raw: str) -> str:
    s = raw
    for pat, repl in _HTML_STRIP:
        s = pat.sub(repl, s)
    s = s.replace("&nbsp;", " ")
    s = _ENTITY_RE.sub(" ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def list_filings(corp: str, bgn_de: str, end_de: str, kw: str | None = None,
                 max_pages: int = 5) -> list[dict]:
    """공시목록 검색(list.json). corp: 회사명 또는 8자리 corp_code. bgn_de/end_de: YYYYMMDD.
    kw 지정 시 report_nm(보고서명)에 그 문자열이 포함된 것만. 최신순."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    corp_code = corp if (corp.isdigit() and len(corp) == 8) else resolve(corp)["corp_code"]
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        j = get_json(f"{_BASE}/list.json", ttl_hours=6, params={
            "crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de,
            "page_no": page, "page_count": 100,
        })
        status = j.get("status")
        if status == "013":  # 조회된 데이타가 없습니다
            break
        if status != "000":
            raise DataError(f"DART 공시검색 오류: {status} {j.get('message')}")
        for it in j.get("list", []):
            if not kw or kw in (it.get("report_nm") or ""):
                out.append({
                    "rcept_no": it.get("rcept_no"), "rcept_dt": it.get("rcept_dt"),
                    "report_nm": it.get("report_nm"), "flr_nm": it.get("flr_nm"),
                    "corp_name": it.get("corp_name"),
                })
        if page >= j.get("total_page", 1):
            break
        page += 1
    return out


def filing_text(rcept_no: str, keyword: str | None = None, context_chars: int = 200,
                max_chars: int = 8000, max_matches: int = 20) -> dict:
    """공시 원문(document.xml) → ZIP 해제 → HTML 태그 제거한 평문.
    keyword 지정 시: 등장 부분만 앞뒤 context_chars 만큼 잘라 반환(전체 원문이 아님) — 특정
    회사명 언급을 찾을 때 씀. 미지정 시 앞부분 max_chars 만 반환(길면 truncated=True)."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    raw = get_bytes(f"{_BASE}/document.xml", ttl_hours=24 * 30,
                    params={"crtfc_key": key, "rcept_no": rcept_no})
    texts = []
    if raw[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(raw))
        for name in zf.namelist():
            texts.append(_best_decode(zf.read(name)))
    else:
        texts.append(_best_decode(raw))
    plain = _strip_html("\n".join(texts))

    if keyword:
        # 영문 회사명(예: "ESSENCORE" vs "Essencore")은 표기가 제각각이라 대소문자 무시로
        # 위치를 찾고, 발췌는 원문 표기 그대로 보여준다(실측 확인: 이 차이로 검색이 안 잡힌 사례 있음).
        plain_lower, kw_lower = plain.lower(), keyword.lower()
        idxs = []
        start = 0
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
            "rcept_no": rcept_no, "total_chars": len(plain), "keyword": keyword,
            "matches": len(idxs), "excerpts": excerpts,
            "note": (None if idxs else
                    "키워드가 원문에 없음(정확한 표기를 다시 확인하거나 다른 계열사를 시도하라)"),
        }

    truncated = len(plain) > max_chars
    return {
        "rcept_no": rcept_no, "total_chars": len(plain), "text": plain[:max_chars],
        "truncated": truncated,
        "note": ("문서가 길어 앞부분만 반환됨. 특정 회사명/키워드를 찾으려면 keyword 인자를 지정하라."
                if truncated else None),
    }

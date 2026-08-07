"""DART provider (한국 금융감독원 전자공시) — 기업 매핑 + 재무제표. authoritative.

- corpCode.xml: 기업명/종목코드 ↔ corp_code 매핑 (월 1회 갱신 캐시)
- fnlttSinglAcntAll: 전체 재무제표 계정 (3개년: 당기/전기/전전기)

LLM 도구는 단일 항목(get_financial_item)만 노출하고,
상증법/DCF 엔진은 statement()/financial_item() 를 코드로 직접 호출한다.
"""
from __future__ import annotations

import io
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
    "revenue": ("IS", ["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
                ["매출액", "수익(매출액)", "영업수익"]),
    "operating_income": ("IS", ["dart_OperatingIncomeLoss",
                                 "ifrs-full_ProfitLossFromOperatingActivities"],
                         ["영업이익", "영업이익(손실)"]),
    "net_income": ("IS", ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "total_assets": ("BS", ["ifrs-full_Assets"], ["자산총계"]),
    "total_liabilities": ("BS", ["ifrs-full_Liabilities"], ["부채총계"]),
    "total_equity": ("BS", ["ifrs-full_Equity",
                            "ifrs-full_EquityAttributableToOwnersOfParent"], ["자본총계"]),
}
ITEM_LABEL = {
    "revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
}


# ── 기업 매핑 ────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _corp_index() -> tuple[dict, dict]:
    """corpCode.xml → (name→[entry...], stock_code→entry)."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    raw = get_bytes(f"{_BASE}/corpCode.xml", ttl_hours=24 * 30, params={"crtfc_key": key})
    zf = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    by_name: dict[str, list] = {}
    by_stock: dict[str, dict] = {}
    for e in root.findall("list"):
        entry = {
            "corp_code": (e.findtext("corp_code") or "").strip(),
            "corp_name": (e.findtext("corp_name") or "").strip(),
            "stock_code": (e.findtext("stock_code") or "").strip(),
        }
        by_name.setdefault(entry["corp_name"], []).append(entry)
        if entry["stock_code"]:
            by_stock[entry["stock_code"]] = entry
    return by_name, by_stock


def resolve(company: str) -> dict:
    """회사명 또는 6자리 종목코드 → {corp_code, corp_name, stock_code}."""
    q = company.strip()
    by_name, by_stock = _corp_index()
    if q.isdigit() and len(q) == 6:          # 종목코드
        if q in by_stock:
            return by_stock[q]
        raise DataError(f"종목코드 {q} 를 DART 에서 못 찾음")
    if q in by_name:                          # 정확 일치 (상장사 우선)
        cands = by_name[q]
        listed = [c for c in cands if c["stock_code"]]
        return (listed or cands)[0]
    # 부분 일치 (상장사 우선, 최단 이름)
    hits = [e for name, lst in by_name.items() if q in name for e in lst]
    if not hits:
        raise DataError(f"DART 에서 기업을 못 찾음: '{company}'")
    hits.sort(key=lambda e: (not e["stock_code"], len(e["corp_name"])))
    return hits[0]


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


def _statement_rows(corp_code: str, year: int, reprt_code: str) -> tuple[list, str]:
    """연결(CFS) 우선, 없으면 별도(OFS)."""
    try:
        rows, div = _fetch_all(corp_code, year, reprt_code, "CFS")
        if rows:
            return rows, "연결(CFS)"
    except DataError:
        pass
    rows, div = _fetch_all(corp_code, year, reprt_code, "OFS")
    return rows, "별도(OFS)"


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
    subset = [r for r in rows if r.get("sj_div") == sj]
    for r in subset:
        if r.get("account_id") in ids:
            return r
    for r in subset:
        if r.get("account_nm") in names:
            return r
    return None


def financial_item(company: str, item: str, year: int | None = None,
                   report: str = "annual") -> Value:
    """단일 재무 항목 조회. item ∈ ITEM_MAP. 값 단위 KRW."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year or (date.today().year - 1)

    rows, fs_label = _statement_rows(ent["corp_code"], yr, reprt_code)
    row = _match(rows, item)
    if row is None:
        raise DataError(f"{ent['corp_name']} {yr} 에서 '{ITEM_LABEL[item]}' 계정을 못 찾음")
    amt = _to_int(row.get("thstrm_amount"))
    if amt is None:
        raise DataError(f"{ent['corp_name']} {ITEM_LABEL[item]} 금액이 비어있음")

    rcept = row.get("rcept_no", "")
    filing_date = f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}" if len(rcept) >= 8 else None
    return Value(
        value=amt, unit=row.get("currency") or "KRW",
        label=f"{ent['corp_name']} {ITEM_LABEL[item]} ({yr}, {fs_label})",
        provenance=Provenance(
            source="DART (금융감독원)",
            source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
            original_field=f"{row.get('sj_div')}/{row.get('account_id')}/{row.get('account_nm')}",
            as_of=f"FY{yr}", filing_date=filing_date,
            note=f"corp_code={ent['corp_code']}, 종목={ent['stock_code']}",
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

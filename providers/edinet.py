"""EDINET provider (일본 금융청 전자공시) — 기업 매핑 + XBRL(CSV 출력) 재무제표. authoritative.

- EdinetCode 목록: EDINET코드 ↔ 회사명(일/영) ↔ 증권코드 ↔ 결산일 매핑 (주간 갱신 캐시)
- documents.json: 날짜별 제출서류 목록(날짜 단위 조회만 가능) → 결산일 + 통상 제출기한(약 3개월)
  구간을 스캔해 유가증권보고서(docTypeCode=120)를 찾는다.
- documents/{docID}?type=5: 유가증권보고서 CSV(UTF-16, tab) 전체 XBRL 값.
  컬럼: 要素ID(태그) | 項目名 | コンテキストID | 相対年度(当期/前期/...) | 連結・個別 | 期間・時点 | ユニットID | 単位 | 値

실측 확인(2026-08, 도요타자동차 E02144, docID S100Y8NY):
  - IFRS 대형 상장사는 '経営指標等'(SummaryOfBusinessResults, 5개년 하이라이트) 표에
    매출액 항목의 IFRS 버전 자체가 없고, 있는 필드(NetSalesSummaryOfBusinessResults)도
    비연결(개별, 컨텍스트 접미사 _NonConsolidatedMember)만 채워지는 경우가 있다.
    → 연결 실적은 본문 재무제표의 표준 IFRS 태그(jpigp_cor:*IFRS, 접미사 없음=연결)에서 확인.
  - 시점(instant) 값의 相対年度 라벨은 "当期末"(기간값 "当期"와 다름) — 혼동하면 매칭 실패.
  - 재무상태표 표준 태그는 plain("Assets"/"Liabilities"/"Equity")이 아니라
    IFRS 접미사가 붙은 "AssetsIFRS"/"LiabilitiesIFRS"/"EquityIFRS" 형태.
  - 연결 컨텍스트가 전혀 없으면(중소형 JGAAP 등) 개별 기준으로 fallback하되 note에 "개별(비연결)" 명시
    (DART의 CFS→OFS fallback과 동일한 사상: 없는 것보다 라벨링된 근사치가 낫다).
"""
from __future__ import annotations

import calendar
import csv
import io
import re
import zipfile
import difflib
from datetime import date, timedelta
from functools import lru_cache

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_bytes, get_json
from core import config

_BASE = "https://disclosure.edinet-fsa.go.jp/api/v2"
_CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

_ANNUAL_REPORT_DOC_TYPE = "120"  # 有価証券報告書

# item 키 → (kind: duration(기간)|instant(시점), [XBRL 태그 후보, 우선순위순])
ITEM_MAP: dict[str, tuple] = {
    "revenue": ("duration", ["jpigp_cor:RevenueIFRS", "jpigp_cor:Revenue",
                             "jpcrp_cor:NetSalesSummaryOfBusinessResults", "jppfs_cor:NetSales"]),
    "operating_income": ("duration", ["jpigp_cor:OperatingProfitLossIFRS", "jppfs_cor:OperatingIncome"]),
    "net_income": ("duration", ["jpigp_cor:ProfitLossAttributableToOwnersOfParentIFRS",
                                "jpigp_cor:ProfitLossIFRS", "jppfs_cor:ProfitLoss",
                                "jpcrp_cor:NetIncomeLossSummaryOfBusinessResults"]),
    "total_assets": ("instant", ["jpigp_cor:AssetsIFRS", "jppfs_cor:Assets",
                                 "jpcrp_cor:TotalAssetsIFRSSummaryOfBusinessResults",
                                 "jpcrp_cor:TotalAssetsSummaryOfBusinessResults"]),
    "total_liabilities": ("instant", ["jpigp_cor:LiabilitiesIFRS", "jppfs_cor:Liabilities"]),
    "total_equity": ("instant", ["jpigp_cor:EquityIFRS", "jpigp_cor:EquityAttributableToOwnersOfParentIFRS",
                                 "jppfs_cor:NetAssets", "jpcrp_cor:NetAssetsSummaryOfBusinessResults"]),
}
ITEM_LABEL = {
    "revenue": "매출액(수익)", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
}
SHARES_TAGS = ["jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults"]

_RELYEAR_DURATION = ["当期", "前期", "前々期", "三期前", "四期前"]
_RELYEAR_INSTANT = ["当期末", "前期末", "前々期末", "三期前時点", "四期前時点"]
_CTX_RE = re.compile(r"^(Current|Prior[1-4])Year(Duration|Instant)(_NonConsolidatedMember)?$")


def _relyear(kind: str, idx: int) -> str:
    return (_RELYEAR_INSTANT if kind == "instant" else _RELYEAR_DURATION)[idx]


# ── 기업 매핑 ────────────────────────────────────────────────────
def _norm_en(s: str) -> str:
    s = re.sub(r"\b(corporation|corp|co|ltd|inc|company|the|kk|plc|group|holdings?)\b\.?", "", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


@lru_cache(maxsize=1)
def _company_index() -> tuple[dict, dict, dict]:
    """EdinetCode 목록 → (증권코드→entry, 정규화영문명→entry, 일문명→entry)."""
    raw = get_bytes(_CODELIST_URL, ttl_hours=24 * 7)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    text = zf.read("EdinetcodeDlInfo.csv").decode("cp932")
    lines = text.splitlines()[1:]  # 첫 줄은 안내문(헤더 아님)
    rdr = csv.reader(lines)
    header = next(rdr)
    by_seccode: dict[str, dict] = {}
    by_en: dict[str, dict] = {}
    by_ja: dict[str, dict] = {}
    for row in rdr:
        rec = dict(zip(header, row))
        seccode = (rec.get("証券コード") or "").strip()
        name_ja = (rec.get("提出者名") or "").strip()
        name_en = (rec.get("提出者名（英字）") or "").strip()
        entry = {
            "edinet_code": (rec.get("ＥＤＩＮＥＴコード") or "").strip(),
            "name_ja": name_ja, "name_en": name_en,
            "sec_code": seccode,
            "fiscal_year_end": (rec.get("決算日") or "").strip(),  # 예: "3月31日"
        }
        if seccode:
            by_seccode.setdefault(seccode, entry)
            if seccode.endswith("0"):
                by_seccode.setdefault(seccode[:-1], entry)  # 4자리 코드로도 조회 가능
        if name_ja:
            by_ja.setdefault(name_ja, entry)
        norm_en = _norm_en(name_en) if name_en else ""
        if norm_en:  # name_en 이 "-"(미기재) 등이면 norm 이 빈 문자열이 되어
            by_en.setdefault(norm_en, entry)  # "" in qn 이 항상 True 라 오탐 유발 → 제외
    return by_seccode, by_en, by_ja


def resolve(company: str) -> dict:
    """회사명(영/일문) 또는 증권코드(4~5자리) → {edinet_code, name_ja, name_en, sec_code, fiscal_year_end}."""
    q = company.strip()
    by_seccode, by_en, by_ja = _company_index()
    if q.isdigit() and len(q) in (4, 5):
        if q in by_seccode:
            return by_seccode[q]
        raise DataError(f"증권코드 {q} 를 EDINET 에서 못 찾음")
    if q in by_ja:
        return by_ja[q]
    qn = _norm_en(q)
    if qn and qn in by_en:
        return by_en[qn]
    if qn:
        # "norm in qn"(등록명이 입력의 일부) 방향은 norm 이 짧으면 우연히 걸리기 쉬움
        # (예: "PA" ⊂ "nosuchcom*pa*nyxyz") → 최소 길이 요구.
        min_len = max(4, int(len(qn) * 0.5))
        cands = [e for norm, e in by_en.items()
                if qn in norm or (norm in qn and len(norm) >= min_len)]
        if cands:
            return sorted(cands, key=lambda e: len(e["name_en"]))[0]
    close = difflib.get_close_matches(qn, list(by_en.keys()), n=1, cutoff=0.8)
    if close:
        return by_en[close[0]]
    sug = difflib.get_close_matches(qn, list(by_en.keys()), n=5, cutoff=0.55)
    hint = f" 혹시: {', '.join(by_en[s]['name_en'] for s in sug)}" if sug else ""
    raise DataError(f"EDINET 에서 기업을 못 찾음: '{company}'.{hint}")


# ── 서류(유가증권보고서) 탐색 ─────────────────────────────────────
def _parse_fye(fye_str: str) -> tuple[int, int]:
    s = fye_str or ""
    m = re.match(r"(\d{1,2})月末日", s)  # 예: "3月末日" (그 달의 마지막 날)
    if m:
        return int(m.group(1)), 0  # day=0 은 "월말" sentinel
    m = re.match(r"(\d{1,2})月(\d{1,2})日", s)
    if not m:
        raise DataError(f"결산일 형식을 해석 못함: '{fye_str}'")
    return int(m.group(1)), int(m.group(2))


def _fye_date(year: int, month: int, day: int) -> date:
    if day == 0:  # "월말" sentinel → 그 달의 실제 마지막 날
        day = calendar.monthrange(year, month)[1]
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)  # 2/29 등 예외적 결산일 대비


def _scan_for_doc(edinet_code: str, start: date, end: date) -> dict | None:
    """start~end 구간을 최신 날짜부터 하루씩 조회해 유가증권보고서 탐색."""
    key = config.require(config.Keys.EDINET, "EDINET_API_KEY")
    d = end
    while d >= start:
        j = get_json(f"{_BASE}/documents.json", ttl_hours=24 * 30,
                    params={"date": d.isoformat(), "type": 2, "Subscription-Key": key})
        for doc in j.get("results", []) or []:
            if doc.get("edinetCode") == edinet_code and doc.get("docTypeCode") == _ANNUAL_REPORT_DOC_TYPE:
                return doc
        d -= timedelta(days=1)
    return None


def _find_annual_doc(edinet_code: str, fye_month: int, fye_day: int,
                     year: int | None, max_back_years: int = 4) -> tuple[dict, int]:
    """연간 유가증권보고서 탐색. 통상 결산일 이후 40~130일 내 제출됨(도요타 실측: +71일)."""
    today = date.today()
    if year is not None:
        fye = _fye_date(year, fye_month, fye_day)
        doc = _scan_for_doc(edinet_code, fye + timedelta(days=40), fye + timedelta(days=130))
        if doc:
            return doc, year
        raise DataError(f"{year}년 결산({fye.isoformat()}) 유가증권보고서를 찾지 못함")
    for back in range(0, max_back_years + 1):
        fye = _fye_date(today.year - back, fye_month, fye_day)
        if fye > today:
            continue
        doc = _scan_for_doc(edinet_code, fye + timedelta(days=40), fye + timedelta(days=130))
        if doc:
            return doc, fye.year
    raise DataError(f"EDINET코드 {edinet_code}: 최근 {max_back_years}년 내 유가증권보고서를 찾지 못함")


# ── 공시 검색·원문 조회 (fallback: 자체 항목만으로 안 잡히는 계열사·연관회사 조사용) ──
_MAX_SCAN_DAYS = 200  # documents.json 은 날짜 단위 조회만 지원 — 무제한 스캔 방지


def list_filings(company: str, bgn_de: str, end_de: str, doc_type: str | None = None) -> list[dict]:
    """공시목록 검색. bgn_de/end_de: YYYYMMDD. doc_type 지정 시 그 유형만(예: '120'=유가증권
    보고서). EDINET 는 날짜별 조회만 지원해 day-by-day 스캔 — 기간이 너무 넓으면 에러."""
    ent = resolve(company)
    key = config.require(config.Keys.EDINET, "EDINET_API_KEY")
    start = date(int(bgn_de[:4]), int(bgn_de[4:6]), int(bgn_de[6:8]))
    end = date(int(end_de[:4]), int(end_de[4:6]), int(end_de[6:8]))
    if (end - start).days > _MAX_SCAN_DAYS:
        raise DataError(f"조회 기간이 {_MAX_SCAN_DAYS}일을 초과합니다 — 기간을 좁혀주세요.")
    out: list[dict] = []
    d = end
    while d >= start:
        j = get_json(f"{_BASE}/documents.json", ttl_hours=24 * 30,
                    params={"date": d.isoformat(), "type": 2, "Subscription-Key": key})
        for doc in j.get("results", []) or []:
            if doc.get("edinetCode") != ent["edinet_code"]:
                continue
            if doc_type and doc.get("docTypeCode") != doc_type:
                continue
            out.append({
                "docID": doc.get("docID"), "docDescription": doc.get("docDescription"),
                "docTypeCode": doc.get("docTypeCode"), "submitDateTime": doc.get("submitDateTime"),
                "filerName": doc.get("filerName"),
            })
        d -= timedelta(days=1)
    return out


def filing_text(docid: str, keyword: str | None = None, context_chars: int = 200,
                max_chars: int = 8000, max_matches: int = 20) -> dict:
    """공시 원문(CSV type=5)의 서술문(사업의 내용·주석 등 TextBlock) 텍스트에서 검색.
    keyword 지정 시 등장 부분만 앞뒤 context_chars 와 함께 반환(전체 원문 아님) — 특정
    회사명·사업부문 언급을 찾을 때 씀. 미지정 시 앞부분 max_chars 만 반환."""
    rows = _doc_rows(docid)
    text_rows = [r for r in rows if r["val"] is None and len((r["text"] or "").strip()) > 20]
    combined = "\n".join(f"[{r['item_nm']}] {r['text']}" for r in text_rows)

    if keyword:
        # 일문 서술 안에도 영문 회사명이 섞여 표기가 제각각일 수 있어(예: "ESSENCORE" vs
        # "Essencore") 대소문자 무시로 찾고, 발췌는 원문 표기 그대로 보여준다.
        combined_lower, kw_lower = combined.lower(), keyword.lower()
        idxs, start = [], 0
        while True:
            idx = combined_lower.find(kw_lower, start)
            if idx == -1:
                break
            idxs.append(idx)
            start = idx + 1
        excerpts = []
        for idx in idxs[:max_matches]:
            s, e = max(0, idx - context_chars), min(len(combined), idx + len(keyword) + context_chars)
            excerpts.append(combined[s:e].replace("\n", " "))
        return {
            "docid": docid, "total_chars": len(combined), "keyword": keyword,
            "matches": len(idxs), "excerpts": excerpts,
            "note": None if idxs else "키워드가 원문 서술문에 없음(정확한 표기를 다시 확인하거나 다른 계열사를 시도하라)",
        }

    truncated = len(combined) > max_chars
    return {
        "docid": docid, "total_chars": len(combined), "text": combined[:max_chars],
        "truncated": truncated,
        "note": ("문서가 길어 앞부분만 반환됨. 특정 회사명/키워드를 찾으려면 keyword 인자를 지정하라."
                if truncated else None),
    }


# ── CSV(type=5) 파싱 ──────────────────────────────────────────────
# 실측 확인: 표시용 "単位" 컬럼은 주식수 항목에서 공란이고, 실제 단위는
# "ユニットID"(unit_id, 예: "JPY"/"shares")에 들어있다 — unit_id 기준으로 판정.
def _parse_val(s: str, unit_id: str) -> int | None:
    s = (s or "").strip()
    if unit_id not in ("JPY", "shares"):
        return None
    neg = s.startswith("△") or s.startswith("-")
    s2 = s.lstrip("△-").replace(",", "")
    if not s2.isdigit():
        return None
    v = int(s2)
    return -v if neg else v


@lru_cache(maxsize=64)
def _doc_rows(docid: str) -> tuple[dict, ...]:
    """유가증권보고서 CSV(type=5) 전체를 파싱해 (tag, item_nm, ctx, relyear, unit_id, val, text)
    행 목록으로. text 는 원본 문자열 그대로 — 숫자 계정은 val 로 파싱되지만, 서술문
    (TextBlock 태그, 예: '사업의 내용'·주석 등)은 val=None 이라도 text 에 실제 문장이 남아있어
    filing_text() 의 원문 검색 대상이 된다(실측 확인: Sony 기준 TextBlock 태그 185개)."""
    key = config.require(config.Keys.EDINET, "EDINET_API_KEY")
    raw = get_bytes(f"{_BASE}/documents/{docid}", ttl_hours=24 * 30,
                    params={"type": 5, "Subscription-Key": key})
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rows = []
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        raw_csv = zf.read(name)
        enc = "utf-16" if raw_csv[:2] in (b"\xff\xfe", b"\xfe\xff") else "cp932"
        text = raw_csv.decode(enc, errors="replace")
        for i, row in enumerate(csv.reader(io.StringIO(text), delimiter="\t")):
            if i == 0 or len(row) < 9:
                continue
            tag, item_nm, ctx, relyear, consol, kind, unit_id, unit, val = row[:9]
            rows.append({"tag": tag, "item_nm": item_nm, "ctx": ctx, "relyear": relyear,
                        "unit_id": unit_id, "val": _parse_val(val, unit_id), "text": val})
    return tuple(rows)


def _find_value(rows: tuple, tags: list[str], kind: str, idx: int = 0):
    """tags 우선순위대로 탐색. 연결(컨텍스트에 _NonConsolidatedMember 없음) 우선,
    없으면 개별로 fallback. 반환: (row, tag, '연결'|'개별(비연결)') 또는 (None, None, None)."""
    label = _relyear(kind, idx)
    matched = [r for tag in tags for r in rows
              if r["tag"] == tag and r["relyear"] == label and _CTX_RE.match(r["ctx"])]
    for tag in tags:
        cons = [r for r in matched if r["tag"] == tag and not r["ctx"].endswith("_NonConsolidatedMember")]
        if cons:
            return cons[0], tag, "연결"
    for tag in tags:
        indiv = [r for r in matched if r["tag"] == tag and r["ctx"].endswith("_NonConsolidatedMember")]
        if indiv:
            return indiv[0], tag, "개별(비연결)"
    return None, None, None


def financial_item(company: str, item: str, year: int | None = None) -> Value:
    """단일 재무 항목(연간, 유가증권보고서 기준). item ∈ ITEM_MAP. 값 단위 JPY."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    kind, tags = ITEM_MAP[item]
    ent = resolve(company)
    fye_m, fye_d = _parse_fye(ent["fiscal_year_end"])
    doc, fy = _find_annual_doc(ent["edinet_code"], fye_m, fye_d, year)
    rows = _doc_rows(doc["docID"])
    row, tag, basis = _find_value(rows, tags, kind)
    if row is None or row["val"] is None:
        raise DataError(f"{ent['name_en'] or ent['name_ja']} FY{fy} 에서 "
                        f"'{ITEM_LABEL[item]}' 값을 못 찾음 (시도한 태그: {tags})")
    name = ent["name_en"] or ent["name_ja"]
    filing_date = (doc.get("submitDateTime") or "").split(" ")[0] or None
    return Value(
        value=row["val"], unit="JPY",
        label=f"{name} {ITEM_LABEL[item]} (FY{fy}, {basis})",
        provenance=Provenance(
            source="EDINET (일본 금융청)", source_type=SourceType.AUTHORITATIVE,
            source_url=f"{_BASE}/documents/{doc['docID']}",
            original_field=f"XBRL要素ID: {tag}",
            as_of=f"FY{fy}", filing_date=filing_date,
            note=f"EDINETコード={ent['edinet_code']}, docID={doc['docID']}, 기준={basis}",
        ),
    )


def financial_item_multiyear(company: str, item: str, year: int | None = None) -> dict:
    """엔진용: 최근 3개년(당기/전기/전전기) 시리즈."""
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}")
    kind, tags = ITEM_MAP[item]
    ent = resolve(company)
    fye_m, fye_d = _parse_fye(ent["fiscal_year_end"])
    doc, fy = _find_annual_doc(ent["edinet_code"], fye_m, fye_d, year)
    rows = _doc_rows(doc["docID"])
    series = []
    basis_used = None
    for i in range(3):
        row, tag, basis = _find_value(rows, tags, kind, i)
        if row is None or row["val"] is None:
            continue
        series.append({"period": f"FY{fy - i}", "amount": row["val"]})
        basis_used = basis_used or basis
    if not series:
        raise DataError(f"{ent['name_en'] or ent['name_ja']}: '{ITEM_LABEL[item]}' 연간 데이터를 못 찾음")
    filing_date = (doc.get("submitDateTime") or "").split(" ")[0] or None
    return {"corp_name": ent["name_en"] or ent["name_ja"], "edinet_code": ent["edinet_code"],
            "docid": doc["docID"], "filing_date": filing_date, "basis": basis_used,
            "item": item, "series": series}


def shares_outstanding(company: str, year: int | None = None) -> Value:
    """발행주식총수. 단위 '株'."""
    ent = resolve(company)
    fye_m, fye_d = _parse_fye(ent["fiscal_year_end"])
    doc, fy = _find_annual_doc(ent["edinet_code"], fye_m, fye_d, year)
    rows = _doc_rows(doc["docID"])
    row, tag, basis = _find_value(rows, SHARES_TAGS, "instant")
    if row is None or row["val"] is None:
        raise DataError(f"{ent['name_en'] or ent['name_ja']}: 발행주식총수를 못 찾음")
    name = ent["name_en"] or ent["name_ja"]
    filing_date = (doc.get("submitDateTime") or "").split(" ")[0] or None
    return Value(
        value=row["val"], unit="株", label=f"{name} 발행주식총수 (FY{fy})",
        provenance=Provenance(
            source="EDINET (일본 금융청)", source_type=SourceType.AUTHORITATIVE,
            source_url=f"{_BASE}/documents/{doc['docID']}",
            original_field=f"XBRL要素ID: {tag}",
            as_of=f"FY{fy}", filing_date=filing_date,
            note=f"EDINETコード={ent['edinet_code']}, docID={doc['docID']}",
        ),
    )

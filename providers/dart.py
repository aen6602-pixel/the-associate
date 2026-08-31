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
from core.cache import TTL_FRESH, TTL_INDEX, ttl_cache
from datetime import date

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_bytes, get_json
from core import config

_BASE = "https://opendart.fss.or.kr/api"

# 사업보고서 유형
# ⚠️ q1 은 11013 이다. 예전에 13013 으로 잘못 적혀 있었는데, DART 는 알 수 없는 reprt_code 를
# 오류로 돌려주지 않고 **사업보고서(연간)를 그대로 반환**해서 조용히 틀렸다(실측 2026-08-27,
# 삼성전자 2025: 13013 → thstrm_nm='제 57 기' 영업이익 43.6조=연간, 11013 → '제 57 기 1분기'
# 6.69조). report='q1' 로 조회한 모든 값이 연간값이었다는 뜻이다.
REPRT = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}

# 누적기간 조회 순서(최근 분기 우선) — LTM 계산에서 "가장 늦은 분기 누적" 을 찾는 데 쓴다.
_QUARTER_ORDER = (("q3", "11014", 9), ("half", "11012", 6), ("q1", "11013", 3))

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
    # 운전자본 3항목 — 현금흐름표에 '자산부채의 변동' 합계가 없는 회사(SK하이닉스·네이버 실측)의
    # ΔNWC 를 재무상태표 증감으로 직접 계산하기 위해 필요하다.
    "trade_receivables": ("BS", ["ifrs-full_TradeAndOtherCurrentReceivables",
                                  "ifrs-full_CurrentTradeReceivables"],
                          ["매출채권", "매출채권 및 기타채권", "매출채권및기타채권"]),
    "inventories": ("BS", ["ifrs-full_Inventories"], ["재고자산"]),
    "trade_payables": ("BS", ["ifrs-full_TradeAndOtherCurrentPayables",
                              "ifrs-full_CurrentTradePayables"],
                       ["매입채무", "매입채무 및 기타채무", "매입채무및기타채무"]),
}
ITEM_LABEL = {
    "revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계",
    "cogs": "매출원가", "sga": "판매비와관리비", "interest_expense": "이자비용(금융비용)",
    "tax_expense": "법인세비용", "ppe": "유형자산", "cash": "현금및현금성자산",
    "trade_receivables": "매출채권", "inventories": "재고자산", "trade_payables": "매입채무",
}

# ── 현금흐름표 계정 태그 (실측 확인: 2026-08, 삼성전자·오리온 FY2025 연결) ─────────
# 계정명(account_nm)은 회사마다 표현이 달라도 XBRL account_id 는 표준이라 태그를 우선한다.
#
# D&A: 이름으로 '상각비' 를 긁으면 **대손상각비**(bad debt)까지 섞여 들어간다(오리온 실측:
# '대손상각비 조정' 5.9억이 D&A 에 가산됨) → 태그 우선, 이름 폴백 시 대손·손상 제외.
_DA_TAGS = (
    "ifrs-full_AdjustmentsForDepreciationExpense",
    "ifrs-full_AdjustmentsForAmortisationExpense",
    "ifrs-full_AdjustmentsForDepreciationAndAmortisationExpense",
    "dart_AdjustmentsForDepreciationRightofuseAssets",
    "dart_AdjustmentsForDepreciationInvestmentProperty",
    "dart_AdjustmentsForAmortisationOfIntangibleAssets",
)
_DA_NAME_INCLUDE = ("감가상각", "상각비")
_DA_NAME_EXCLUDE = ("대손", "손상")  # 대손상각비·손상차손은 D&A 가 아니다

# 이자: 손익 '금융비용'(ifrs-full_FinanceCosts)은 환차손·파생손실까지 포함해 Kd 산정에 쓸 수
# 없다(삼성전자 FY2025 실측: 금융비용 11.7조 / 차입금 24.1조 → Kd 48.8% 라는 비상식적 값).
# 이자 전용 계정을 쓴다 — 발생주의(이자비용 조정) 우선, 없으면 현금주의(이자의 지급).
_INTEREST_ACCRUAL_TAGS = ("dart_AdjustmentsForInterestExpenses",
                          "ifrs-full_AdjustmentsForInterestExpense")
_INTEREST_PAID_TAGS = ("ifrs-full_InterestPaidClassifiedAsOperatingActivities",
                       "ifrs-full_InterestPaid",
                       "ifrs-full_InterestPaidClassifiedAsFinancingActivities")
_CAPEX_TANGIBLE_TAGS = (
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",)
_CAPEX_INTANGIBLE_TAGS = (
    "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",)


def _da_rows(cf_rows: list) -> list:
    """CF 에서 D&A 로 인정할 행들. 태그 매칭 우선, 없으면 이름 매칭(대손·손상 제외)."""
    tagged = [r for r in cf_rows if r.get("account_id") in _DA_TAGS]
    if tagged:
        return tagged
    out = []
    for r in cf_rows:
        nm = r.get("account_nm") or ""
        if any(x in nm for x in _DA_NAME_INCLUDE) and not any(x in nm for x in _DA_NAME_EXCLUDE):
            out.append(r)
    return out


# ── 기업 매핑 ────────────────────────────────────────────────────
# 영문 약칭 ↔ 한글 등록명 정규화 (DART 는 한글 등록명 사용: SK→에스케이 등)
_PREFIX_ALIAS = {
    "에스케이": "sk", "엘지": "lg", "지에스": "gs", "케이티앤지": "ktng", "케이티": "kt",
    "씨제이": "cj", "포스코": "posco", "엘에스": "ls", "케이비": "kb", "엔에이치": "nh",
    "에이치디": "hd", "디엘": "dl", "오씨아이": "oci", "에스디": "sd", "제이비": "jb",
    "네이버": "naver", "카카오": "kakao",  # 영문 등록명 매칭용
    "현대차": "현대자동차", "기아차": "기아",  # 구어 약칭 확장
}


# ── 입력 식별자 전처리 ────────────────────────────────────────────
# 실측 실패(2026-08): `현대자동차(005380)` 이라는 지극히 평범한 입력이 못 찾음으로 떨어졌다.
# 원인은 _norm_name 이 **괄호 기호만 지우고 안의 숫자는 남겨서** "현대자동차005380" 이라는
# 토큰이 만들어진 것이다. 그러면
#   · 부분일치 방향1(등록명 ⊂ 입력)의 길이 게이트 len(norm) >= int(len(qn)*0.6)=9 에 걸리고
#     (len("현대자동차")=5 < 9)
#   · fuzzy cutoff 0.78 도 유사도 0.5 로 미달
# 두 게이트에 동시에 막혔다. 괄호 안 내용을 **통째로 분리**하면 둘 다 통과한다.
_PAREN_RE = re.compile(r"[（(\[]\s*([^）)\]]*?)\s*[）)\]]")
_TICKER_SUFFIX_RE = re.compile(r"\.(ks|kq|kospi|kosdaq)\s*$", re.I)
_SIX_DIGITS_RE = re.compile(r"^\d{6}$")


def parse_identifier(company: str) -> tuple[str, str | None]:
    """'현대자동차(005380)' → ('현대자동차', '005380'), '005380.KS' → ('', '005380').

    돌려주는 종목코드는 **가장 강한 신호**라서 resolve() 가 이름보다 먼저 쓴다.
    괄호 안이 종목코드가 아니어도(예: '삼성전자(반도체)') 이름에서 떼어내는 것이 맞다 —
    등록명에는 없는 수식어이기 때문이다.
    """
    q = (company or "").strip()
    code = None
    for m in _PAREN_RE.finditer(q):
        inner = (m.group(1) or "").strip()
        inner_digits = _TICKER_SUFFIX_RE.sub("", inner).strip()
        if _SIX_DIGITS_RE.match(inner_digits):
            code = inner_digits
    q = _PAREN_RE.sub(" ", q).strip()
    q = _TICKER_SUFFIX_RE.sub("", q).strip()
    if _SIX_DIGITS_RE.match(q):
        code, q = q, ""
    return q, code


def _norm_name(s: str) -> str:
    """비교용 정규화: 소문자, 법인격/공백/기호 제거, 한글약칭→영문."""
    s = str(s).lower().strip()
    s = re.sub(r"주식회사|\(주\)|㈜|\(유\)|holdings", "", s)
    for ko, en in _PREFIX_ALIAS.items():
        s = s.replace(ko, en)
    s = re.sub(r"[\s.,\-·'&()]", "", s)
    return s


@ttl_cache(TTL_INDEX, maxsize=1)
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
            "corp_eng_name": (e.findtext("corp_eng_name") or "").strip(),
            "stock_code": (e.findtext("stock_code") or "").strip(),
        }
        by_name.setdefault(entry["corp_name"], []).append(entry)
        if entry["stock_code"]:
            by_stock[entry["stock_code"]] = entry
        by_norm.setdefault(_norm_name(entry["corp_name"]), []).append(entry)
        # corpCode.xml 에는 영문 등록명도 있다 — "Hyundai Motor" 처럼 영문으로 물어오는 입력을
        # 예전에는 전혀 못 받았다(한글 색인만 만들어서). 같은 정규화 함수를 태워 같이 넣는다.
        eng_norm = _norm_name(entry["corp_eng_name"])
        if eng_norm and eng_norm != _norm_name(entry["corp_name"]):
            by_norm.setdefault(eng_norm, []).append(entry)
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
    """회사명(대충 입력 허용) 또는 6자리 종목코드 → {corp_code, corp_name, stock_code}.

    허용 입력(전부 동일 법인으로 해석): '현대자동차', '현대차', '005380', '005380.KS',
    '현대자동차(005380)', 'Hyundai Motor'.
    """
    raw = (company or "").strip()
    by_name, by_stock, by_norm = _corp_index()
    if raw in by_name:                    # 원문이 등록명과 정확히 같으면 전처리 없이 채택
        return _best(by_name[raw])
    q, code = parse_identifier(raw)
    if code:                              # 종목코드가 가장 강한 신호 → 이름보다 먼저
        if code in by_stock:
            return by_stock[code]
        if not q:
            raise DataError(f"종목코드 {code} 를 DART 에서 못 찾음")
        # 코드가 안 맞아도 이름이 남아 있으면 이름으로 계속 시도한다(오타 가능성).
    if not q:
        raise DataError(f"조회할 회사명이나 종목코드를 찾지 못했습니다: {company!r}")
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
    # ⚠️ 빈 응답(status != 000)은 1시간만 캐시한다 — 아직 접수 안 된 사업연도를 물으면
    # DART 가 013 을 주는데, 그것을 길게 캐시하면 보고서가 올라온 뒤에도 '최신 연도' 판정이
    # 그만큼 늦어진다(리노공업 FY2025 는 2026-03-18 접수).
    j = get_json(f"{_BASE}/fnlttSinglAcntAll.json", ttl_hours=24, params={
        "crtfc_key": key, "corp_code": corp_code, "bsns_year": str(year),
        "reprt_code": reprt_code, "fs_div": fs_div,
    }, is_empty=lambda d: d.get("status") != "000")
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


# ⚠️ **여기에 TTL 이 없으면 새 사업보고서를 영원히 못 본다.** lru_cache 는 프로세스가
# 사는 동안 만료되지 않아서, 배포 서버가 3월 이전에 '최신=2024' 를 한 번 계산해두면
# 재시작 전까지 계속 2024 를 돌려준다(리노공업 FY2022~2024 사고의 직접 원인).
@ttl_cache(TTL_FRESH, maxsize=512)
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


def _cum_amount(row: dict) -> int | None:
    """분기보고서 행의 **누적(YTD) 금액**. thstrm_add_amount 가 누적, thstrm_amount 는
    당분기 3개월 단독이다(실측 2026-08-27, 삼성전자 제57기 3분기 영업이익:
    thstrm=12.17조(3개월) / add=23.53조(9개월 누적)). 1분기는 둘이 같다."""
    return _to_int(row.get("thstrm_add_amount")) or _to_int(row.get("thstrm_amount"))


def _latest_quarter(corp_code: str, year: int, prefer: str) -> tuple[str, str, int] | None:
    """해당 사업연도에 존재하는 가장 늦은 분기보고서 → (report, reprt_code, 누적개월)."""
    for report, reprt_code, months in _QUARTER_ORDER:
        try:
            rows, _ = _statement_rows(corp_code, year, reprt_code, prefer)
        except DataError:
            continue
        if rows:
            return report, reprt_code, months
    return None


def ltm_item(company: str, item: str, prefer: str = "CFS") -> Value:
    """LTM(최근 12개월) 손익 항목.

    LTM = 직전 연간 + 당해 누적(최신 분기) − 전년 동기 누적.
    분기보고서가 아직 없으면 연간값을 그대로 쓰고 그 사실을 label·note 에 남긴다
    (조용히 연간을 LTM 이라고 부르지 않는다).
    """
    if item not in ITEM_MAP:
        raise DataError(f"지원하지 않는 항목: {item}. 지원: {list(ITEM_MAP)}")
    sj = ITEM_MAP[item][0]
    if not (isinstance(sj, tuple) and "IS" in sj):
        raise DataError(f"'{ITEM_LABEL[item]}' 은 시점(stock) 항목이라 LTM 개념이 없습니다 "
                        f"— 최신 시점값(financial_item)을 쓰세요.")
    ent = resolve(company)
    cc = ent["corp_code"]
    fy = _latest_year(cc, REPRT["annual"], prefer)
    annual = financial_item(company, item, fy, "annual", prefer)

    q = _latest_quarter(cc, fy + 1, prefer)
    if q is None:
        return Value(
            annual.value, annual.unit,
            label=f"{ent['corp_name']} {ITEM_LABEL[item]} (FY{fy} 연간 — LTM 아님)",
            provenance=Provenance(
                source=annual.provenance.source, source_type=annual.provenance.source_type,
                source_url=annual.provenance.source_url,
                original_field=annual.provenance.original_field, as_of=f"FY{fy}",
                filing_date=annual.provenance.filing_date,
                note=(f"FY{fy+1} 분기보고서가 아직 없어 LTM 을 만들 수 없습니다 → FY{fy} 연간값. "
                      f"기준기간이 LTM 이 아니라 연간이라는 점을 비교표에 표시해야 합니다."),
            ),
        )

    report, reprt_code, months = q
    cur_rows, fs_label = _statement_rows(cc, fy + 1, reprt_code, prefer)
    cur_row = _match(cur_rows, item)
    prev_rows, _ = _statement_rows(cc, fy, reprt_code, prefer)
    prev_row = _match(prev_rows, item)
    cur = _cum_amount(cur_row) if cur_row else None
    prev = _cum_amount(prev_row) if prev_row else None
    if cur is None or prev is None:
        raise DataError(
            f"{ent['corp_name']} LTM 계산 실패: {fy+1}년 {report} 누적"
            f"{'(없음)' if cur is None else f'={cur:,}'} / {fy}년 동기 누적"
            f"{'(없음)' if prev is None else f'={prev:,}'} — 분기 비교가 성립하지 않습니다.")

    ltm = annual.value + cur - prev
    rcept = (cur_row or {}).get("rcept_no", "")
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    return Value(
        ltm, annual.unit,
        label=f"{ent['corp_name']} {ITEM_LABEL[item]} (LTM, {fy+1} {report} 기준)",
        provenance=Provenance(
            source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
            original_field=f"연간 + {months}개월 누적 − 전년 동기 누적",
            as_of=f"LTM~{fy+1} {report}", filing_date=_filing_date(rcept),
            note=(f"LTM = FY{fy} 연간 {f(annual.value)} + FY{fy+1} {months}개월 누적 "
                  f"{f(cur)} − FY{fy} {months}개월 누적 {f(prev)} = {f(ltm)}. {fs_label}."),
        ),
        extras={"annual": annual},
    )


def ltm_da(company: str, prefer: str = "CFS") -> Value:
    """LTM D&A. 현금흐름표에 D&A 가 분리 공시되면 LTM 으로, 아니면 **연간값으로 폴백**하고
    그 사실을 note 에 남긴다.

    실측 2026-08-27: 삼성전자는 연결 CF 에 감가상각 계정이 따로 없고('조정' 한 줄에 뭉쳐 있음)
    성격별 분류 주석에서만 D&A 가 나온다 → 분기 주석까지 파싱하지는 않으므로 연간 D&A 를
    쓰게 된다. 이때 EBITDA 의 분모 기간이 EBIT(LTM)과 어긋나므로 반드시 표에 표시해야 한다.
    """
    ent = resolve(company)
    cc = ent["corp_code"]
    fy = _latest_year(cc, REPRT["annual"], prefer)
    annual = da_best(company, fy, "annual", prefer)

    q = _latest_quarter(cc, fy + 1, prefer)
    if q is not None:
        report, reprt_code, months = q
        try:
            cur = _cf_da_cumulative(cc, fy + 1, reprt_code, prefer)
            prev = _cf_da_cumulative(cc, fy, reprt_code, prefer)
        except DataError:
            cur = prev = None
        if cur is not None and prev is not None:
            ltm = annual.value + cur - prev
            f = lambda x: f"{x:,.0f}"  # noqa: E731
            return Value(
                ltm, "KRW", label=f"{ent['corp_name']} D&A (LTM, {fy+1} {report} 기준)",
                provenance=Provenance(
                    source="DART (금융감독원) 현금흐름표", source_type=SourceType.AUTHORITATIVE,
                    source_url=annual.provenance.source_url,
                    original_field="CF 감가상각·무형상각 누적", as_of=f"LTM~{fy+1} {report}",
                    note=(f"LTM D&A = FY{fy} {f(annual.value)} + FY{fy+1} {months}개월 "
                          f"{f(cur)} − FY{fy} {months}개월 {f(prev)} = {f(ltm)}"),
                ),
                extras={"annual": annual},
            )
    return Value(
        annual.value, "KRW",
        label=f"{ent['corp_name']} D&A (FY{fy} 연간 — LTM 아님)",
        provenance=Provenance(
            source=annual.provenance.source, source_type=annual.provenance.source_type,
            source_url=annual.provenance.source_url,
            original_field=annual.provenance.original_field, as_of=f"FY{fy}",
            note=("분기 D&A 를 공시에서 분리할 수 없어 연간값을 사용했습니다 "
                  "→ EBIT(LTM)과 기간이 어긋나므로 EBITDA 기준을 표에 명시해야 합니다. "
                  f"원 note: {annual.provenance.note}"),
        ),
    )


def _cf_da_cumulative(corp_code: str, year: int, reprt_code: str, prefer: str) -> int | None:
    """분기보고서 CF 의 D&A 누적금액. 분리 공시가 없으면 None."""
    rows, _ = _statement_rows(corp_code, year, reprt_code, prefer)
    cf_rows = [r for r in rows if r.get("sj_div") == "CF"]
    da = _da_rows(cf_rows)
    if not da:
        return None
    total = 0
    for r in da:
        amt = _cum_amount(r)
        if amt is None:
            return None
        total += amt
    return total


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

    series = [{"year": y, "period": f"FY{y}", "amount": amounts.get(y)}
              for y in years_needed]
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
        rows, list(_CAPEX_TANGIBLE_TAGS), all_of=["유형자산의취득"]))
    capex_intangible = _series(lambda rows: _match_row(
        rows, list(_CAPEX_INTANGIBLE_TAGS), all_of=["무형자산의취득"]))
    ocf = _series(lambda rows: _match_row(
        rows, ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], all_of=["영업활동", "현금흐름"]))
    nwc = _series(lambda rows: _match_row(
        rows, ["dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"],
        any_of=["운전자본", "자산부채"]))
    interest = _series(lambda rows: _match_row(rows, list(_INTEREST_ACCRUAL_TAGS))
                       or _match_row(rows, list(_INTEREST_PAID_TAGS), any_of=["이자의지급", "이자지급"]))

    # D&A 는 회사에 따라 여러 행(감가상각비/무형자산상각비/사용권자산)으로 쪼개져 있어 합산한다.
    # 태그 기반(_da_rows)이라 대손상각비는 섞이지 않는다. 삼성전자처럼 '조정' 한 줄에 뭉쳐
    # 공시하는 회사는 아무 행도 안 잡혀 None → 가정 필요(조용히 0 으로 채우지 않는다).
    da_out: dict[int, int] = {}
    for anchor_yr, cf_rows in anchors.items():
        rows_da = _da_rows(cf_rows)
        for amt_key, offset in (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2)):
            y = anchor_yr - offset
            if y not in years_needed or y in da_out:
                continue
            total, any_found = 0, False
            for r in rows_da:
                a = _to_int(r.get(amt_key))
                if a is not None:
                    total, any_found = total + a, True
            if any_found:
                da_out[y] = total
    da = [{"year": y, "amount": da_out.get(y)} for y in years_needed]

    return {"corp_name": ent["corp_name"], "capex": capex,
            "capex_intangible": capex_intangible, "ocf": ocf,
            "nwc_change": nwc, "da": da, "interest": interest}


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
    lease_amt, lease_rcept = 0, None
    for r in bs_rows:
        nm = _norm_label(r.get("account_nm") or "")
        amt = _to_int(r.get("thstrm_amount"))
        if amt is None:
            continue
        # 리스부채(IFRS 16)는 이자부담 부채지만 '차입금/사채' 라는 이름이 아니라 따로 모은다.
        # 기존 호출부 호환을 위해 short_term/long_term 합계에는 넣지 않고 별도 키로 돌려준다.
        if "리스부채" in nm:
            lease_amt += amt
            lease_rcept = lease_rcept or r.get("rcept_no")
            continue
        if "차입금" not in nm and "사채" not in nm:
            continue
        is_short = ("단기" in nm) or ("유동성" in nm) or ("유동" in nm and "비유동" not in nm)
        if is_short:
            st_amt += amt
            st_rcept = st_rcept or r.get("rcept_no")
        else:
            lt_amt += amt
            lt_rcept = lt_rcept or r.get("rcept_no")
    rcept = st_rcept or lt_rcept or lease_rcept

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
            "long_term": _v(lt_amt, "장기차입금"),
            "lease": _v(lease_amt, "리스부채(유동+비유동)")}


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
        list(_CAPEX_TANGIBLE_TAGS), all_of=["유형자산의취득"])
    capex_int, capex_int_rcept = _by_tag_or_name(
        list(_CAPEX_INTANGIBLE_TAGS), all_of=["무형자산의취득"])
    ocf, ocf_rcept = _by_tag_or_name(
        ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], all_of=["영업활동", "현금흐름"])
    nwc, nwc_rcept = _by_tag_or_name(
        ["dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"],
        any_of=["운전자본", "자산부채"])

    # 이자: 발생주의(이자비용 조정) 우선, 없으면 현금주의(이자의 지급). 어느 쪽을 썼는지 남긴다.
    interest, interest_rcept = _by_tag_or_name(list(_INTEREST_ACCRUAL_TAGS))
    interest_basis = "발생주의(CF 이자비용 조정)"
    if interest is None:
        interest, interest_rcept = _by_tag_or_name(list(_INTEREST_PAID_TAGS),
                                                   any_of=["이자의지급", "이자지급"])
        interest_basis = "현금주의(CF 이자의 지급)"

    da_total, da_rcept = None, None
    for r in _da_rows(cf_rows):
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
        "capex_intangible": _v(capex_int, "무형자산의 취득"),
        "ocf": _v(ocf, "영업활동현금흐름"),
        "nwc_change": _v(nwc, "영업활동으로 인한 자산부채의 변동"),
        "da": _v(da_total, "감가상각비·무형자산상각비(CF 조정 합산)",
                note=f"D&A 표준계정 태그 행 합산(대손상각비 제외). corp_code={ent['corp_code']}"),
        "interest": _v(interest, f"이자비용 [{interest_basis}]",
                      note=f"{interest_basis}. 손익 '금융비용'은 환차손 등을 포함해 Kd 산정에 "
                           f"부적합하므로 이자 전용 계정을 사용. corp_code={ent['corp_code']}"),
    }


# ── D&A 주석 파싱 (CF 에 D&A 가 없는 회사용) ─────────────────────────
# 삼성전자·SK하이닉스처럼 현금흐름표에서 D&A 를 '조정' 한 줄에 뭉쳐 공시하는 회사는 CF 로
# D&A 를 분리할 수 없다. 대신 IFRS 가 요구하는 **비용의 성격별 분류** 주석에 총 D&A 가 나온다
# (실측 확인 2026-08: SK하이닉스 FY2025 '감가상각비 및 무형자산 상각 10,558,323' 백만원).
_DA_NOTE_LABELS = (
    "감가상각비 및 무형자산 상각",
    "감가상각비 및 무형자산상각",
    "감가상각비및무형자산상각",
    "감가상각비와 무형자산상각",
    "감가상각비 및 무형자산 상각비",
    "감가상각비 및 상각비",
)
_UNIT_MULT = {"백만원": 10 ** 6, "십억원": 10 ** 9, "억원": 10 ** 8,
              "천원": 10 ** 3, "원": 1}
_UNIT_RE = re.compile(r"단위\s*[:：]\s*([가-힣]+원)")
_NUM_RE = re.compile(r"\(?\s*(\d[\d,]{2,})\s*\)?")


_DA_SECTION_ANCHORS = ("비용의 성격별 분류", "영업비용의 내역", "영업비용")
# 성격별 분류표에서 D&A 로 합산할 구성요소. 각 그룹에서 **처음 발견된 라벨 하나**만 취해
# 합산한다(같은 개념의 다른 표기를 이중계상하지 않도록).
# 사용권자산상각비(IFRS 16 리스)는 D&A 에 포함한다 — 현금흐름표 경로(_DA_TAGS)도
# dart_AdjustmentsForDepreciationRightofuseAssets 를 포함하므로 두 경로가 일관된다.
_DA_COMPONENT_GROUPS = (
    ("감가상각비", "감가상각"),
    ("무형자산상각비", "무형자산 상각비", "무형자산상각"),
    ("사용권자산상각비", "사용권자산 상각비", "사용권자산감가상각비"),
)


def _amount_after(text: str, label: str, unit_mult: int) -> int | None:
    """표에서 label 바로 뒤 첫 숫자(=당기 열)를 읽는다."""
    pos = text.find(label)
    if pos < 0:
        return None
    m = _NUM_RE.search(text[pos + len(label): pos + len(label) + 60])
    return int(m.group(1).replace(",", "")) * unit_mult if m else None


def da_from_notes(company: str, year: int | None = None, report: str = "annual",
                  prefer: str = "CFS") -> Value:
    """비용의 성격별 분류(영업비용) 주석에서 총 D&A 를 뽑는다.

    현금흐름표에 D&A 가 분리돼 있으면 `cf_extras` 가 낫다 — 이건 그게 없을 때의 보완 경로다.
    회사마다 표기가 둘로 갈린다(실측 2026-08):
      · 합산 1줄: SK하이닉스 '감가상각비 및 무형자산 상각' 10,558,323백만원
      · 분리 2줄: 네이버 '감가상각비' 135,492,430천원 + '무형자산상각비'
    '감가상각비' 라는 말은 유형자산 증감표·기능별 배분표에도 나오므로 **성격별 분류 섹션을
    앵커로 잡고 그 뒤 구간에서만** 읽는다. 마지막으로 **매출 대비 0.3~40%** 를 벗어나면 다른
    표를 잘못 읽은 것으로 보고 채택하지 않는다(조용히 틀린 값을 내지 않는다)."""
    reprt_code = REPRT.get(report, "11011")
    ent = resolve(company)
    yr = year if year is not None else _latest_year(ent["corp_code"], reprt_code, prefer)
    rows, _ = _statement_rows(ent["corp_code"], yr, reprt_code, prefer)
    rcept = next((r.get("rcept_no") for r in rows if r.get("rcept_no")), None)
    if not rcept:
        raise DataError(f"{ent['corp_name']} {yr} 보고서 접수번호를 찾지 못했습니다.")

    rev = financial_item(company, "revenue", yr, report, prefer).value

    def _accept(amount: int, how: str, excerpt: str, unit_name: str) -> Value | None:
        ratio = amount / rev if rev else 0
        if not 0.003 <= ratio <= 0.40:
            return None
        return Value(
            value=amount, unit="KRW",
            label=f"{ent['corp_name']} 감가상각비+무형자산상각비 ({yr})",
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field=f"주석/비용의 성격별 분류/{how}", as_of=f"FY{yr}",
                filing_date=_filing_date(rcept),
                note=(f"현금흐름표에 D&A 가 분리돼 있지 않아 주석에서 추출({how}, 단위 "
                      f"{unit_name}, 매출 대비 {ratio * 100:.1f}%). 발췌: …{excerpt[:160]}…"),
            ),
        )

    # (1) 합산 1줄 표기
    for label in _DA_NOTE_LABELS:
        try:
            res = filing_text(rcept, keyword=label, context_chars=1500, max_matches=8)
        except DataError:
            continue
        for ex in res.get("excerpts", []):
            pos = ex.find(label)
            if pos < 0:
                continue
            units = _UNIT_RE.findall(ex[:pos])
            if not units:
                continue
            amt = _amount_after(ex[pos:], label, _UNIT_MULT.get(units[-1], 0))
            if not amt:
                continue
            v = _accept(amt, label, ex[max(0, pos - 60):pos + 140], units[-1])
            if v is not None:
                return v

    # (2) 성격별 분류 섹션 앵커 → 그 구간에서 감가상각비 (+ 무형자산상각비) 합산
    for anchor in _DA_SECTION_ANCHORS:
        try:
            res = filing_text(rcept, keyword=anchor, context_chars=2200, max_matches=6)
        except DataError:
            continue
        for ex in res.get("excerpts", []):
            pos = ex.find(anchor)
            if pos < 0:
                continue
            window = ex[pos:]
            units = _UNIT_RE.findall(window[:400]) or _UNIT_RE.findall(ex[:pos])
            if not units:
                continue
            mult = _UNIT_MULT.get(units[0] if _UNIT_RE.findall(window[:400]) else units[-1], 0)
            if not mult:
                continue
            total, parts = 0, []
            for group in _DA_COMPONENT_GROUPS:
                for lab in group:
                    amt = _amount_after(window, lab, mult)
                    if amt:
                        total += amt
                        parts.append(lab)
                        break  # 같은 개념의 다른 표기를 중복 합산하지 않는다
            if not total or not parts:
                continue
            v = _accept(total, f"{anchor} 섹션의 {'+'.join(parts)}", window[:200], units[0])
            if v is not None:
                return v

    raise DataError(
        f"{ent['corp_name']} {yr} 주석에서 D&A(감가상각비+무형자산상각비)를 찾지 못했습니다. "
        f"D&A 를 가정으로 직접 지정하세요."
    )


def da_best(company: str, year: int | None = None, report: str = "annual",
            prefer: str = "CFS") -> Value:
    """D&A 를 얻는 최선의 경로: 현금흐름표 → (없으면) 성격별 분류 주석."""
    cf = cf_extras(company, year, report, prefer)
    if cf.get("da") is not None:
        return cf["da"]
    return da_from_notes(company, year, report, prefer)


def shares_outstanding(company: str, year: int | None = None, report: str = "annual",
                       basis: str = "issued") -> Value:
    """주식수. 단위 '주'.

    basis="issued"(기본)   → 발행주식총수 = 유통주식수 + 자기주식수 (상증법 분모)
    basis="outstanding"    → 유통주식수 (주당가치 계산에 쓰는 쪽)

    ⚠️ **시가총액 계산에는 이 함수를 쓰지 마라.** 시가총액은 거래소가 보통주 기준으로 이미
    계산해 주므로 providers.naver.implied_common_shares(=KRX 시총 ÷ 종가)를 쓴다. 여기서
    나오는 수치는 보통주+우선주 합계라서 보통주 종가와 곱하면 시가총액이 과대계상된다.
    """
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
        # ⚠️ now_to_isu_stock_totqy 는 "**현재까지 발행한** 주식의 총수" 로, 이후 소각된
        # 주식까지 누적된 값이다 — 발행주식수도 유통주식수도 아니다. 예전에 이 필드를 썼는데
        # 실측(2026-08-27) 결과가 이랬다:
        #   SK하이닉스 5,721,980,209주(누적) vs 실제 유통 701,691,520주 → 8.15배 과대
        #   삼성전자   8,975,138,200주(누적) vs 실제 유통 6,630,180,138주 → 1.35배 과대
        # SK하이닉스는 5.7억주를 발행한 적이 없다. 이 값이 DCF·상증법의 분모로 쓰이면
        # 주당가치가 그 배수만큼 낮게 나온다.
        # 올바른 필드: distb_stock_co(유통주식수), tesstk_co(자기주식수).
        #   발행주식총수 = 유통주식수 + 자기주식수
        rows = {(r.get("se") or "").strip(): r for r in j.get("list", [])}
        agg = rows.get("합계") or rows.get("보통주")
        if agg is None:
            raise DataError(f"{ent['corp_name']} 주식총수 표에 합계·보통주 행이 없음")
        distb = _to_int(agg.get("distb_stock_co"))
        treasury = _to_int(agg.get("tesstk_co")) or 0
        if not distb or distb <= 0:
            raise DataError(f"{ent['corp_name']} 유통주식수(distb_stock_co)를 못 구함")
        issued = distb + treasury
        shares = distb if (basis or "issued").lower() == "outstanding" else issued

        def _part(se: str, field: str) -> int:
            r = rows.get(se)
            return (_to_int(r.get(field)) if r else None) or 0

        common_out, pref_out = _part("보통주", "distb_stock_co"), _part("우선주", "distb_stock_co")
        rcept = (j.get("list") or [{}])[0].get("rcept_no", "")
        note = (f"유통주식수 {distb:,} (보통주 {common_out:,} / 우선주 {pref_out:,}) "
                f"+ 자기주식 {treasury:,} = 발행주식총수 {issued:,}. "
                f"반환값 = {'유통주식수' if shares == distb and basis == 'outstanding' else '발행주식총수'}. "
                f"DART 의 '현재까지 발행한 주식의 총수'(누적, 소각분 포함"
                f"{_to_int(agg.get('now_to_isu_stock_totqy')) or 0:,})는 쓰지 않는다.")
        return Value(
            value=shares, unit="주",
            label=(f"{ent['corp_name']} "
                   f"{'유통주식수' if basis == 'outstanding' else '발행주식총수'} ({yr})"),
            provenance=Provenance(
                source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                original_field=("stockTotqySttus/distb_stock_co(합계)" if basis == "outstanding"
                                else "stockTotqySttus/distb_stock_co + tesstk_co(합계)"),
                as_of=f"FY{yr}", filing_date=_filing_date(rcept), note=note,
            ),
            extras={
                "outstanding": Value(
                    distb, "주", label=f"{ent['corp_name']} 유통주식수 ({yr})",
                    provenance=Provenance(
                        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                        original_field="stockTotqySttus/distb_stock_co(합계)", as_of=f"FY{yr}")),
                "treasury": Value(
                    treasury, "주", label=f"{ent['corp_name']} 자기주식수 ({yr})",
                    provenance=Provenance(
                        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                        original_field="stockTotqySttus/tesstk_co(합계)", as_of=f"FY{yr}")),
                # 보통주·우선주를 나눠 노출한다. 합계만 주면 호출부가 "이 주당가치가 보통주
                # 기준인가" 를 판단할 수 없다 — 삼성전자·현대차는 우선주 비중이 커서 이게
                # material 하다.
                "common_outstanding": Value(
                    common_out, "주", label=f"{ent['corp_name']} 보통주 유통주식수 ({yr})",
                    provenance=Provenance(
                        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                        original_field="stockTotqySttus/distb_stock_co(보통주)",
                        as_of=f"FY{yr}")),
                "preferred_outstanding": Value(
                    pref_out, "주", label=f"{ent['corp_name']} 우선주 유통주식수 ({yr})",
                    provenance=Provenance(
                        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
                        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                        original_field="stockTotqySttus/distb_stock_co(우선주)",
                        as_of=f"FY{yr}")),
            },
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


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    """살아있는지 실제 호출로 확인한다(캐시 우회). 실패하면 DataError."""
    from datetime import timedelta
    from core.http import probe

    key = config.require(config.Keys.DART, "DART_API_KEY")
    today = date.today()
    j = probe("GET", f"{_BASE}/list.json", params={
        "crtfc_key": key, "page_count": 1,
        "bgn_de": (today - timedelta(days=7)).strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d")}).json()
    st = j.get("status")
    if st not in ("000", "013"):   # 013 = 조회 결과 없음(응답 자체는 정상)
        raise DataError(f"DART 응답 코드 {st}: {j.get('message')}")
    return "공시목록 조회 OK"

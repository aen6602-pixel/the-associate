"""비상장 외감법인 재무 — DART 감사보고서 원문(document.xml) 표 파싱.

정기보고서 API(fnlttSinglAcntAll)에 없는 비상장 재무를 감사보고서에서 추출한다.
- 라벨은 공백 제거 후 매칭 ('자 본 총 계' → '자본총계')
- 금액은 각 행의 '오른쪽 2개' 숫자 = (당기, 전기)  ← 주석번호 컬럼 회피
- 발행주식수는 주석 텍스트에서 정규식 추출
표 구조가 회계법인마다 달라 완벽하진 않다(근사). 못 찾으면 DataError.
"""
from __future__ import annotations

import io
import re
import zipfile
from functools import lru_cache

from core.schema import DataError
from core.http import get_json, session
from core import config

_BASE = "https://opendart.fss.or.kr/api"

# 계정 → 라벨 후보 (공백 제거·정규화된 형태로 비교)
#
# 아래 DCF 입력용 항목들은 **감사보고서 본표(재무상태표·현금흐름표)** 에 나오는 것만 넣는다.
# 주석 표는 단위가 다른 경우가 많아(실측: 에스케이트리켐 FY2025 본표 '이자의 지급' 2,900,896,319원
# vs 주석 '이자비용' 2,811,495천원) 섞이면 1000배 오차가 난다. `_extract_row` 가 문서 앞에서부터
# 첫 매칭을 취하고 본표가 주석보다 앞에 오므로 본표 값이 잡힌다.
_LABELS = {
    "net_income": {"당기순이익", "당기순이익(손실)", "당기순손익", "당기순이익(손실)"},
    "total_equity": {"자본총계"},
    "total_assets": {"자산총계"},
    "total_liabilities": {"부채총계"},
    "revenue": {"매출액", "수익(매출액)", "영업수익"},
    "operating_income": {"영업이익", "영업이익(손실)"},
    "sga": {"판매비와관리비", "판매비및관리비", "판매관리비"},
    # ── DCF 입력용 (재무상태표) ──
    "cash": {"현금및현금성자산", "현금및현금등가물"},
    "trade_receivables": {"매출채권", "매출채권및기타채권", "매출채권(순액)"},
    "inventories": {"재고자산"},
    "trade_payables": {"매입채무", "매입채무및기타채무"},
    "short_term_debt": {"단기차입금", "유동성장기차입금", "유동성장기부채", "단기차입부채"},
    "long_term_debt": {"장기차입금", "사채", "비유동차입금", "장기차입부채"},
    "lease_liability": {"리스부채", "유동리스부채", "비유동리스부채"},
    # ── DCF 입력용 (현금흐름표) ──
    "capex": {"유형자산의취득", "유형자산취득"},
    "capex_intangible": {"무형자산의취득", "무형자산취득"},
    "interest_paid": {"이자의지급", "이자지급", "이자의지급액"},
    "ocf": {"영업활동으로인한현금흐름", "영업활동현금흐름", "영업활동으로인한순현금흐름"},
}

# 본표에 없고 주석에만 있는 항목 — 단위가 다를 수 있어 호출부에서 매출 대비 비율로 검증해야 한다.
NOTE_ONLY_ITEMS = frozenset({"depreciation"})
_LABELS["depreciation"] = {"감가상각비", "감가상각비및무형자산상각비"}


def _norm(s: str) -> str:
    """비교용 정규화: 공백 제거 + 앞머리 번호(Ⅰ. / 1. / (1)) 제거 + 뒤따르는 주석 참조 제거.

    실측: 포마트 감사보고서는 계정명에 주석 번호가 붙는다 — '매출채권(주석4, 10)',
    '단기차입금(주석8,9,10,11)'. 이걸 안 떼면 라벨 완전일치가 전부 실패한다.
    """
    s = re.sub(r"\s+", "", s or "")
    s = re.sub(r"^[（(]?[0-9Ⅰ-Ⅹⅰ-ⅹ]+[).．]?", "", s)
    s = re.sub(r"[（(]주석[^）)]*[）)]", "", s)   # (주석4,10) 제거
    s = re.sub(r"[（(]\s*주\s*[0-9,，\s]*[）)]", "", s)  # (주1) 형태
    return s


def _extract_shares(text: str) -> int | None:
    """감사보고서 텍스트에서 '발행한 주식수(issued)'를 여러 표현으로 추출."""
    # 1) "발행주식수: 5,000,000주" (트리켐형)
    m = re.search(r"발행주식\s*수\s*[:：]?\s*([\d,]{3,})\s*주", text)
    if m:
        return int(m.group(1).replace(",", ""))
    # 2) "발행할 주식의 총수, 발행한 주식의 수 및 1주당 금액은 각각 A주, B주 (및 C원)" → 발행한=B (포마트형)
    m = re.search(r"발행할\s*주식의\s*총수.{0,60}?발행한\s*주식의?\s*수.{0,80}?"
                  r"각각\s*([\d,]+)\s*주\s*,\s*([\d,]+)\s*주", text, re.S)
    if m:
        return int(m.group(2).replace(",", ""))
    # 3) "발행한 주식의 (총)수 ... N주"
    m = re.search(r"발행한\s*주식의?\s*총?수[^0-9]{0,15}([\d,]{3,})\s*주", text)
    if m:
        return int(m.group(1).replace(",", ""))
    # 4) "보통주식 N주"
    m = re.search(r"보통주\s*식?\s*([\d,]{3,})\s*주", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _num(x: str):
    x = (x or "").strip().replace(",", "").replace(" ", "").replace("\xa0", "")
    if x in ("", "-", "–", "—"):
        return None
    neg = False
    if x[:1] in ("(", "（") and x[-1:] in (")", "）"):
        neg, x = True, x[1:-1]
    if x[:1] in ("△", "▲", "-", "−"):
        neg, x = True, x[1:]
    if not re.fullmatch(r"\d+(\.\d+)?", x):
        return None
    v = int(float(x))
    return -v if neg else v


def _cells(tr: str) -> list[str]:
    raw = re.findall(r"<T[DEH][^>]*>(.*?)</T[DEH]>", tr, re.S | re.I)
    return [re.sub(r"<[^>]+>", "", c).replace("\xa0", " ").strip() for c in raw]


@lru_cache(maxsize=32)
def _report_text(rcept: str) -> str:
    key = config.require(config.Keys.DART, "DART_API_KEY")
    r = session().get(f"{_BASE}/document.xml",
                      params={"crtfc_key": key, "rcept_no": rcept}, timeout=45)
    r.raise_for_status()
    if r.content[:2] != b"PK":
        raise DataError(f"감사보고서 문서 다운로드 실패 (rcept={rcept})")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


def _rows(rcept: str) -> list[list[str]]:
    text = _report_text(rcept)
    return [_cells(t) for t in re.findall(r"<TR[^>]*>(.*?)</TR>", text, re.S | re.I)]


@lru_cache(maxsize=64)
def _audit_reports(corp_code: str) -> tuple:
    """corp_code → ((year, rcept), ...) 별도 감사보고서, 연도 내림차순."""
    key = config.require(config.Keys.DART, "DART_API_KEY")
    j = get_json(f"{_BASE}/list.json", ttl_hours=24, params={
        "crtfc_key": key, "corp_code": corp_code, "pblntf_ty": "F",
        "bgn_de": "20150101", "end_de": "20261231", "page_count": 100})
    found: dict[int, str] = {}
    for it in j.get("list", []):
        nm = it.get("report_nm") or ""
        if "감사보고서" in nm and "연결" not in nm:
            m = re.search(r"\((\d{4})[.\-]\d{2}\)", nm)
            if m:
                found.setdefault(int(m.group(1)), it.get("rcept_no"))  # 최신(첫)
    return tuple(sorted(found.items(), reverse=True))


def _reports_map(corp_code: str) -> dict:
    return dict(_audit_reports(corp_code))


def _extract_row(rows: list, item: str):
    """[당기, 전기] (오른쪽 2개 숫자) 또는 None."""
    targets = {_norm(t) for t in _LABELS[item]}
    for cells in rows:
        label = _norm(next((c for c in cells if c.strip()), ""))
        if label in targets:
            nums = [n for n in (_num(c) for c in cells) if n is not None]
            if len(nums) >= 2:
                return nums[-2:]
            if len(nums) == 1:
                return [nums[0], None]
    return None


def has_audit(corp_code: str) -> bool:
    return bool(_audit_reports(corp_code))


def latest_audit_year(corp_code: str) -> int | None:
    """가장 최근 감사보고서 사업연도. 감사보고서가 전혀 없으면 None."""
    reports = _reports_map(corp_code)
    return max(reports) if reports else None


def _year_amount(corp_code: str, item: str, year: int):
    """정확히 그 연도 값: 해당연도 보고서의 당기, 없으면 (연도+1)보고서의 전기. (amount, rcept) 또는 None."""
    reports = _reports_map(corp_code)
    if year in reports:
        nums = _extract_row(_rows(reports[year]), item)
        if nums and nums[0] is not None:
            return nums[0], reports[year]
    if (year + 1) in reports:
        nums = _extract_row(_rows(reports[year + 1]), item)
        if nums and len(nums) >= 2 and nums[1] is not None:
            return nums[1], reports[year + 1]
    return None


def year_value(corp_code: str, item: str, year: int) -> tuple[int, str, int]:
    """(금액, rcept, 실제연도). 요청연도 데이터 없으면 최신 감사보고서 연도로 대체."""
    reports = _reports_map(corp_code)
    if not reports:
        raise DataError("감사보고서를 찾지 못함(비상장 재무 없음)")
    yr = year if (year in reports or (year + 1) in reports) else max(reports)
    r = _year_amount(corp_code, item, yr)
    if r is None and yr != max(reports):
        yr = max(reports)
        r = _year_amount(corp_code, item, yr)
    if r is None:
        raise DataError(f"감사보고서에서 {item} 값을 찾지 못함")
    return r[0], r[1], yr


def multiyear(corp_code: str, item: str, target_year: int) -> list[dict]:
    """3개년 [{year, amount, rcept}] (최근연도 먼저). 요청연도 없으면 최신연도 기준."""
    reports = _reports_map(corp_code)
    if not reports:
        return []
    anchor = target_year if target_year in reports else max(reports)
    out = []
    for y in (anchor, anchor - 1, anchor - 2):
        r = _year_amount(corp_code, item, y)
        if r is not None:
            out.append({"year": y, "amount": r[0], "rcept": r[1]})
    return out


def dcf_inputs(corp_code: str, year: int | None = None) -> dict:
    """비상장 감사보고서에서 DCF·WACC 입력 원자료를 뽑는다.

    상장사는 DART 정형 API(`providers.dart`)로 같은 값을 얻지만, 외감법인은 그 API 에
    데이터가 없어(013 오류) 감사보고서 원문 표를 파싱해야 한다.

    돌려주는 dict: {항목: {"amount", "rcept", "year"}} — 못 찾은 항목은 아예 키가 없다
    (0 으로 채우지 않는다). 현금흐름표 유출 항목(capex·이자의 지급)은 음수로 공시되므로
    **절댓값**으로 정규화해 상장사 경로와 부호를 맞춘다.

    `depreciation` 은 본표가 아닌 주석에서만 나오는 경우가 많고 주석표는 단위가 다를 수 있어
    (실측: 본표 원 vs 주석 천원) **매출 대비 0.3~40% 검증을 통과할 때만** 담는다.
    """
    reports = _reports_map(corp_code)
    if not reports:
        raise DataError("감사보고서를 찾지 못함(비상장 재무 없음)")
    yr = year if (year in reports) else max(reports)

    out: dict[str, dict] = {}
    for item in ("cash", "trade_receivables", "inventories", "trade_payables",
                 "short_term_debt", "long_term_debt", "lease_liability",
                 "capex", "capex_intangible", "interest_paid", "ocf", "revenue"):
        try:
            amount, rcept, used = year_value(corp_code, item, yr)
        except DataError:
            continue
        if item in ("capex", "capex_intangible", "interest_paid"):
            amount = abs(amount)
        out[item] = {"amount": amount, "rcept": rcept, "year": used}

    rev = (out.get("revenue") or {}).get("amount")
    try:
        amount, rcept, used = year_value(corp_code, "depreciation", yr)
        amount = abs(amount)
        if rev and 0.003 <= amount / rev <= 0.40:
            out["depreciation"] = {"amount": amount, "rcept": rcept, "year": used,
                                   "note": "주석 표에서 추출(매출 대비 검증 통과)"}
    except DataError:
        pass

    out["_year"] = yr
    out["_rcept"] = reports[yr]
    return out


def shares(corp_code: str, year: int | None = None) -> tuple[int, str, int]:
    """(발행주식수, rcept, 사용한연도). 주석 텍스트에서 정규식 추출."""
    reports = _reports_map(corp_code)
    if not reports:
        raise DataError("감사보고서를 찾지 못함")
    ry = year if (year in reports) else max(reports)
    n = _extract_shares(_report_text(reports[ry]))
    if n and n > 0:
        return n, reports[ry], ry
    raise DataError("감사보고서에서 발행주식수(발행한 주식의 수)를 찾지 못함")

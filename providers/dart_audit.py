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
_LABELS = {
    "net_income": {"당기순이익", "당기순이익(손실)", "당기순손익", "당기순이익(손실)"},
    "total_equity": {"자본총계"},
    "total_assets": {"자산총계"},
    "total_liabilities": {"부채총계"},
    "revenue": {"매출액", "수익(매출액)", "영업수익"},
    "operating_income": {"영업이익", "영업이익(손실)"},
}
def _norm(s: str) -> str:
    """공백 제거 + 앞머리 번호(Ⅰ. / 1. / (1)) 제거 → 'Ⅰ. 매출액' → '매출액'."""
    s = re.sub(r"\s+", "", s or "")
    s = re.sub(r"^[（(]?[0-9Ⅰ-Ⅹⅰ-ⅹ]+[).．]?", "", s)
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

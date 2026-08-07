"""Damodaran 데이터셋 provider (무료, 키 불필요).

국가별:
  - Equity Risk Premium (ERP / MRP)
  - Country Risk Premium (CRP)
  - Corporate Tax Rate
  - Moody's sovereign rating

소스: https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx
갱신: 연 1~2회 (연초 + 년중) → 7일 캐시.

'Regional breakdown' 시트를 사용한다 (0행 헤더, 국가별 단일 값이 깔끔하게 정리됨).
시트명이 바뀌어도 헤더로 자동 탐지하도록 방어 코드를 둔다.
"""
from __future__ import annotations

import io
import warnings
from functools import lru_cache
from typing import Optional

import pandas as pd

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_bytes

CTRYPREM_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx"

# 국가명 별칭 → Damodaran 표기 매칭용 토큰 (소문자 부분일치)
_COUNTRY_ALIASES = {
    "KR": ["korea (south)", "south korea", "korea, republic", "korea"],
    "US": ["united states"],
    "JP": ["japan"],
    "TW": ["taiwan"],
}


def _norm(s) -> str:
    return str(s).strip().lower()


@lru_cache(maxsize=1)
def _load() -> tuple[pd.DataFrame, str, Optional[str]]:
    """Regional breakdown 시트 + 갱신일자를 로드/캐시."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = get_bytes(CTRYPREM_URL, ttl_hours=24 * 7)
        xls = pd.ExcelFile(io.BytesIO(raw))

        # 1) 갱신일자 (provenance 용)
        update_date = None
        if "ERPs by country" in xls.sheet_names:
            head = xls.parse("ERPs by country", header=None, nrows=6)
            for i in range(len(head)):
                if "date of update" in _norm(head.iloc[i, 0]):
                    val = head.iloc[i, 1]
                    update_date = str(pd.to_datetime(val).date()) if pd.notna(val) else None
                    break

        # 2) 국가별 값 시트: 'Regional breakdown' 우선, 없으면 헤더로 탐지
        target = None
        if "Regional breakdown" in xls.sheet_names:
            target = "Regional breakdown"
        else:
            for name in xls.sheet_names:
                probe = xls.parse(name, header=0, nrows=1)
                cols = [_norm(c) for c in probe.columns]
                if "country" in cols and any("equity risk premium" in c for c in cols):
                    target = name
                    break
        if target is None:
            raise DataError("Damodaran: ERP 국가별 시트를 찾지 못함")

        df = xls.parse(target, header=0)
        df.columns = [str(c).strip() for c in df.columns]
        return df, target, update_date


def _col(df: pd.DataFrame, *needles: str) -> str:
    for c in df.columns:
        cl = c.lower()
        if all(n in cl for n in needles):
            return c
    raise DataError(f"Damodaran 컬럼 탐지 실패({needles}). 가용: {list(df.columns)}")


def _row_for(df: pd.DataFrame, country: str) -> pd.Series:
    key = country.strip().upper()
    aliases = _COUNTRY_ALIASES.get(key, [country.strip().lower()])
    ccol = _col(df, "country")
    mask = df[ccol].apply(lambda x: any(a in _norm(x) for a in aliases) if pd.notna(x) else False)
    hits = df[mask]
    if hits.empty:
        raise DataError(
            f"Damodaran 국가 매칭 실패: {country} (별칭 {aliases}). "
            f"예시: {df[ccol].dropna().head(5).tolist()}"
        )
    # 가장 짧은 이름(정확 매칭 우선; 예: 'Korea' vs 'North Korea')
    return hits.loc[hits[ccol].astype(str).str.len().idxmin()]


def _pct(v) -> float:
    v = float(v)
    return round(v * 100, 4) if abs(v) < 1 else round(v, 4)


def _make(country: str, needles: tuple[str, ...], label: str, unit: str = "%") -> Value:
    df, sheet, upd = _load()
    ccol = _col(df, "country")
    col = _col(df, *needles)
    row = _row_for(df, country)
    if pd.isna(row[col]):
        raise DataError(f"Damodaran: {row[ccol]} 의 '{col}' 값이 비어있음")
    return Value(
        value=_pct(row[col]) if unit == "%" else row[col],
        unit=unit,
        label=f"{label} ({row[ccol]})",
        provenance=Provenance(
            source="Damodaran",
            source_type=SourceType.REFERENCE,  # 업계 표준 참조 데이터셋 (정부 API 아님)
            source_url=CTRYPREM_URL,
            original_field=f"{sheet}!{col}",
            as_of=upd,
            note=f"Moody's rating={row.get(_col(df, 'moody'), 'n/a')}"
            if any("moody" in c.lower() for c in df.columns) else None,
        ),
    )


def equity_risk_premium(country: str = "KR") -> Value:
    """국가 총 주식위험프리미엄 (= market risk premium). 단위 %."""
    return _make(country, ("equity", "risk", "premium"), "Equity Risk Premium")


def country_risk_premium(country: str = "KR") -> Value:
    """국가위험프리미엄 (CRP). 단위 %."""
    return _make(country, ("country", "risk", "premium"), "Country Risk Premium")


def corporate_tax_rate(country: str = "KR") -> Value:
    """법인세율. 단위 %."""
    return _make(country, ("corporate", "tax"), "Corporate Tax Rate")

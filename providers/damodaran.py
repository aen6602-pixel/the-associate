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
from core.cache import TTL_INDEX, ttl_cache
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


@ttl_cache(TTL_INDEX, maxsize=1)
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


# ── 산업별 베타·자본구조 (betas.xls 계열) ──────────────────────────
# 'Industry Averages' 시트 컬럼(실측 2026-08, 2026-01-05 갱신):
#   Industry Name | Number of firms | Beta | D/E Ratio | Effective Tax rate |
#   Unlevered beta | Cash/Firm value | Unlevered beta corrected for cash | ...
# 비상장사·신설법인의 베타와 '목표 부채비중' 을 여기서 얻는다.
_INDUSTRY_FILES = {
    "emerging": "betaemerg.xls",   # 한국 등 신흥시장
    "global": "betaGlobal.xls",
    "us": "betas.xls",
}
_REGION_BY_COUNTRY = {"KR": "emerging", "TW": "emerging", "US": "us", "JP": "global"}


# 산업별 자본비용(WACC) 데이터셋 — 'Industry Averages' 시트, 헤더 18행 (실측 2026-08).
# 컬럼: Industry Name | Number of Firms | Beta | Cost of Equity | E/(D+E) | Std Dev in Stock |
#       Cost of Debt | Tax Rate | After-tax Cost of Debt | D/(D+E) | Cost of Capital | ...
# 해외 기업은 공시에서 Kd 를 뽑을 수 없으므로(DART 는 한국 전용) 이 산업평균을 쓴다.
_WACC_FILES = {
    "emerging": "waccemerg.xls",
    "global": "waccGlobal.xls",
    "us": "wacc.xls",
}


def _industry_frame(region: str, kind: str = "beta") -> tuple[pd.DataFrame, str, str]:
    files = _INDUSTRY_FILES if kind == "beta" else _WACC_FILES
    fname = files.get(region)
    if fname is None:
        raise DataError(f"지원하지 않는 지역: {region} (지원: {', '.join(files)})")
    url = f"https://pages.stern.nyu.edu/~adamodar/pc/datasets/{fname}"
    raw = get_bytes(url, ttl_hours=24 * 30)
    xls = pd.ExcelFile(io.BytesIO(raw))
    sheet = next((s for s in xls.sheet_names if "industry" in s.lower()), xls.sheet_names[0])
    probe = xls.parse(sheet, header=None, nrows=25)
    hdr = None
    for i in range(len(probe)):
        cells = [str(c) for c in probe.iloc[i].tolist()]
        if any("Industry" in c and "Name" in c for c in cells):
            hdr = i
            break
    if hdr is None:
        raise DataError(f"{fname} 에서 산업 테이블 헤더를 찾지 못했습니다.")
    df = xls.parse(sheet, header=hdr).dropna(how="all")
    df.columns = [str(c).strip() for c in df.columns]
    as_of = None
    for i in range(min(4, len(probe))):
        for c in probe.iloc[i].tolist():
            if hasattr(c, "strftime"):
                as_of = c.strftime("%Y-%m-%d")
                break
        if as_of:
            break
    return df, url, as_of or "n/a"


def industry_list(region: str = "emerging") -> list[str]:
    df, _, _ = _industry_frame(region)
    return [str(x).strip() for x in df["Industry Name"].dropna().tolist()]


def industry_metrics(industry: str, region: str = "emerging") -> dict:
    """{unlevered_beta, levered_beta, de_ratio, debt_to_value, effective_tax_rate} (Value).

    industry 는 Damodaran 산업명(예: 'Semiconductor', 'Food Processing') — 부분일치로 찾는다.
    `debt_to_value` = (D/E)/(1+D/E) 로 환산한 목표 부채비중이라 WACC 에 바로 넣을 수 있다."""
    df, url, as_of = _industry_frame(region)
    names = df["Industry Name"].astype(str)
    q = _norm(industry)
    exact = df[names.map(_norm) == q]
    hit = exact if len(exact) else df[names.map(lambda s: q in _norm(s))]
    if not len(hit):
        import difflib

        close = difflib.get_close_matches(industry, names.tolist(), n=5, cutoff=0.4)
        raise DataError(
            f"Damodaran 산업 '{industry}' 를 {region} 데이터셋에서 찾지 못했습니다. "
            + (f"비슷한 이름: {', '.join(close)}" if close else
               "get_industry_benchmarks 없이 베타를 직접 지정하세요."))
    row = hit.iloc[0]
    name = str(row["Industry Name"]).strip()

    def _prov(field: str, note: str) -> Provenance:
        return Provenance(source=f"Damodaran ({_INDUSTRY_FILES[region]})",
                          source_type=SourceType.REFERENCE, source_url=url,
                          original_field=field, as_of=as_of, note=note)

    def _num(col: str) -> float | None:
        try:
            v = float(row[col])
        except (TypeError, ValueError, KeyError):
            return None
        return None if v != v else v  # NaN 제거

    de = _num("D/E Ratio")
    unlev = _num("Unlevered beta")
    lev = _num("Beta")
    tax = _num("Effective Tax rate")
    n_firms = _num("Number of firms")

    out: dict[str, Value] = {}
    if unlev is not None:
        out["unlevered_beta"] = Value(
            round(unlev, 4), "배", label=f"{name} 산업 무차입베타 ({region})",
            provenance=_prov("Unlevered beta",
                             f"표본 {int(n_firms) if n_firms else '?'}개사. 자기 자본구조로 "
                             f"재레버리지(relever)해서 쓴다."))
    if lev is not None:
        out["levered_beta"] = Value(
            round(lev, 4), "배", label=f"{name} 산업 레버드베타 ({region})",
            provenance=_prov("Beta", f"산업 평균 자본구조(D/E {de:.3f}) 기준"
                                     if de is not None else "산업 평균"))
    if de is not None:
        out["de_ratio"] = Value(
            round(de, 4), "배", label=f"{name} 산업 D/E ({region})",
            provenance=_prov("D/E Ratio", "시장가치 기준 산업 평균 부채/자본"))
        out["debt_to_value"] = Value(
            round(de / (1 + de), 4), "비율", label=f"{name} 산업 목표부채비중 D/(D+E) ({region})",
            provenance=_prov("D/E Ratio", f"D/E {de:.4f} → D/(D+E) = D/E÷(1+D/E) 로 환산. "
                                          f"WACC 의 목표 자본구조로 사용."))
    if tax is not None:
        out["effective_tax_rate"] = Value(
            round(tax * 100, 2), "%", label=f"{name} 산업 실효세율 ({region})",
            provenance=_prov("Effective Tax rate", "무차입베타 재레버리지에 쓰는 세율"))
    out["industry_name"] = Value(
        0, "", label=name,
        provenance=_prov("Industry Name", f"매칭된 Damodaran 산업명: {name}"))
    return out


def industry_wacc(industry: str, region: str = "emerging") -> dict:
    """산업별 자본비용 구성요소 — {cost_of_debt, debt_to_value, levered_beta, tax_rate} (Value).

    해외 기업은 DART 공시가 없어 이자비용÷차입금으로 Kd 를 만들 수 없다. 그때 이 산업평균
    Kd 와 목표 부채비중을 쓴다(등급 reference — 그 회사 실제 값이 아니라 산업 평균이다)."""
    df, url, as_of = _industry_frame(region, kind="wacc")
    names = df["Industry Name"].astype(str)
    q = _norm(industry)
    exact = df[names.map(_norm) == q]
    hit = exact if len(exact) else df[names.map(lambda s: q in _norm(s))]
    if not len(hit):
        import difflib

        close = difflib.get_close_matches(industry, names.tolist(), n=5, cutoff=0.4)
        raise DataError(
            f"Damodaran 산업 '{industry}' 를 {region} 자본비용 데이터셋에서 찾지 못했습니다. "
            + (f"비슷한 이름: {', '.join(close)}" if close else ""))
    row = hit.iloc[0]
    name = str(row["Industry Name"]).strip()

    def _num(col: str) -> float | None:
        try:
            v = float(row[col])
        except (TypeError, ValueError, KeyError):
            return None
        return None if v != v else v

    def _prov(field: str, note: str) -> Provenance:
        return Provenance(source=f"Damodaran ({_WACC_FILES[region]})",
                          source_type=SourceType.REFERENCE, source_url=url,
                          original_field=field, as_of=as_of, note=note)

    n_firms = _num("Number of Firms")
    tag = f"표본 {int(n_firms) if n_firms else '?'}개사, {region} 산업평균"
    out: dict[str, Value] = {}
    kd = _num("Cost of Debt")
    if kd is not None:
        out["cost_of_debt"] = Value(
            round(kd * 100, 2), "%", label=f"{name} 산업 세전 타인자본비용 ({region})",
            provenance=_prov("Cost of Debt", f"{tag}. 회사 고유값이 아니라 산업 평균이다."))
    dv = _num("D/(D+E)")
    if dv is not None:
        out["debt_to_value"] = Value(
            round(dv, 4), "비율", label=f"{name} 산업 목표부채비중 D/(D+E) ({region})",
            provenance=_prov("D/(D+E)", f"{tag}. 시장가치 기준."))
    b = _num("Beta")
    if b is not None:
        out["levered_beta"] = Value(round(b, 4), "배",
                                    label=f"{name} 산업 레버드베타 ({region})",
                                    provenance=_prov("Beta", tag))
    t = _num("Tax Rate")
    if t is not None:
        out["tax_rate"] = Value(round(t * 100, 2), "%", label=f"{name} 산업 실효세율 ({region})",
                                provenance=_prov("Tax Rate", tag))
    out["industry_name"] = Value(0, "", label=name,
                                 provenance=_prov("Industry Name", f"매칭된 산업명: {name}"))
    return out


def region_for(country: str) -> str:
    return _REGION_BY_COUNTRY.get((country or "KR").strip().upper(), "global")


def equity_risk_premium(country: str = "KR") -> Value:
    """국가 총 주식위험프리미엄 (= market risk premium). 단위 %."""
    return _make(country, ("equity", "risk", "premium"), "Equity Risk Premium")


def country_risk_premium(country: str = "KR") -> Value:
    """국가위험프리미엄 (CRP). 단위 %."""
    return _make(country, ("country", "risk", "premium"), "Country Risk Premium")


def corporate_tax_rate(country: str = "KR") -> Value:
    """법인세율. 단위 %."""
    return _make(country, ("corporate", "tax"), "Corporate Tax Rate")

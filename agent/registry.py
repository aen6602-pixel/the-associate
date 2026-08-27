"""Tool 레지스트리 — Claude tool-use 스키마 + provider 디스패치.

새 데이터 소스를 추가하는 = 여기에 tool 하나를 등록하는 것.
LLM 은 "어떤 tool 을 어떤 인자로" 부를지만 정하고, 실제 값은 여기서 provider(코드)가 만든다.
각 tool 결과는 Value.to_dict() (출처·등급 포함) 로 반환되어 UI 와 LLM 양쪽에서 쓰인다.
"""
from __future__ import annotations

from typing import Callable

from core.schema import Value, Provenance, SourceType, DataError
from core import skills as skills_lib
from providers import damodaran, fx, ecos, fred, dart, sec, edinet, finmind, openfigi, mops
from engines import (wacc as wacc_engine, sangjeung as sangjeung_engine,
                     dcf as dcf_engine, comps as comps_engine,
                     dcf_inputs as dcf_inputs_engine, beta as beta_engine,
                     market_data, business_mix)

_ITEM_ENUM = ["revenue", "operating_income", "net_income",
             "total_assets", "total_liabilities", "total_equity",
             "cash", "ppe", "inventories", "trade_receivables", "trade_payables", "da"]
_ITEM_DESC = ("항목: revenue(매출액), operating_income(영업이익), net_income(당기순이익), "
             "total_assets(자산총계), total_liabilities(부채총계), total_equity(자본총계), "
             "cash(현금및현금성자산), ppe(유형자산), inventories(재고자산), "
             "trade_receivables(매출채권), trade_payables(매입채무), "
             "da(감가상각비+무형자산상각비 — EBITDA = operating_income + da).")
# 일본(EDINET)은 유가증권보고서 XBRL 에서 핵심 6항목만 매핑돼 있다. 도구 설명이 지원하지
# 않는 항목을 광고하면 LLM 이 "데이터가 없다" 로 오해하고 포기한다 — 시장별로 실제 지원
# 범위를 그대로 적는다.
_ITEM_ENUM_JP = ["revenue", "operating_income", "net_income",
                 "total_assets", "total_liabilities", "total_equity"]
_ITEM_DESC_JP = ("항목: revenue(매출액), operating_income(영업이익), net_income(당기순이익), "
                 "total_assets(자산총계), total_liabilities(부채총계), total_equity(자본총계). "
                 "일본은 이 6개만 지원한다(현금·D&A 는 EDINET XBRL 매핑이 없음).")

# ── tool 이름 → (실행 함수, Claude 스키마) ──────────────────────────────────

def _erp(country: str) -> Value:
    return damodaran.equity_risk_premium(country)

def _crp(country: str) -> Value:
    return damodaran.country_risk_premium(country)

def _tax(country: str) -> Value:
    return damodaran.corporate_tax_rate(country)

def _fx(base: str, quote: str, date: str | None = None) -> Value:
    return fx.fx_rate(base, quote, date)

def _rf(country: str, tenor: str = "10Y") -> Value:
    c = country.strip().upper()
    if c == "KR":
        return ecos.risk_free_rate(tenor)
    if c == "US":
        return fred.risk_free_rate(tenor)
    raise DataError(f"무위험수익률 미지원 국가: {country} (현재 KR, US)")

def _wacc(country: str, beta: float, cost_of_debt_pct: float,
          debt_to_value: float, tenor: str = "10Y") -> Value:
    return wacc_engine.compute_wacc(country, beta, cost_of_debt_pct, debt_to_value, tenor)

def _fin_item(company: str, item: str, year: int | None = None, report: str = "annual") -> Value:
    # D&A 는 손익계산서 계정이 아니라 현금흐름표(없으면 성격별 분류 주석)에서 나온다 —
    # 별도 경로가 필요해 ITEM_MAP 에 없고, 그래서 예전에는 어떤 도구로도 뽑을 수 없었다.
    if item == "da":
        return dart.da_best(company, _pos(year), report)
    return dart.financial_item(company, item, year, report)

def _search_filings(corp: str, bgn_de: str, end_de: str, kw: str | None = None) -> Value:
    rows = dart.list_filings(corp, bgn_de, end_de, kw)
    return Value(
        value=rows, unit="filing_list",
        label=(f"{corp} 공시목록 ({bgn_de}~{end_de}" + (f", 키워드='{kw}'" if kw else "")
              + f") — {len(rows)}건"),
        provenance=Provenance(
            source="DART (금융감독원) 공시검색", source_type=SourceType.AUTHORITATIVE,
            source_url="https://dart.fss.or.kr", original_field="list.json",
            note="report_nm/rcept_no 목록만 제공. 본문 내용은 read_dart_filing 으로 조회.",
        ),
    )

def _read_filing(rcept_no: str, keyword: str | None = None) -> Value:
    result = dart.filing_text(rcept_no, keyword)
    return Value(
        value=result, unit="filing_text",
        label=f"공시원문 {rcept_no}" + (f" (키워드='{keyword}')" if keyword else ""),
        provenance=Provenance(
            source="DART (금융감독원) 공시원문", source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
            original_field="document.xml",
            note="비정형 서술문 원문. 여기서 추출한 숫자는 반드시 llm_estimate 로 표시할 것.",
        ),
    )

def _search_edinet_filings(company: str, bgn_de: str, end_de: str, doc_type: str | None = None) -> Value:
    rows = edinet.list_filings(company, bgn_de, end_de, doc_type)
    return Value(
        value=rows, unit="filing_list",
        label=f"{company} EDINET 공시목록 ({bgn_de}~{end_de}) — {len(rows)}건",
        provenance=Provenance(
            source="EDINET (일본 금융청) 공시검색", source_type=SourceType.AUTHORITATIVE,
            source_url="https://disclosure.edinet-fsa.go.jp", original_field="documents.json",
            note="docID/docDescription 목록만 제공. 본문은 read_edinet_filing 으로 조회.",
        ),
    )

def _read_edinet_filing(docid: str, keyword: str | None = None) -> Value:
    result = edinet.filing_text(docid, keyword)
    return Value(
        value=result, unit="filing_text",
        label=f"EDINET 공시원문 {docid}" + (f" (키워드='{keyword}')" if keyword else ""),
        provenance=Provenance(
            source="EDINET (일본 금융청) 공시원문", source_type=SourceType.AUTHORITATIVE,
            source_url=f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{docid}",
            original_field="documents/{docid}?type=5(CSV)",
            note="비정형 서술문 원문(일본어). 여기서 추출한 숫자는 반드시 llm_estimate 로 표시할 것.",
        ),
    )

def _search_sec_filings(keyword: str, company: str | None = None, forms: str | None = None,
                        start_date: str | None = None, end_date: str | None = None) -> Value:
    rows = sec.search_filings(keyword, company, forms, start_date, end_date)
    return Value(
        value=rows, unit="filing_list",
        label=f"SEC 통합검색 '{keyword}'" + (f" ({company})" if company else "") + f" — {len(rows)}건",
        provenance=Provenance(
            source="SEC EDGAR 통합 원문검색", source_type=SourceType.AUTHORITATIVE,
            source_url="https://www.sec.gov/cgi-bin/srqsb", original_field="efts.sec.gov/LATEST/search-index",
            note="cik/accession/filename 목록만 제공. 본문은 read_sec_filing 으로 조회.",
        ),
    )

def _read_sec_filing(cik: str, accession: str, filename: str, keyword: str | None = None) -> Value:
    result = sec.filing_text(cik, accession, filename, keyword)
    return Value(
        value=result, unit="filing_text",
        label=f"SEC 공시원문 {accession}/{filename}" + (f" (키워드='{keyword}')" if keyword else ""),
        provenance=Provenance(
            source="SEC EDGAR 공시원문", source_type=SourceType.AUTHORITATIVE,
            source_url=result.get("url", ""), original_field="EDGAR Archives 문서",
            note="비정형 서술문 원문. 여기서 추출한 숫자는 반드시 llm_estimate 로 표시할 것.",
        ),
    )

def _mops_recent(company: str) -> Value:
    return mops.recent_disclosures_value(company)

def _fin_item_us(company: str, item: str, year: int | None = None) -> Value:
    return sec.financial_item(company, item, year)

def _fin_item_jp(company: str, item: str, year: int | None = None) -> Value:
    return edinet.financial_item(company, item, year)

def _fin_item_tw(company: str, item: str, year: int | None = None) -> Value:
    return finmind.financial_item(company, item, year)

def _figi(id_type: str, id_value: str, exch_code: str | None = None) -> Value:
    return openfigi.figi(id_type, id_value, exch_code)

def _sangjeung(company: str, year: int | None = None,
               real_estate_heavy: bool | None = None, report: str = "annual",
               nav_only: bool | None = None, largest_shareholder: bool = False,
               sme: bool = False) -> Value:
    return sangjeung_engine.evaluate(company, _pos(year), real_estate_heavy, report,
                                     nav_only, bool(largest_shareholder), bool(sme))

def _dcf(company: str, wacc_pct: float, net_debt: float, revenue_growth_pct: float,
         ebit_margin_pct: float, da_pct: float, capex_pct: float, nwc_pct: float,
         terminal_growth_pct: float, forecast_years: int = 5,
         tax_rate_pct: float | None = None, year: int | None = None,
         market: str = "KR", allow_mixed: bool = False) -> Value:
    return dcf_engine.evaluate(company, wacc_pct, net_debt, revenue_growth_pct,
                               ebit_margin_pct, da_pct, capex_pct, nwc_pct,
                               terminal_growth_pct, forecast_years, tax_rate_pct, year,
                               market, bool(allow_mixed))

def _comps(companies: list, target: str | None = None, market: str = "KR",
           as_of: str | None = None, basis: str = "LTM",
           display_currency: str = "USD") -> Value:
    return comps_engine.evaluate(companies, _blank(target), _blank(market) or "KR",
                                 _blank(as_of), _blank(basis) or "LTM",
                                 _blank(display_currency) or "USD")


def _fin_history(company: str, item: str, years: int = 3, market: str = "KR") -> Value:
    """최근 N개 회계연도 시계열. **연도를 인자로 받지 않는다** — 각 시장 provider 가
    '실제로 데이터가 있는 최신 회계연도' 를 찾고 거기서 내려온다."""
    # 재무 시계열은 비상장사도 대상이다 → 시세를 전제하는 resolve() 대신 이쪽을 쓴다.
    spec = market_data.resolve_financials(company, _blank(market) or "KR")
    n = int(_pos(years) or 3)
    h = market_data.history(spec, item, n)
    rows = h["rows"]
    if not rows:
        raise DataError(f"{spec['name']}: '{item}' 연간 시계열을 찾지 못했습니다.")
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    detail = " / ".join(f"{r.get('period') or r['year']} {f(r['amount'])}" for r in rows)
    latest = rows[0]
    note = (f"최근 {len(rows)}개 회계연도({rows[0].get('period')} ~ {rows[-1].get('period')}): "
            f"{detail} [{h['currency']}]. 출처 {h['source']} · {h['basis']}"
            + (f" · 접수일 {h['filing_date']}" if h.get("filing_date") else "")
            + ". 연도는 조회 시점에 공시가 존재하는 최신 회계연도부터 역순으로 잡혔다 — "
              "호출부가 연도를 찍지 않는다.")
    extras = {}
    for r in rows:
        key = str(r.get("period") or r["year"])
        extras[key] = Value(
            r["amount"], h["currency"], label=f"{spec['name']} {item} {key}",
            provenance=Provenance(
                source=h["source"], source_type=SourceType.AUTHORITATIVE,
                source_url=h["source_url"], original_field=h["basis"],
                as_of=key, filing_date=h.get("filing_date")))
    return Value(
        value=latest["amount"], unit=h["currency"],
        label=f"{spec['name']} {item} 최근 {len(rows)}개년 (최신 {latest.get('period')})",
        provenance=Provenance(
            source=h["source"], source_type=SourceType.AUTHORITATIVE,
            source_url=h["source_url"], original_field=h["basis"],
            as_of=str(latest.get("period")), filing_date=h.get("filing_date"), note=note),
        extras=extras)


def _business_mix(company: str, year: int | None = None) -> Value:
    return business_mix.gate_value(company, _pos(year))


def _market_cost_of_debt(company: str, year: int | None = None, country: str = "KR",
                         rating: str | None = None) -> Value:
    return dcf_inputs_engine.market_cost_of_debt(company, _pos(year),
                                                 _blank(country) or "KR", _blank(rating))


def _market_cap(company: str, market: str = "KR", as_of: str | None = None) -> Value:
    spec = market_data.resolve(company, _blank(market) or "KR")
    return market_data.market_cap(spec, _blank(as_of))


def _ebitda(company: str, market: str = "KR", basis: str = "LTM") -> Value:
    """EBITDA = 영업이익 + D&A. 산술을 LLM 이 하지 않도록 엔진이 계산해 근거를 붙인다."""
    spec = market_data.resolve(company, _blank(market) or "KR")
    use_ltm = (_blank(basis) or "LTM").upper() != "FY"
    if use_ltm and market_data.supports_ltm(spec["market"]):
        ebit, eb_basis = market_data.ltm(spec, "operating_income")
        da, da_basis = market_data.ltm(spec, "da")
    else:
        ebit, eb_basis = market_data.point(spec, "operating_income"), "FY"
        da, da_basis = market_data.point(spec, "da"), "FY"
    total = ebit.value + da.value
    mixed = "" if eb_basis == da_basis else (
        f" ⚠️ 기준기간 혼용: 영업이익={eb_basis}, D&A={da_basis} — 비교표에 이 사실을 표시할 것.")
    return Value(
        total, spec["currency"], label=f"{spec['name']} EBITDA ({eb_basis} 기준)",
        provenance=Provenance(
            source="계산엔진(engines.market_data)", source_type=SourceType.COMPUTED,
            source_url="(computed: 영업이익 + D&A)",
            as_of=ebit.provenance.as_of,
            note=(f"영업이익 {ebit.value:,.0f} ({eb_basis}, {ebit.provenance.as_of}) + "
                  f"D&A {da.value:,.0f} ({da_basis}, {da.provenance.as_of}) = "
                  f"{total:,.0f} {spec['currency']}.{mixed}"),
        ),
        extras={"operating_income": ebit, "da": da},
    )


# ── 선택 인자 정규화 ──────────────────────────────────────────────
# LLM 은 선택 인자를 **생략하지 않고 0/빈문자열로 채워 보내는 일이 흔하다**(실측: gpt-5.6-terra
# 가 beta_override=0, year=0, industry="" 를 넘김). 그대로 두면 β=0 인 WACC(=Rf)가 조용히
# 계산되거나 DART 조회가 실패한다 → "값 없음" 으로 해석해 자동 도출 경로를 타게 한다.
def _blank(x):
    """None/빈문자열/공백 → None."""
    return None if x is None or (isinstance(x, str) and not x.strip()) else x


def _pos(x):
    """None/0/음수/빈값 → None. 0 이나 음수가 의미 없는 인자(연도·베타·비율)에만 쓴다."""
    x = _blank(x)
    if x is None:
        return None
    try:
        return x if float(x) > 0 else None
    except (TypeError, ValueError):
        return None


# ── DCF 입력 자동 도출 ────────────────────────────────────────────
def _net_debt(company: str, year: int | None = None, include_lease: bool = True,
              market: str = "KR") -> Value:
    m = market_data.normalize_market(_blank(market), "KR")
    if m == "KR":
        return dcf_inputs_engine.net_debt(company, _pos(year), include_lease)
    spec = market_data.resolve(company, m)
    return market_data.net_debt(spec, include_lease)


def _dcf_assumptions(company: str, n: int = 5, year: int | None = None) -> Value:
    """5개년 실적에서 DCF 가정 후보를 한 번에 뽑아 하나의 Value 로 묶어 돌려준다."""
    r = dcf_inputs_engine.historical_ratios(company, int(_pos(n) or 5), _pos(year))
    found = {k: v for k, v in r.items()
             if k.endswith("_pct") and isinstance(v, Value)}
    if not found:
        raise DataError(f"{company} 의 5개년 비율을 하나도 계산하지 못했습니다.")
    lines = [f"{v.label}: {v.value}%" for v in found.values()]
    note = (f"{r['company']} {r['years'][-1]}~{r['years'][0]} 실적 기반. " + " / ".join(lines)
            + (f". 자동 도출 실패: {', '.join(r['missing'])} → 이 항목만 가정으로 지정 필요"
               if r["missing"] else ". 전 항목 자동 도출 성공")
            + (f". 운전자본 기준: {r['nwc_basis']}" if r.get("nwc_basis") else "")
            + (f" ⚠️ {r['nwc_needs_confirmation']}" if r.get("nwc_needs_confirmation") else ""))
    return Value(
        value=len(found), unit="개 항목",
        label=f"{r['company']} DCF 가정 자동 도출({r['years'][-1]}~{r['years'][0]})",
        provenance=Provenance(source="계산엔진(engines.dcf_inputs)",
                              source_type=SourceType.COMPUTED,
                              source_url="(computed from DART 5개년 공시)",
                              as_of=r["as_of"], note=note),
        extras=found,
    )


def _cost_of_debt(company: str, year: int | None = None, include_lease: bool = True) -> Value:
    return dcf_inputs_engine.cost_of_debt(company, _pos(year), include_lease)


def _terminal_growth(country: str = "KR", tenor: str = "10Y") -> Value:
    return dcf_inputs_engine.terminal_growth_cap(country, _blank(tenor) or "10Y")


def _beta(company: str, industry: str | None = None, country: str = "KR",
          period: str = "week", years: int = 5, market: str | None = None,
          symbol: str | None = None) -> Value:
    return beta_engine.beta_for(company, _blank(industry), country,
                               _blank(period) or "week", int(_pos(years) or 5),
                               "KOSPI", _blank(market), _blank(symbol))


def _industry_benchmarks(industry: str, country: str = "KR") -> Value:
    """Damodaran 산업 평균(무차입베타·D/E·목표부채비중·실효세율)을 한 Value 로 묶어 돌려준다."""
    industry = _blank(industry)
    if not industry:
        raise DataError("industry (Damodaran 산업명)를 지정하세요. 예: Semiconductor")
    region = damodaran.region_for(country)
    m = damodaran.industry_metrics(industry, region)
    name = m["industry_name"].label
    parts = {k: v for k, v in m.items() if k != "industry_name"}
    note = (f"Damodaran '{name}' 산업 평균({region}). "
            + " / ".join(f"{v.label.split(' 산업 ')[-1] if ' 산업 ' in v.label else v.label}: "
                        f"{v.value}{v.unit}" for v in parts.values()))
    return Value(
        value=len(parts), unit="개 지표", label=f"{name} 산업 벤치마크 ({region})",
        provenance=Provenance(source=f"Damodaran ({region} 산업평균)",
                              source_type=SourceType.REFERENCE,
                              source_url=next(iter(parts.values())).provenance.source_url,
                              as_of=next(iter(parts.values())).provenance.as_of, note=note),
        extras=parts,
    )


def _wacc_auto(company: str, country: str = "KR", industry: str | None = None,
               beta_override: float | None = None, cost_of_debt_pct: float | None = None,
               debt_to_value: float | None = None,
               debt_ratio_source: str = "auto", market: str | None = None,
               symbol: str | None = None, risk_free_pct: float | None = None) -> Value:
    # 0 은 "지정 안 함" 으로 본다 — β=0·Kd=0·D/(D+E)=0 은 의미 없는 입력이고, 그대로 통과시키면
    # Rf 와 같은 WACC 가 조용히 나온다(실측: LLM 이 beta_override=0 을 넘겨 WACC 4.32% 산출).
    return wacc_engine.compute_wacc_auto(
        company, country, _blank(industry), "10Y", _pos(beta_override),
        _pos(cost_of_debt_pct), _pos(debt_to_value), _blank(debt_ratio_source) or "auto",
        _blank(market), _blank(symbol), _pos(risk_free_pct))


# ── Skill (절차서) 로딩 ───────────────────────────────────────────
def _text_value(label: str, body: str, note: str, source: str) -> Value:
    """긴 텍스트를 Value 로 감싼다 — 숫자가 아니라 절차서이므로 value 는 길이만 담는다."""
    return Value(
        value=len(body), unit="자", label=label,
        provenance=Provenance(source=source, source_type=SourceType.REFERENCE,
                              source_url="(repo: skills/)", note=note),
        extras={},
        text=body,
    )


def _load_skill(name: str) -> Value:
    s = skills_lib.load(_blank(name) or "")
    refs = ", ".join(s["references"]) or "(없음)"
    return _text_value(
        f"skill: {s['name']}", s["body"],
        f"{s['description']} · 참조 파일: {refs} "
        f"(필요한 것만 read_skill_reference 로 추가로 읽어라)",
        "절차서(skills/)")


def _read_skill_reference(name: str, file: str) -> Value:
    r = skills_lib.reference(_blank(name) or "", _blank(file) or "")
    return _text_value(f"skill: {r['name']} / {r['file']}", r["body"],
                       f"{r['name']} 절차서의 참조 파일 {r['file']}", "절차서(skills/)")


_COUNTRY_PROP = {
    "type": "string",
    "description": "국가 코드 또는 이름. 지원: KR(한국), US(미국), JP(일본), TW(대만).",
}

REGISTRY: dict[str, dict] = {
    "get_equity_risk_premium": {
        "fn": _erp,
        "schema": {
            "name": "get_equity_risk_premium",
            "description": (
                "국가별 주식위험프리미엄(ERP = market risk premium)을 Damodaran 데이터셋에서 "
                "조회한다. WACC 의 자기자본비용(CAPM) 계산에 쓰는 값. "
                "'한국 market risk premium은?', 'ERP' 류 질문에 사용. 단위 %."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"country": _COUNTRY_PROP},
                "required": ["country"],
                "additionalProperties": False,
            },
        },
    },
    "get_country_risk_premium": {
        "fn": _crp,
        "schema": {
            "name": "get_country_risk_premium",
            "description": (
                "국가위험프리미엄(CRP)을 Damodaran 데이터셋에서 조회한다. "
                "성숙시장 대비 해당 국가의 추가 위험프리미엄. 단위 %."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"country": _COUNTRY_PROP},
                "required": ["country"],
                "additionalProperties": False,
            },
        },
    },
    "get_corporate_tax_rate": {
        "fn": _tax,
        "schema": {
            "name": "get_corporate_tax_rate",
            "description": (
                "국가별 법인세율을 Damodaran 데이터셋에서 조회한다. "
                "WACC 의 세후 타인자본비용, DCF 의 세후영업이익 계산에 쓴다. 단위 %."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"country": _COUNTRY_PROP},
                "required": ["country"],
                "additionalProperties": False,
            },
        },
    },
    "get_fx_rate": {
        "fn": _fx,
        "schema": {
            "name": "get_fx_rate",
            "description": (
                "환율(ECB 기준)을 조회한다. 1 단위 base 통화당 quote 통화 금액. "
                "예: base=USD, quote=KRW → 원/달러 환율."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "기준통화 ISO 코드, 예: USD"},
                    "quote": {"type": "string", "description": "표시통화 ISO 코드, 예: KRW"},
                    "date": {
                        "type": "string",
                        "description": "선택. YYYY-MM-DD. 미지정 시 최신 영업일.",
                    },
                },
                "required": ["base", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "get_risk_free_rate": {
        "fn": _rf,
        "schema": {
            "name": "get_risk_free_rate",
            "description": (
                "무위험수익률(국채 수익률)을 조회한다. 한국=한국은행 ECOS 국고채, "
                "미국=FRED 미국채. WACC 의 CAPM 자기자본비용 계산에 쓴다. 단위 %."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "country": {"type": "string", "description": "국가 코드. 현재 KR, US 지원."},
                    "tenor": {
                        "type": "string",
                        "description": "만기. 예: 10Y(기본), 5Y, 3Y, 2Y, 1Y, 20Y, 30Y.",
                    },
                },
                "required": ["country"],
                "additionalProperties": False,
            },
        },
    },
    "get_financial_item": {
        "fn": _fin_item,
        "schema": {
            "name": "get_financial_item",
            "description": (
                "한국 기업의 재무제표 단일 항목을 DART(전자공시)에서 조회한다. "
                "회사명 또는 6자리 종목코드로 조회. 연결(CFS) 우선, 없으면 별도(OFS). 단위 KRW(원). "
                "여러 항목이 필요하면 이 도구를 여러 번(병렬로) 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명(예: 삼성전자) 또는 6자리 종목코드(예: 005930)."},
                    "item": {
                        "type": "string",
                        "enum": ["revenue", "operating_income", "net_income",
                                 "total_assets", "total_liabilities", "total_equity",
                                 "cash", "ppe", "inventories", "trade_receivables",
                                 "trade_payables", "cogs", "sga", "interest_expense",
                                 "tax_expense", "da"],
                        "description": ("항목: revenue(매출액), operating_income(영업이익), "
                                        "net_income(당기순이익), total_assets(자산총계), "
                                        "total_liabilities(부채총계), total_equity(자본총계=순자산), "
                                        "cash(현금및현금성자산), ppe(유형자산), inventories(재고자산), "
                                        "trade_receivables(매출채권), trade_payables(매입채무), "
                                        "cogs(매출원가), sga(판매비와관리비), "
                                        "interest_expense(금융비용), tax_expense(법인세비용), "
                                        "da(감가상각비+무형자산상각비 — 현금흐름표 또는 성격별 분류 "
                                        "주석에서 추출. EBITDA = operating_income + da 이며 "
                                        "직접 더하지 말고 get_ebitda 를 쓰는 게 낫다)."),
                    },
                    "year": {"type": "integer", "description": "사업연도. **생략하면 공시가 존재하는 최신 사업연도**를 자동으로 찾는다 — 최신 값을 원하면 넣지 마라. 특정 과거 연도가 필요할 때만 지정한다."},
                    "report": {
                        "type": "string",
                        "enum": ["annual", "half", "q1", "q3"],
                        "description": "보고서 종류. 기본 annual(사업보고서).",
                    },
                },
                "required": ["company", "item"],
                "additionalProperties": False,
            },
        },
    },
    "search_dart_filings": {
        "fn": _search_filings,
        "schema": {
            "name": "search_dart_filings",
            "description": (
                "[fallback 전용] DART 에 자체 corp_code 가 없는 회사(비상장·해외법인·자체 공시 없는 "
                "계열사)를 조사할 때만 쓴다. get_financial_item 등이 '회사를 못 찾음' 오류를 낸 뒤에만 "
                "사용하고, 평소 조회에는 절대 쓰지 않는다. 그룹 지주사/상위 계열사(예: SK㈜, SK에코플랜트)의 "
                "공시목록을 기간·보고서명 키워드로 검색해 후보 공시를 찾는다. 결과에는 본문 내용이 없으므로 "
                "유망한 rcept_no 를 찾으면 read_dart_filing 으로 본문을 읽어야 한다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "corp": {"type": "string", "description": "지주사/계열사명 또는 8자리 corp_code."},
                    "bgn_de": {"type": "string", "description": "검색시작일 YYYYMMDD."},
                    "end_de": {"type": "string", "description": "검색종료일 YYYYMMDD."},
                    "kw": {"type": "string", "description": "보고서명(report_nm) 필터 키워드(예: '사업보고서'). 선택."},
                },
                "required": ["corp", "bgn_de", "end_de"],
                "additionalProperties": False,
            },
        },
    },
    "read_dart_filing": {
        "fn": _read_filing,
        "schema": {
            "name": "read_dart_filing",
            "description": (
                "[fallback 전용] search_dart_filings 로 찾은 공시(rcept_no)의 원문을 읽는다. "
                "keyword 를 지정하면 그 단어가 등장하는 부분만 앞뒤 문맥과 함께 반환(전체 원문이 아님) — "
                "찾는 회사명을 keyword 로 넣으면 그 회사가 언급된 문단만 뽑을 수 있다. keyword 미지정 시 "
                "문서 앞부분만 반환된다. 여기서 읽은 숫자를 답변에 쓸 때는 반드시 source_type=llm_estimate "
                "로 표시하고 근거 문장을 함께 인용하라(구조화된 XBRL 계정이 아니라 서술문에서 읽은 값이므로)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rcept_no": {"type": "string", "description": "공시 접수번호(search_dart_filings 결과의 rcept_no)."},
                    "keyword": {"type": "string", "description": "찾을 단어(보통 회사명). 지정 권장."},
                },
                "required": ["rcept_no"],
                "additionalProperties": False,
            },
        },
    },
    "search_edinet_filings": {
        "fn": _search_edinet_filings,
        "schema": {
            "name": "search_edinet_filings",
            "description": (
                "[fallback 전용] get_financial_item_jp 가 '회사를 못 찾음' 오류를 낸 뒤에만 사용. "
                "일본 그룹 지주사·상위 계열사의 EDINET 공시목록을 기간으로 검색해 후보 공시(docID)를 "
                "찾는다. doc_type='120' 이면 유가증권보고서만. 결과에는 본문 내용이 없으므로 "
                "read_edinet_filing 으로 원문을 읽어야 한다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "지주사/계열사명(영/일문) 또는 증권코드."},
                    "bgn_de": {"type": "string", "description": "검색시작일 YYYYMMDD."},
                    "end_de": {"type": "string", "description": "검색종료일 YYYYMMDD(기간이 너무 넓으면 에러 — 200일 이내로)."},
                    "doc_type": {"type": "string", "description": "문서유형 코드(예: '120'=유가증권보고서). 선택."},
                },
                "required": ["company", "bgn_de", "end_de"],
                "additionalProperties": False,
            },
        },
    },
    "read_edinet_filing": {
        "fn": _read_edinet_filing,
        "schema": {
            "name": "read_edinet_filing",
            "description": (
                "[fallback 전용] search_edinet_filings 로 찾은 공시(docID)의 서술문(사업의 내용·주석 등)을 "
                "읽는다. keyword 지정 시 그 단어가 등장하는 부분만 앞뒤 문맥과 함께 반환(전체 원문이 "
                "아님) — 찾는 회사명을 keyword 로 넣어라(영문명도 시도). 여기서 읽은 숫자는 반드시 "
                "source_type=llm_estimate 로 표시하고 근거 문장을 인용하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "docid": {"type": "string", "description": "공시 문서ID(search_edinet_filings 결과의 docID)."},
                    "keyword": {"type": "string", "description": "찾을 단어(보통 회사명, 영/일문 모두 시도). 지정 권장."},
                },
                "required": ["docid"],
                "additionalProperties": False,
            },
        },
    },
    "search_sec_filings": {
        "fn": _search_sec_filings,
        "schema": {
            "name": "search_sec_filings",
            "description": (
                "[fallback 전용] get_financial_item_us 가 실패한 뒤에만 사용. EDGAR 전체회사 통합 "
                "원문검색 — 회사를 지정하지 않아도 키워드만으로 전체 공시 대상 검색이 가능하다(DART/"
                "EDINET 과 달리 회사 특정 없이도 넓게 찾을 수 있음). keyword 는 정확한 문구를 찾으려면 "
                "겹따옴표로 감싸라(예: '\"FICT Limited\"'). 결과에는 본문 내용이 없으므로 "
                "read_sec_filing 으로 원문을 읽어야 한다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "검색어. 정확한 문구는 겹따옴표로 감싸기."},
                    "company": {"type": "string", "description": "특정 회사로 좁힐 때 회사명/티커. 선택."},
                    "forms": {"type": "string", "description": "쉼표구분 문서유형(예: '10-K,10-Q'). 선택."},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD. 선택."},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD. 선택."},
                },
                "required": ["keyword"],
                "additionalProperties": False,
            },
        },
    },
    "read_sec_filing": {
        "fn": _read_sec_filing,
        "schema": {
            "name": "read_sec_filing",
            "description": (
                "[fallback 전용] search_sec_filings 결과의 cik/accession/filename 으로 실제 공시 "
                "문서를 읽는다. keyword 지정 시 그 단어가 등장하는 부분만 앞뒤 문맥과 함께 반환(전체 "
                "원문이 아님, 대소문자 무시 검색). 여기서 읽은 숫자는 반드시 source_type=llm_estimate "
                "로 표시하고 근거 문장을 인용하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cik": {"type": "string", "description": "search_sec_filings 결과의 cik."},
                    "accession": {"type": "string", "description": "search_sec_filings 결과의 accession(예: '0001023128-19-000042')."},
                    "filename": {"type": "string", "description": "search_sec_filings 결과의 filename."},
                    "keyword": {"type": "string", "description": "찾을 단어(보통 회사명). 지정 권장."},
                },
                "required": ["cik", "accession", "filename"],
                "additionalProperties": False,
            },
        },
    },
    "get_mops_recent_disclosures": {
        "fn": _mops_recent,
        "schema": {
            "name": "get_mops_recent_disclosures",
            "description": (
                "[fallback 전용] get_financial_item_tw 가 실패한 뒤에만 사용. 대만거래소 MOPS(公開資訊觀測站)의 "
                "重大訊息(공식 공시) 중 '최신 영업일' 것만 조회한다. ⚠️ 과거 날짜 조회는 이 도구로 불가능하다 "
                "(API 자체 한계) — 과거 공시가 필요한 질문이면 이 도구로는 못 찾는다고 솔직히 답하라. "
                "결과의 detail 필드에 공시 전문이 이미 들어있어(별도 원문조회 tool 불필요) 바로 인용하면 된다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "4자리 종목코드(예: '2330')."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_financial_item_us": {
        "fn": _fin_item_us,
        "schema": {
            "name": "get_financial_item_us",
            "description": (
                "미국 상장기업의 재무제표 단일 항목을 SEC EDGAR(XBRL, 10-K 기준)에서 조회한다. "
                "회사명 또는 티커로 조회(예: AAPL, Apple). 단위 USD. "
                f"{_ITEM_DESC} 여러 항목이 필요하면 이 도구를 여러 번(병렬로) 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 티커(예: AAPL)."},
                    "item": {"type": "string", "enum": _ITEM_ENUM, "description": _ITEM_DESC},
                    "year": {"type": "integer",
                             "description": "회계연도. **생략하면 최신 10-K** 를 쓴다 — 최신 값을 "
                                            "원하면 넣지 마라. 과거 연도가 필요할 때만 지정."},
                },
                "required": ["company", "item"],
                "additionalProperties": False,
            },
        },
    },
    "get_financial_item_jp": {
        "fn": _fin_item_jp,
        "schema": {
            "name": "get_financial_item_jp",
            "description": (
                "일본 상장기업의 재무제표 단일 항목을 EDINET(유가증권보고서 XBRL)에서 조회한다. "
                "회사명(영/일문) 또는 증권코드(4~5자리)로 조회(예: 7203, TOYOTA MOTOR). 단위 JPY. "
                "연결(그룹) 기준을 우선하며, 연결 데이터가 없는 항목은 개별(비연결) 기준으로 "
                "대체되고 그 사실이 결과의 label/note 에 명시된다. "
                f"{_ITEM_DESC_JP} 여러 항목이 필요하면 이 도구를 여러 번(병렬로) 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명(영/일문) 또는 증권코드(예: 7203)."},
                    "item": {"type": "string", "enum": _ITEM_ENUM_JP, "description": _ITEM_DESC_JP},
                    "year": {"type": "integer",
                             "description": "결산연도. **생략하면 최신 유가증권보고서**를 쓴다 — "
                                            "최신 값을 원하면 넣지 마라. 3월결산 기업이 많아 "
                                            "회계연도 표기가 한국과 한 해 어긋날 수 있다."},
                },
                "required": ["company", "item"],
                "additionalProperties": False,
            },
        },
    },
    "get_financial_item_tw": {
        "fn": _fin_item_tw,
        "schema": {
            "name": "get_financial_item_tw",
            "description": (
                "대만 상장기업의 재무제표 단일 항목을 FinMind에서 조회한다. "
                "4자리 종목코드 또는 정식 중국어 회사명으로 조회(예: 2330). 영문/한글 회사명은 "
                "지원하지 않으므로 종목코드를 우선 사용하라. 단위 TWD. 손익 항목은 4개 분기 합산이다. "
                f"{_ITEM_DESC} 여러 항목이 필요하면 이 도구를 여러 번(병렬로) 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "4자리 종목코드(예: 2330) 또는 정식 중국어 회사명."},
                    "item": {"type": "string", "enum": _ITEM_ENUM, "description": _ITEM_DESC},
                    "year": {"type": "integer",
                             "description": "회계연도. **생략하면 4개 분기가 모두 존재하는 최신 "
                                            "연도**를 자동으로 찾는다 — 최신 값을 원하면 넣지 마라."},
                },
                "required": ["company", "item"],
                "additionalProperties": False,
            },
        },
    },
    "get_figi": {
        "fn": _figi,
        "schema": {
            "name": "get_figi",
            "description": (
                "종목 식별자(티커/ISIN/CUSIP 등)를 Bloomberg OpenFIGI 로 FIGI 및 회사명·거래소·"
                "증권종류로 매핑한다. 크로스보더 comps 에서 '이 티커와 저 티커가 같은 회사인가' "
                "검증하거나 ISIN을 거래소별 티커로 환산할 때 쓴다. 티커만 주면 여러 거래소에 "
                "복수상장된 경우 모호할 수 있으니 이때는 exch_code 로 좁혀라.\n"
                "⚠️ exch_code 는 **거래소 코드이지 국가 코드가 아니다.** 대만은 TW 가 아니라 "
                "**TT** 다(실측 2026-08-27: TICKER=2330 + exch_code=TT → BBG000BN2JD8 성공, "
                "TW 로는 실패). 자주 쓰는 코드: US(미국), KS(KRX 유가증권), KQ(코스닥), "
                "JT/JP(일본), TT(대만), HK(홍콩), CH(중국), LN(런던), GR(독일).\n"
                "코드 하나 틀렸다고 '종목 식별 실패' 로 결론내지 마라 — 다른 코드나 exch_code "
                "없이 다시 시도하고, 미국 ADR 도 시도하라(TSMC → TICKER=TSM + US → "
                "BBG000BD8ZK0). 그리고 **FIGI 매핑 실패는 밸류에이션 불가 사유가 아니다** — "
                "시가총액·재무는 각 시장 도구(get_market_cap, get_financial_item_*)로 "
                "종목코드만 있으면 바로 조회된다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "id_type": {
                        "type": "string",
                        "enum": ["TICKER", "ID_ISIN", "ID_CUSIP", "ID_SEDOL", "ID_WERTPAPIER", "ID_BB_GLOBAL"],
                        "description": "식별자 종류. 보통 TICKER 또는 ID_ISIN.",
                    },
                    "id_value": {"type": "string", "description": "식별자 값(예: AAPL, US0378331005)."},
                    "exch_code": {"type": "string", "description": "거래소 코드(예: US, KS, JP). 모호할 때 지정."},
                },
                "required": ["id_type", "id_value"],
                "additionalProperties": False,
            },
        },
    },
    "evaluate_sangjeung_value": {
        "fn": _sangjeung,
        "schema": {
            "name": "evaluate_sangjeung_value",
            "description": (
                "한국 기업의 상증법(상속세및증여세법) 보충적 평가방법에 따른 1주당 평가액을 계산한다. "
                "DART 별도재무제표의 3개년 당기순이익·자본총계·발행주식총수로 "
                "순손익가치(가중 3:2, 환원율 10%)와 순자산가치를 조합. 결과는 computed 등급이며 "
                "세무조정·시가평가 미반영 근사임(답변에 이 한계를 반드시 명시). 단위 원/주.\n"
                "법령 판정을 엔진이 자동으로 한다(결과 note 의 [법령판정] 을 그대로 인용하라):\n"
                "· 부동산과다보유법인(상증령 §54①) — 재무상태표의 토지·건물·투자부동산 비중이 "
                "50% 이상이면 가중치를 3:2 에서 **2:3 으로 전환**\n"
                "· 순자산가치 단독평가(상증령 §54④) — 3개년 순손익 부족(사업개시 3년 미만) 또는 "
                "부동산·주식 등이 자산의 80% 이상이면 순자산가치만으로 평가\n"
                "· 최대주주 할증(상증법 §63③) — largest_shareholder=true 면 20% 할증, "
                "sme=true(중소기업)면 할증 제외\n"
                "청산·휴폐업 같은 재무제표로 알 수 없는 사유는 nav_only 로 직접 지정한다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 6자리 종목코드."},
                    "year": {"type": "integer", "description": "평가 기준 사업연도. **생략하면 공시가 존재하는 최신 사업연도**를 자동으로 찾는다 — 최신 값을 원하면 넣지 마라. 특정 과거 연도가 필요할 때만 지정한다."},
                    "real_estate_heavy": {
                        "type": "boolean",
                        "description": "부동산과다보유법인 여부(가중치 2:3). **생략하면 재무상태표 "
                                       "부동산 비중으로 자동 판정한다** — 아는 경우에만 지정.",
                    },
                    "nav_only": {
                        "type": "boolean",
                        "description": "순자산가치 단독평가 강제. 생략하면 자동 판정(순손익 3개년 "
                                       "부족·부동산주식 80% 이상). 청산·휴폐업이면 true 로 지정.",
                    },
                    "largest_shareholder": {
                        "type": "boolean",
                        "description": "최대주주 지분이면 true → 20% 할증(상증법 §63③).",
                    },
                    "sme": {
                        "type": "boolean",
                        "description": "중소기업 등 할증 제외 대상이면 true.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "compute_dcf": {
        "fn": _dcf,
        "schema": {
            "name": "compute_dcf",
            "description": (
                "DCF(UFCF 방식) 주당가치를 계산한다. 기준매출·발행주식수(시장별 공시데이터)·세율(Damodaran)은 "
                "자동 조회하고, 나머지는 사용자가 제시하는 밸류에이션 가정이다: wacc_pct(먼저 compute_wacc로 구해도 됨), "
                "net_debt(순부채=차입금−현금, 현지통화), revenue_growth_pct, ebit_margin_pct, da_pct, capex_pct, "
                "nwc_pct, terminal_growth_pct. 가정이 없으면 지어내지 말고 사용자에게 물어봐라. "
                "market으로 한국(KR·DART)/미국(US·SEC)/일본(JP·EDINET)/대만(TW·FinMind) 기업 모두 계산 가능. "
                "결과 computed, 단위는 시장별 현지통화/주. 주당가치 외에 Enterprise Value·Equity Value도 "
                "extras(enterprise_value, equity_value)로 함께 반환되니 답변에 같이 인용할 것.\n"
                "⛔ **게이트**: 한국 기업이 캡티브 금융 보유(mixed)이거나 순수 금융회사(financial)면 "
                "이 도구가 오류로 차단하고 SOTP 대안을 알려준다 — 우회하지 말고 그 안내를 사용자에게 "
                "전달하라. get_business_mix 로 미리 확인할 수 있다. allow_mixed=true 는 사용자가 "
                "명시적으로 강행을 요청했을 때만 쓰고, 그 경우 결과에 이중 왜곡 경고가 붙는다.\n"
                "⛔ **봉인**: UFCF 전 연도 음수 / 최종연도 UFCF ≤ 0 / EV 음수 / 지분가치 음수 중 "
                "하나라도 걸리면 주당가치를 **null 로 반환**한다(NM). 그때는 숫자를 만들어내지 말고 "
                "note 의 '[산출 불가 · NM]' 사유와 원인 가정을 그대로 전달하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 종목코드."},
                    "market": {
                        "type": "string",
                        "description": "시장 코드. KR(한국,기본값)/US(미국)/JP(일본)/TW(대만).",
                    },
                    "wacc_pct": {"type": "number", "description": "WACC %, 예: 8.5"},
                    "net_debt": {"type": "number", "description": "순부채(현지통화). 순현금이면 음수. 예: -50000000000000"},
                    "revenue_growth_pct": {"type": "number", "description": "연 매출성장률 %(전 기간 동일 적용). 예: 5"},
                    "ebit_margin_pct": {"type": "number", "description": "영업이익률 %. 예: 12"},
                    "da_pct": {"type": "number", "description": "감가상각비(D&A) 매출 대비 %. 예: 8"},
                    "capex_pct": {"type": "number", "description": "Capex 매출 대비 %. 예: 10"},
                    "nwc_pct": {"type": "number", "description": "순운전자본 증가분, 매출증가 대비 %. 예: 3"},
                    "terminal_growth_pct": {"type": "number", "description": "영구성장률 %. WACC보다 작아야 함. 예: 2"},
                    "forecast_years": {"type": "integer", "description": "예측기간(년). 기본 5."},
                    "tax_rate_pct": {"type": "number", "description": "법인세율 %. 미지정 시 Damodaran 해당국 세율."},
                    "year": {"type": "integer", "description": "기준 사업연도. **생략하면 공시가 존재하는 최신 사업연도**를 자동으로 찾는다 — 최신 값을 원하면 넣지 마라. 특정 과거 연도가 필요할 때만 지정한다."},
                    "allow_mixed": {
                        "type": "boolean",
                        "description": "금융부문 게이트를 우회한다. 사용자가 명시적으로 강행을 "
                                       "요청했을 때만 true. 결과에 이중 왜곡 경고가 붙는다.",
                    },
                },
                "required": ["company", "wacc_pct", "net_debt", "revenue_growth_pct",
                             "ebit_margin_pct", "da_pct", "capex_pct", "nwc_pct", "terminal_growth_pct"],
                "additionalProperties": False,
            },
        },
    },
    "compute_comps": {
        "fn": _comps,
        "schema": {
            "name": "compute_comps",
            "description": (
                "Trading comps 비교표를 만든다 — **크로스보더(한국·미국·일본·대만 혼합) 가능**하고 "
                "EV 배수와 자기자본 배수를 함께 낸다: EV/EBITDA, EV/EBIT, EV/Revenue, P/E, P/B.\n"
                "기준 정렬을 엔진이 강제한다: ① 시가총액은 4개 시장의 **공통 거래일** 종가 × 유통 "
                "보통주식수 ② 분모는 각 시장 LTM(최근 12개월) ③ 절대금액만 display_currency 로 환산"
                "(배수는 통화중립이라 환산하지 않음).\n"
                "**타깃 없이 표만 만드는 것이 기본 용도다** — '이 4개사 comps 표 만들어줘' 같은 요청은 "
                "companies 만 넣고 target 은 비운다. target 을 넣으면 median 배수를 적용해 내재 "
                "EV·지분가치·주당가치까지 계산한다.\n"
                "한 회사의 한 항목을 못 구해도 표 전체를 포기하지 않는다 — 그 셀만 미확보로 남기고 "
                "나머지를 계산하며, 결과 note 의 '미확보 항목'·'⚠️' 경고(기준기간 혼용, 결산월 차이, "
                "순부채 정의 차이, 회계기준 차이)를 **답변에 그대로 옮겨 적어야 한다**. "
                "extras 에 회사별 원자료 Value(시총·순부채·영업이익·D&A·순이익·자본)가 들어 있으니 "
                "표의 각 숫자를 그것으로 인용하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "companies": {
                        "type": "array", "items": {"type": "string"},
                        "description": ("비교기업 목록. 해외 종목은 '회사:시장' 으로 시장을 붙인다 "
                                        "(시장: KR/US/JP/TW). 예: "
                                        "['삼성전자','SK하이닉스','MU:US','2330:TW']. "
                                        "미국은 티커(MU), 대만은 4자리 종목코드(2330), "
                                        "일본은 증권코드(7203)를 쓰는 것이 가장 확실하다."),
                    },
                    "target": {
                        "type": "string",
                        "description": ("선택. 평가 대상 회사('회사:시장' 형식 가능). 지정하면 median "
                                        "배수를 적용해 내재 주당가치를 계산한다. 비교표 자체가 "
                                        "산출물이면 비워둔다."),
                    },
                    "market": {
                        "type": "string",
                        "description": "시장 코드를 안 붙인 항목의 기본 시장. KR(기본)/US/JP/TW.",
                    },
                    "as_of": {
                        "type": "string",
                        "description": ("선택. 시가총액 기준일 YYYY-MM-DD. 미지정 시 모든 종목의 "
                                        "공통 최신 거래일을 엔진이 정한다."),
                    },
                    "basis": {
                        "type": "string",
                        "enum": ["LTM", "FY"],
                        "description": ("분모 기준기간. LTM(기본, 최근 12개월) 또는 FY(최근 확정 연간). "
                                        "일본은 LTM 이 불가해 자동으로 FY 가 되고 표에 표시된다."),
                    },
                    "display_currency": {
                        "type": "string",
                        "description": "절대금액 표시통화. 기본 USD. 배수는 환산하지 않는다.",
                    },
                },
                "required": ["companies"],
                "additionalProperties": False,
            },
        },
    },
    "get_financial_history": {
        "fn": _fin_history,
        "schema": {
            "name": "get_financial_history",
            "description": (
                "한 항목의 **최근 N개 회계연도** 시계열을 한 번에 가져온다 — 한국·미국·일본·"
                "대만 모두. 값은 extras 에 연도별로 들어오고, value 는 최신연도 값이다.\n"
                "⭐ **'최근 3개년 재무' 류 요청에는 이 도구를 쓴다.** get_financial_item 을 "
                "연도별로 여러 번 부르지 마라 — 그러면 연도를 직접 찍어야 하고, 그때 낡은 "
                "연도를 넣어 옛 데이터를 가져오는 사고가 난다(실측: 리노공업에 year=2024 를 "
                "넣어 FY2022~2024 를 반환. 실제 최신은 FY2025, 2026-03-18 접수).\n"
                "이 도구는 **연도 인자를 받지 않는다.** 각 시장 provider 가 조회 시점에 공시가 "
                "존재하는 최신 회계연도를 스스로 찾아 거기서 역순으로 내려온다. 결산월이 다른 "
                "회사(미국 8월결산·일본 3월결산)도 각자의 최신 회계연도가 잡히므로, 여러 회사를 "
                "나란히 볼 때는 note 의 회계연도 표기를 그대로 옮겨 기준 차이를 밝혀라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string",
                                "description": "회사명 또는 종목코드/티커(예: 리노공업, MU, 2330, 7203)."},
                    "item": {"type": "string", "enum": _ITEM_ENUM, "description": _ITEM_DESC},
                    "years": {"type": "integer",
                              "description": "가져올 연수. 기본 3, 최대 10. 일본은 유가증권보고서 "
                                             "한 건에 담기는 5개년이 상한이다."},
                    "market": {"type": "string",
                               "description": "시장 코드 KR(기본)/US/JP/TW. 해외면 반드시 지정."},
                },
                "required": ["company", "item"],
                "additionalProperties": False,
            },
        },
    },
    "get_business_mix": {
        "fn": _business_mix,
        "schema": {
            "name": "get_business_mix",
            "description": (
                "이 회사에 **단일 FCFF DCF 를 적용할 수 있는지** 판정한다. 결과는 3분류: "
                "industrial(제조·서비스 단일 실체 → 단일 DCF 가능) / mixed(캡티브 금융 보유 "
                "→ SOTP 필요) / financial(순수 금융회사 → FCFF·EV 개념 불성립, P/B·잔여이익).\n"
                "**한국 기업의 DCF 를 시작하기 전에 이걸 먼저 부른다.** 캡티브 금융이 섞인 회사는 "
                "연결 IBD·운전자본·부채비중에 금융부문이 들어가 WACC 과대 + EV 과다차감의 이중 "
                "왜곡이 생긴다(현대자동차 실측: 주당 −5,042,055원). compute_dcf 가 같은 판정으로 "
                "차단하지만, 미리 확인해서 사용자에게 SOTP 를 제안하는 것이 낫다.\n"
                "판정 근거(금융업 자산비율·손익 구성·사업의 내용 섹션)가 note 에 들어오니 "
                "답변에 그대로 인용하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 6자리 종목코드."},
                    "year": {"type": "integer", "description": "사업연도. 생략하면 최신."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_market_cost_of_debt": {
        "fn": _market_cost_of_debt,
        "schema": {
            "name": "get_market_cost_of_debt",
            "description": (
                "**시장** 세전 타인자본비용(Kd) — 한국은행 ECOS 의 등급별 회사채(3년) 유통수익률. "
                "WACC 에 넣어야 하는 값은 이쪽이다(신규 조달금리).\n"
                "get_cost_of_debt 는 '이자비용 ÷ 차입금' 으로 **과거 조달금리의 가중평균**을 낸다 — "
                "정확한 실효금리지만 신규 조달비용이 아니어서, 저금리 시기에 조달한 회사는 Kd 가 "
                "무위험수익률보다 낮아지는 역전이 생긴다(SK하이닉스 실측 3.79% < Rf 4.288% → "
                "신용스프레드가 음수라는 비논리). 두 값의 괴리와 역전 여부가 note 에 들어온다.\n"
                "등급을 모르면 생략하라 — 이자보상배율(EBIT÷이자비용)로 AA-/BBB- 구간을 고른다. "
                "한국은행 고시 등급이 AA-/BBB- 둘뿐이라 그 사이 등급은 근사다. 한국 기업만 지원."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 6자리 종목코드."},
                    "year": {"type": "integer", "description": "사업연도. 생략하면 최신."},
                    "country": {"type": "string", "description": "국가 코드. 현재 KR 만 지원."},
                    "rating": {
                        "type": "string",
                        "description": "신용등급을 알면 지정: 'AA-' 또는 'BBB-'. 생략 시 "
                                       "이자보상배율로 자동 선택.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_market_cap": {
        "fn": _market_cap,
        "schema": {
            "name": "get_market_cap",
            "description": (
                "시가총액을 조회한다 — **국내외 모두 가능**. 유통 보통주식수 × 기준일 종가로 "
                "계산하며 종가·주식수·기준일을 extras 와 note 에 남긴다.\n"
                "시장별 원천: KR=네이버(KRX 시가총액을 종가로 역산한 유통보통주수), "
                "US=Yahoo 종가 × SEC 발행주식수, TW=Yahoo × FinMind, JP=Yahoo × EDINET.\n"
                "⚠️ 한국 기업의 시가총액에 DART 발행주식총수를 쓰면 안 된다 — 우선주·누적발행분이 "
                "포함돼 실측에서 삼성전자 +53.8%, SK하이닉스 +683% 과대였다. 이 도구가 올바른 "
                "주식수를 쓴다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string",
                                "description": "회사명 또는 종목코드/티커(예: 삼성전자, MU, 2330, 7203)."},
                    "market": {"type": "string",
                               "description": "시장 코드 KR(기본)/US/JP/TW. 해외면 반드시 지정."},
                    "as_of": {"type": "string",
                              "description": "선택. 기준일 YYYY-MM-DD. 그 날짜 이하 최근 거래일 종가를 쓴다."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_ebitda": {
        "fn": _ebitda,
        "schema": {
            "name": "get_ebitda",
            "description": (
                "EBITDA(=영업이익 + D&A)를 계산한다 — 국내외 모두. basis='LTM'(기본)이면 최근 12개월, "
                "'FY'면 최근 확정 연간. 영업이익과 D&A 를 각각 공시에서 뽑아 엔진이 더하고 "
                "각각의 기준기간·출처를 note 와 extras 에 남긴다. "
                "**직접 더하지 말고 이 도구를 쓴다.** 한국 기업은 D&A 가 현금흐름표에 분리돼 있지 "
                "않은 경우(삼성전자·SK하이닉스 실측) 성격별 분류 주석에서 연간값을 쓰게 되며, "
                "그때 note 에 '기준기간 혼용' 경고가 붙으니 답변에 그대로 전달하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 종목코드/티커."},
                    "market": {"type": "string", "description": "시장 코드 KR(기본)/US/JP/TW."},
                    "basis": {"type": "string", "enum": ["LTM", "FY"],
                              "description": "기준기간. 기본 LTM."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "compute_wacc": {
        "fn": _wacc,
        "schema": {
            "name": "compute_wacc",
            "description": (
                "WACC(가중평균자본비용)를 계산한다. Rf(ECOS/FRED)·ERP·세율(Damodaran)은 "
                "자동 조회하고, beta·cost_of_debt_pct·debt_to_value 는 사용자가 제시하는 가정이다. "
                "이 가정들이 없으면 지어내지 말고 사용자에게 물어봐라. 결과는 computed(계산) 등급."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "country": {"type": "string", "description": "국가 코드 (KR 또는 US)."},
                    "beta": {"type": "number", "description": "레버드 베타 (예: 1.1)."},
                    "cost_of_debt_pct": {"type": "number", "description": "세전 타인자본비용 %, 예: 5.0"},
                    "debt_to_value": {"type": "number", "description": "부채비중 D/(D+E), 0~1. 예: 0.3"},
                    "tenor": {"type": "string", "description": "무위험수익률 만기. 기본 10Y."},
                },
                "required": ["country", "beta", "cost_of_debt_pct", "debt_to_value"],
                "additionalProperties": False,
            },
        },
    },

    # ── 절차서(skill) ─────────────────────────────────────────────────────────
    "load_skill": {
        "fn": _load_skill,
        "schema": {
            "name": "load_skill",
            "description": (
                "등록된 작업 절차서(skill)를 읽는다. 절차서는 '어떻게 일할지' 를 정한 문서로, "
                "정식 밸류에이션 보고서처럼 승인 게이트·검증 체크리스트가 필요한 작업에 쓴다. "
                "**사용자가 정식 가치평가·보고서를 요청하면 계산을 시작하기 전에 먼저 이걸 "
                "부른다.** 본문에 참조 파일 목록이 오면 필요한 것만 read_skill_reference 로 "
                "추가로 읽는다(전부 읽지 말 것). 단순 데이터 조회 질문에는 쓰지 않는다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "절차서 이름 (시스템 프롬프트의 목록 참고)."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    "read_skill_reference": {
        "fn": _read_skill_reference,
        "schema": {
            "name": "read_skill_reference",
            "description": (
                "절차서의 참조 파일 하나를 읽는다. load_skill 결과가 가리키는 파일 중 "
                "**지금 단계에 필요한 것만** 읽는다(예: DCF 를 하기로 정했으면 dcf.md 만)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "절차서 이름."},
                    "file": {"type": "string",
                             "description": "참조 파일명 (예: dcf.md, validation.md)."},
                },
                "required": ["name", "file"],
                "additionalProperties": False,
            },
        },
    },

    # ── DCF 입력 자동 도출 (compute_dcf 를 부르기 전에 이걸 먼저 쓴다) ──────────
    "get_net_debt": {
        "fn": _net_debt,
        "schema": {
            "name": "get_net_debt",
            "description": (
                "순부채를 공시에서 자동 계산한다 — **국내외 모두 가능**. 순부채 = 이자발생부채"
                "(단기차입금 + 장기차입금·사채 + 리스부채) − 현금및현금성자산. compute_dcf 의 "
                "net_debt 인자에 그대로 넣으면 된다. 음수면 순현금(net cash) 상태. "
                "**순부채를 사용자에게 묻지 말고 이 도구를 먼저 쓸 것.**\n"
                "시장별 원천: KR=DART(비상장은 감사보고서 파싱), US=SEC XBRL, TW=FinMind. "
                "일본은 차입금 계정 자동추출이 없어 지원하지 않는다(오류로 명확히 알려준다). "
                "정의 차이: 대만 공시에는 리스부채 계정이 없어 IFRS 16 리스부채가 빠지며 그 사실이 "
                "note 에 남으므로 비교표에 표시하라. 단기투자자산은 어느 시장에서도 차감하지 "
                "않는다(정의 통일)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string",
                                "description": "회사명 또는 종목코드/티커(예: 삼성전자, MU, 2330)."},
                    "market": {"type": "string",
                               "description": "시장 코드 KR(기본)/US/TW. 해외면 반드시 지정."},
                    "year": {"type": "integer", "description": "사업연도. 생략하면 최신(해외는 항상 최신)."},
                    "include_lease": {
                        "type": "boolean",
                        "description": "리스부채(IFRS 16) 포함 여부. 기본 true — D&A 에 "
                                       "사용권자산상각비가 포함되므로 일관되게 포함하는 것이 맞다.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_dcf_assumptions": {
        "fn": _dcf_assumptions,
        "schema": {
            "name": "get_dcf_assumptions",
            "description": (
                "최근 5개년 실적에서 DCF 가정의 출발점을 한 번에 도출한다: 매출성장률(YoY 평균 "
                "및 CAGR), 영업이익률(EBIT margin), D&A/매출, CAPEX/매출, ΔNWC/Δ매출. "
                "각 항목은 extras 에 연도별 내역과 함께 담겨 검증 가능하다. "
                "compute_dcf 의 revenue_growth_pct·ebit_margin_pct·da_pct·capex_pct·nwc_pct 에 "
                "대응한다. **이 값들을 사용자에게 묻기 전에 반드시 이 도구를 먼저 쓸 것.** "
                "일부 회사는 특정 항목이 공시에서 안 나올 수 있고, 그 경우 note 에 명시된다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 (한국 기업)."},
                    "n": {"type": "integer", "description": "평균 낼 연수. 기본 5."},
                    "year": {"type": "integer", "description": "기준 사업연도. 생략하면 최신."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_cost_of_debt": {
        "fn": _cost_of_debt,
        "schema": {
            "name": "get_cost_of_debt",
            "description": (
                "세전 타인자본비용(Kd)을 공시에서 계산한다: 현금흐름표의 이자비용 ÷ 이자발생부채. "
                "손익계산서의 '금융비용'은 환차손·파생손실을 포함해 Kd 로 쓸 수 없어(삼성전자 "
                "실측 48.8%) 이자 전용 계정을 쓴다. "
                "무차입 회사는 계산 불가 → 산업평균(get_industry_benchmarks)을 쓸 것.\n"
                "⚠️ 이것은 **실효(과거 가중평균) 조달금리**다. WACC 에 넣을 신규 조달비용은 "
                "get_market_cost_of_debt(ECOS 등급별 회사채)를 쓰고, 이 값은 교차검증에 쓴다 — "
                "저금리 조달분이 남아 있으면 이 값이 무위험수익률보다 낮아진다(실측)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 (한국 기업)."},
                    "year": {"type": "integer", "description": "사업연도. 생략하면 최신."},
                    "include_lease": {"type": "boolean",
                                      "description": "리스부채를 IBD 에 포함. 기본 true."},
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_terminal_growth": {
        "fn": _terminal_growth,
        "schema": {
            "name": "get_terminal_growth",
            "description": (
                "영구성장률 g 의 **권장값과 상한을 분리해서** 돌려준다. "
                "value = 권장 g(장기 물가+실질성장 수준, 한국 2.0%), extras.cap = 상한"
                "(해당 국가 10년 국채수익률, Damodaran 원칙 g ≤ Rf).\n"
                "⚠️ **상한을 g 로 그대로 쓰지 마라.** 예전에는 이 도구가 국채수익률만 돌려줬고 "
                "그 값이 g 로 쓰여서 WACC−g 스프레드가 0.2%p 로 좁아지고 TV 가 EV 의 92% 를 "
                "차지하는 결과가 나왔다(실측). 상한 근처를 쓰려면 근거를 별도로 제시해야 한다. "
                "compute_dcf 의 terminal_growth_pct 에는 value(권장값)를 넣는다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "country": {"type": "string", "description": "국가 코드 (KR 또는 US)."},
                    "tenor": {"type": "string", "description": "국채 만기. 기본 10Y."},
                },
                "required": ["country"],
                "additionalProperties": False,
            },
        },
    },
    "get_beta": {
        "fn": _beta,
        "schema": {
            "name": "get_beta",
            "description": (
                "레버드 베타를 계산한다. 상장사는 네이버 금융(KRX 시세)의 주가·KOSPI 시계열로 "
                "OLS 회귀(기본 5년 주봉)해서 구하고, R²(설명력)를 함께 준다 — R² 가 낮으면 "
                "그 베타는 신뢰도가 낮다는 뜻이다. 비상장사는 industry 를 주면 Damodaran 산업 "
                "무차입베타를 Hamada 식으로 재레버리지해 구한다. "
                "**베타를 사용자에게 묻지 말고 이 도구를 먼저 쓸 것.**"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명."},
                    "industry": {
                        "type": "string",
                        "description": "Damodaran 산업명 (예: Semiconductor, Food Processing). "
                                       "비상장사이거나 회귀가 불가할 때 대체 경로로 쓰인다.",
                    },
                    "country": {"type": "string", "description": "국가 코드. 기본 KR."},
                    "period": {"type": "string",
                               "description": "회귀 주기: day | week | month. 기본 week."},
                    "years": {"type": "integer", "description": "회귀 기간(년). 기본 5."},
                    "market": {
                        "type": "string",
                        "description": "시장: KR(네이버·KOSPI) | US(S&P500) | JP(닛케이225) | "
                                       "TW(TAIEX) | HK(항셍). 생략하면 country 를 따른다.",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "해외 종목의 Yahoo 티커. 예: AAPL, 7203.T(도요타), "
                                       "2330.TW(TSMC). 해외 시장이면 반드시 지정한다.",
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
    "get_industry_benchmarks": {
        "fn": _industry_benchmarks,
        "schema": {
            "name": "get_industry_benchmarks",
            "description": (
                "Damodaran 산업 평균을 조회한다: 무차입베타(unlevered beta), 레버드베타, D/E, "
                "목표 부채비중 D/(D+E), 실효세율. 비상장사 밸류에이션의 베타·자본구조 기준이나 "
                "상장사의 교차검증에 쓴다. 산업명은 부분일치로 찾고, 못 찾으면 비슷한 이름을 "
                "제안한다(예: Semiconductor, Food Processing, Software (Internet))."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "industry": {"type": "string", "description": "Damodaran 산업명."},
                    "country": {"type": "string",
                                "description": "국가 코드. KR/TW→신흥시장, US→미국, JP→글로벌."},
                },
                "required": ["industry"],
                "additionalProperties": False,
            },
        },
    },
    "compute_wacc_auto": {
        "fn": _wacc_auto,
        "schema": {
            "name": "compute_wacc_auto",
            "description": (
                "WACC 를 공시·시세에서 자동으로 구성한다 — β, Kd, D/(D+E), Rf, ERP·세율을 각각 "
                "도출해 조합하고 어느 경로를 썼는지 note 에 전부 남긴다. "
                "**WACC 3대 입력(베타·타인자본비용·부채비중)을 사용자에게 묻기 전에 이 도구를 "
                "먼저 쓸 것.** 특정 입력만 지정하려면 해당 인자만 넘긴다.\n"
                "⚠️ **한국 기업이 아니면 country·market·industry 를 반드시 함께 지정한다.** "
                "DART 공시는 한국 전용이라 해외 기업의 Kd·부채비중은 Damodaran 산업평균으로만 "
                "낼 수 있고, 베타도 Yahoo 티커(symbol)가 있어야 회귀할 수 있다. 지정하지 않으면 "
                "'DART 에서 기업을 못 찾음' 오류가 난다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명."},
                    "country": {"type": "string",
                                "description": "국가 코드 KR/US/JP/TW. 기본 KR. ERP·세율·Rf 선택에 쓴다."},
                    "market": {"type": "string",
                               "description": "상장 시장: KR(네이버·KOSPI) | US | JP | TW | HK. "
                                              "생략하면 country 를 따른다. 해외면 반드시 지정."},
                    "symbol": {"type": "string",
                               "description": "해외 종목의 Yahoo 티커(예: AAPL, 7203.T, 2330.TW). "
                                              "없으면 산업베타로 대체된다."},
                    "industry": {"type": "string",
                                 "description": "Damodaran 산업명(예: Apparel, Semiconductor). "
                                                "해외 기업과 비상장사에는 사실상 필수."},
                    "risk_free_pct": {
                        "type": "number",
                        "description": "무위험수익률 %를 직접 지정. Rf 조회는 KR·US 만 지원하므로 "
                                       "일본·대만 등은 해당 통화 국채수익률을 여기에 넣는다.",
                    },
                    "beta_override": {"type": "number", "description": "베타를 직접 지정."},
                    "cost_of_debt_pct": {"type": "number",
                                         "description": "세전 타인자본비용 %를 직접 지정."},
                    "debt_to_value": {"type": "number",
                                      "description": "목표 부채비중 D/(D+E), 0~1 을 직접 지정."},
                    "debt_ratio_source": {
                        "type": "string",
                        "enum": ["auto", "industry", "spot"],
                        "description": ("부채비중 산출 경로. auto(기본)=industry=Damodaran 산업 "
                                        "median 을 **목표자본구조**로 사용. spot=평가시점 시장가치 "
                                        "레버리지(IBD ÷ (IBD+시가총액)) — 이것은 target 이 아니라 "
                                        "순간값이고, 주가 급등 시점에는 자기자본 비중이 과대해져 "
                                        "WACC 이 구조적으로 높아진다(SK하이닉스 실측 D/V 1.88%). "
                                        "spot 은 교차검증용으로만 쓴다."),
                    },
                },
                "required": ["company"],
                "additionalProperties": False,
            },
        },
    },
}


def tool_schemas() -> list[dict]:
    """Claude messages.create(tools=...) 에 넘길 스키마 목록."""
    return [t["schema"] for t in REGISTRY.values()]


def dispatch(name: str, tool_input: dict) -> dict:
    """tool 실행 → {ok, value(dict) | error} 반환. 예외는 삼키지 않고 error 로 표면화."""
    entry = REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"알 수 없는 tool: {name}"}
    try:
        value: Value = entry["fn"](**tool_input)
        return {"ok": True, "value": value.to_dict()}
    except DataError as e:
        return {"ok": False, "error": f"데이터 조회 실패: {e}"}
    except TypeError as e:
        return {"ok": False, "error": f"인자 오류: {e}"}
    except Exception as e:  # noqa: BLE001 — 어떤 예외도 LLM 이 지어내지 못하게 error 로 전달
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

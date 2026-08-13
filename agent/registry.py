"""Tool 레지스트리 — Claude tool-use 스키마 + provider 디스패치.

새 데이터 소스를 추가하는 = 여기에 tool 하나를 등록하는 것.
LLM 은 "어떤 tool 을 어떤 인자로" 부를지만 정하고, 실제 값은 여기서 provider(코드)가 만든다.
각 tool 결과는 Value.to_dict() (출처·등급 포함) 로 반환되어 UI 와 LLM 양쪽에서 쓰인다.
"""
from __future__ import annotations

from typing import Callable

from core.schema import Value, Provenance, SourceType, DataError
from providers import damodaran, fx, ecos, fred, dart, sec, edinet, finmind, openfigi, mops
from engines import (wacc as wacc_engine, sangjeung as sangjeung_engine,
                     dcf as dcf_engine, comps as comps_engine)

_ITEM_ENUM = ["revenue", "operating_income", "net_income",
             "total_assets", "total_liabilities", "total_equity"]
_ITEM_DESC = ("항목: revenue(매출액), operating_income(영업이익), net_income(당기순이익), "
             "total_assets(자산총계), total_liabilities(부채총계), total_equity(자본총계).")

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
               real_estate_heavy: bool = False, report: str = "annual") -> Value:
    return sangjeung_engine.evaluate(company, year, real_estate_heavy, report)

def _dcf(company: str, wacc_pct: float, net_debt: float, revenue_growth_pct: float,
         ebit_margin_pct: float, da_pct: float, capex_pct: float, nwc_pct: float,
         terminal_growth_pct: float, forecast_years: int = 5,
         tax_rate_pct: float | None = None, year: int | None = None,
         market: str = "KR") -> Value:
    return dcf_engine.evaluate(company, wacc_pct, net_debt, revenue_growth_pct,
                               ebit_margin_pct, da_pct, capex_pct, nwc_pct,
                               terminal_growth_pct, forecast_years, tax_rate_pct, year,
                               market)

def _comps(target: str, peers: list, year: int | None = None) -> Value:
    return comps_engine.evaluate(target, peers, year)


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
                                 "total_assets", "total_liabilities", "total_equity"],
                        "description": ("항목: revenue(매출액), operating_income(영업이익), "
                                        "net_income(당기순이익), total_assets(자산총계), "
                                        "total_liabilities(부채총계), total_equity(자본총계=순자산)."),
                    },
                    "year": {"type": "integer", "description": "사업연도(예: 2024). 미지정 시 직전연도."},
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
                    "year": {"type": "integer", "description": "회계연도(예: 2024). 미지정 시 최신 10-K."},
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
                f"{_ITEM_DESC} 여러 항목이 필요하면 이 도구를 여러 번(병렬로) 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명(영/일문) 또는 증권코드(예: 7203)."},
                    "item": {"type": "string", "enum": _ITEM_ENUM, "description": _ITEM_DESC},
                    "year": {"type": "integer", "description": "결산연도(예: 2024). 미지정 시 최신 유가증권보고서."},
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
                    "year": {"type": "integer", "description": "회계연도(예: 2023). 미지정 시 4개 분기가 모두 존재하는 최신연도."},
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
                "복수상장된 경우 모호할 수 있으니 이때는 exch_code(예: US, KS, JP)로 좁혀라."
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
                "순손익가치(3:2 가중, 환원율 10%)와 순자산가치를 조합. 결과는 computed 등급이며 "
                "세무조정·시가평가 미반영 근사임(답변에 이 한계를 반드시 명시). 단위 원/주."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "회사명 또는 6자리 종목코드."},
                    "year": {"type": "integer", "description": "평가 기준 사업연도(예: 2024). 미지정 시 직전연도."},
                    "real_estate_heavy": {
                        "type": "boolean",
                        "description": "부동산과다보유법인이면 true (가중치 2:3 적용). 기본 false(3:2).",
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
                "extras(enterprise_value, equity_value)로 함께 반환되니 답변에 같이 인용할 것."
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
                    "year": {"type": "integer", "description": "기준 사업연도. 미지정 시 직전연도."},
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
                "Trading comps(자기자본배수)로 타깃의 주당가치를 추정한다. peer들의 PER(=시총/순이익)·"
                "PBR(=시총/자본)의 중앙값을 타깃 당기순이익·자본총계에 적용. 시가총액=네이버(KRX 시세), "
                "재무=DART. peer는 반드시 상장사여야 한다(비상장은 시총 없어 제외). 결과 computed, 원/주."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "평가 대상 회사명 또는 종목코드."},
                    "peers": {
                        "type": "array", "items": {"type": "string"},
                        "description": "비교 상장사 목록(회사명 또는 종목코드). 예: ['SK하이닉스','삼성전자'].",
                    },
                    "year": {"type": "integer", "description": "기준 사업연도. 미지정 시 직전연도."},
                },
                "required": ["target", "peers"],
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

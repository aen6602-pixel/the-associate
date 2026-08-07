"""Tool 레지스트리 — Claude tool-use 스키마 + provider 디스패치.

새 데이터 소스를 추가하는 = 여기에 tool 하나를 등록하는 것.
LLM 은 "어떤 tool 을 어떤 인자로" 부를지만 정하고, 실제 값은 여기서 provider(코드)가 만든다.
각 tool 결과는 Value.to_dict() (출처·등급 포함) 로 반환되어 UI 와 LLM 양쪽에서 쓰인다.
"""
from __future__ import annotations

from typing import Callable

from core.schema import Value, DataError
from providers import damodaran, fx, ecos, fred, dart
from engines import wacc as wacc_engine

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

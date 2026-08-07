"""핵심 데이터 스키마 — provenance(출처)를 1급 필드로 강제한다.

이 프로젝트의 불변 원칙:
  - 모든 숫자는 반드시 Value 로 감싸서 출처(Provenance)를 달고 다닌다.
  - LLM 은 숫자를 만들지 않는다. provider(코드)가 원본에서 뽑거나 engine(코드)이 계산한다.
  - "이 숫자 어디서 나왔냐"에 3초 안에 답할 수 있어야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceType:
    """소스 등급 — "이 숫자가 공신력 기관에서 왔나, LLM 추정인가"를 명시.

    사용자 원칙: 1순위는 공신력 기관 API. 없으면 참조 데이터셋. 그래도 없을 때만
    LLM 추정이며, 그 경우 반드시 llm_estimate 로 라벨해 답변에서 구분되게 한다.
    """

    AUTHORITATIVE = "authoritative"   # 정부·규제기관·중앙은행·거래소 공식 (DART, SEC, ECOS, FRED, KRX, EDINET)
    REFERENCE = "reference"           # 업계 표준 참조 데이터셋 (Damodaran 등)
    COMPUTED = "computed"             # 우리 엔진이 다른 Value 로부터 계산 (WACC, EV, 멀티플)
    LLM_ESTIMATE = "llm_estimate"     # 소스가 없어 LLM 이 추정 — 반드시 이 라벨

    ALL = {AUTHORITATIVE, REFERENCE, COMPUTED, LLM_ESTIMATE}


@dataclass
class Provenance:
    """숫자 하나의 출처. 감사 추적(audit trail)의 최소 단위."""

    source: str                       # "Damodaran", "DART", "FRED", "ECOS", "KRX", ...
    source_url: str                   # 원본 문서/엔드포인트 URL
    source_type: str = SourceType.AUTHORITATIVE  # 위 SourceType 중 하나
    retrieved_at: str = field(default_factory=now_iso)  # 우리가 조회한 시각(UTC)
    original_field: Optional[str] = None   # 원본에서의 계정명/필드명 (예: "Total Equity Risk Premium")
    as_of: Optional[str] = None            # 값이 가리키는 기준일/기간 (예: "2026-07", "FY2024")
    filing_date: Optional[str] = None      # point-in-time: 공시일 (있으면)
    note: Optional[str] = None

    def __post_init__(self):
        if self.source_type not in SourceType.ALL:
            raise ValueError(f"알 수 없는 source_type: {self.source_type}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Value:
    """provenance 를 달고 다니는 단일 수치.

    unit 예: "%", "ratio", "x"(멀티플), "KRW", "USD", "JPY", "TWD",
             "shares", "KRW_mn", "days" 등.
    """

    value: Any
    unit: str
    provenance: Provenance
    label: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "label": self.label,
            "provenance": self.provenance.to_dict(),
        }

    def __repr__(self) -> str:
        lbl = f"{self.label}=" if self.label else ""
        return f"<Value {lbl}{self.value} {self.unit} src={self.provenance.source}>"


class DataError(Exception):
    """provider 가 데이터를 못 찾거나 소스가 응답하지 않을 때. LLM 이 지어내지 못하도록
    반드시 예외로 올린다 (조용히 None/0 을 돌려주지 않는다)."""

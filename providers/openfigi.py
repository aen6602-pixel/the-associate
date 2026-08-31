"""OpenFIGI provider (Bloomberg 종목 식별자 매핑) — reference.

- POST /v3/mapping: 여러 idType/idValue 조합(최대 100건/요청, 실측 확인 — 101건부터 413)을
  한 번에 FIGI(Financial Instrument Global Identifier)로 매핑. 티커만으로는 상장시장이
  여러 곳일 수 있어(예: AAPL — US/독일/스위스 등 수십 개 복수상장) exchCode 로 좁히거나,
  ISIN처럼 종목 자체를 특정하는 식별자를 쓰지 않으면 모호(ambiguous)할 수 있다.
- 매칭 실패 시 응답이 {"data":[...]} 대신 {"warning": "No identifier found."} 형태로 옴
  (실측 확인, {"error":...} 형태도 방어적으로 함께 처리).
- 크로스보더 comps 에서 "이 티커와 저 티커가 같은 회사인가"를 검증하거나, ISIN/CUSIP 를
  거래소별 티커로 환산할 때 사용. 재무 수치를 만들지 않는 순수 식별자 매핑이라 tier=reference.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from core.http import post_json
from core import config

_URL = "https://api.openfigi.com/v3/mapping"
_MAX_JOBS_PER_REQUEST = 100  # 실측 확인(2026-08): 101건부터 413 Payload Too Large

ID_TYPES = ["TICKER", "ID_ISIN", "ID_CUSIP", "ID_SEDOL", "ID_WERTPAPIER", "ID_BB_GLOBAL"]


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = config.Keys.OPENFIGI
    if key:  # 키 없어도 동작하나(무료 등록 없이도 가능) rate-limit 이 훨씬 낮음
        h["X-OPENFIGI-APIKEY"] = key
    return h


def _post(jobs: list[dict]) -> list[dict]:
    if not jobs:
        return []
    if len(jobs) > _MAX_JOBS_PER_REQUEST:
        raise DataError(f"OpenFIGI 요청당 최대 {_MAX_JOBS_PER_REQUEST}건까지 매핑 가능"
                        f"({len(jobs)}건 요청됨 — 나눠서 호출하세요)")
    return post_json(_URL, jobs, ttl_hours=24 * 7, headers=_headers())


def _job(id_type: str, id_value: str, exch_code: str | None = None,
         mic_code: str | None = None, currency: str | None = None) -> dict:
    if id_type not in ID_TYPES:
        raise DataError(f"지원하지 않는 idType: {id_type}. 지원: {ID_TYPES}")
    job = {"idType": id_type, "idValue": id_value}
    if exch_code:
        job["exchCode"] = exch_code
    if mic_code:
        job["micCode"] = mic_code
    if currency:
        job["currency"] = currency
    return job


def map_one(id_type: str, id_value: str, exch_code: str | None = None,
            mic_code: str | None = None, currency: str | None = None) -> list[dict]:
    """단일 식별자 → 매칭된 원본 레코드 목록(여러 상장시장이면 여러 건).
    각 레코드: figi, name, ticker, exchCode, compositeFIGI, securityType, marketSector 등."""
    job = _job(id_type, id_value, exch_code, mic_code, currency)
    results = _post([job])
    r = results[0]
    data = r.get("data")
    if not data:
        msg = r.get("warning") or r.get("error") or "매칭 결과 없음"
        raise DataError(f"OpenFIGI: {id_type}={id_value} 매핑 실패: {msg}")
    return data


def map_batch(jobs: list[dict]) -> list[list[dict] | None]:
    """여러 건 한 번에 매핑(요청당 최대 100건). 항목별 job={'id_type','id_value','exch_code'(선택)}.
    매칭 실패한 항목은 None (예외를 던지지 않음 — 대량 매핑 중 일부 실패를 허용)."""
    built = [_job(j["id_type"], j["id_value"], j.get("exch_code"), j.get("mic_code"), j.get("currency"))
            for j in jobs]
    results = _post(built)
    return [r.get("data") or None for r in results]


def figi(id_type: str, id_value: str, exch_code: str | None = None,
         mic_code: str | None = None) -> Value:
    """단일 FIGI 조회(레지스트리 tool 용). 여러 상장시장이 매칭돼 모호하면
    exch_code 로 좁혀야 하며, 그래도 모호하면 후보를 제시하고 에러."""
    rows = map_one(id_type, id_value, exch_code, mic_code)
    if len(rows) > 1:
        cands = ", ".join(f"{r.get('ticker')}@{r.get('exchCode')}" for r in rows[:10])
        raise DataError(f"OpenFIGI: '{id_value}' 결과가 {len(rows)}건으로 모호함 "
                        f"(exch_code 로 특정 필요). 후보: {cands}"
                        + (" ..." if len(rows) > 10 else ""))
    r = rows[0]
    note = (f"securityType={r.get('securityType')}, marketSector={r.get('marketSector')}, "
            f"compositeFIGI={r.get('compositeFIGI')}, shareClassFIGI={r.get('shareClassFIGI')}")
    return Value(
        value=r["figi"], unit="FIGI",
        label=f"{r.get('name')} ({r.get('ticker')}@{r.get('exchCode')}) FIGI",
        provenance=Provenance(
            source="OpenFIGI (Bloomberg)", source_type=SourceType.REFERENCE,
            source_url="https://www.openfigi.com/api/documentation",
            original_field=f"idType={id_type}, idValue={id_value}"
                          + (f", exchCode={exch_code}" if exch_code else ""),
            note=note,
        ),
    )


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    j = probe("POST", _URL, headers=_headers(),
              json_body=[{"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}]).json()
    if not (isinstance(j, list) and j and (j[0].get("data") or j[0].get("warning"))):
        raise DataError(f"OpenFIGI 응답이 예상과 다릅니다: {str(j)[:120]}")
    return "식별자 매핑 OK"

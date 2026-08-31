"""대만거래소 MOPS(公開資訊觀測站) provider — 상장사 공식 공시(重大訊息). authoritative.

FinMind 는 재무데이터 API 일 뿐 원문 공시가 없다(뉴스 크롤링만 있고 공식 공시 아님) — 대만
공식 공시는 이 provider 가 다룬다.

실측 확인(2026-08):
- `openapi.twse.com.tw/v1/opendata/t187ap04_L`(上市公司每日重大訊息): keyless, 공시 전문이
  "說明" 필드에 그대로 들어있음 — 확인됨, 정상 동작.
  ⚠️ 단, date 류 파라미터를 무시하고 **항상 최신 영업일만** 반환한다(실측: 여러 날짜를
  넘겨도 결과 동일) — 과거 조회는 이 API 로 불가능, "최근 공시"용으로만 쓴다.
- **과거 날짜 포함 키워드 검색**(`mops.twse.com.tw/mops/web/ajax_t51sb10` +
  `ajax_t05st01`, 공개된 R 스크래퍼 레퍼런스 기반)은 이 환경에서 시도했으나 User-Agent·
  Referer·쿠키·X-Requested-With 등 여러 조합을 다 시도해도 계속 WAF 차단 페이지("FOR
  SECURITY REASONS THIS PAGE CAN NOT BE ACCESSED")만 돌아와 구현하지 않았다(파라미터
  문제가 아니라 이 환경의 발신 IP 자체가 막히는 것으로 보임) — DART/EDINET/SEC 와 달리
  대만은 "최근 공시"만 가능하고 과거 전체기간 검색은 지금은 제공하지 않는다.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from core.http import get_json

_RECENT_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"


def recent_disclosures(company: str | None = None, keyword: str | None = None,
                       max_results: int = 20) -> dict:
    """상장사 重大訊息(최신 영업일 전체, keyless). company: 4자리 종목코드(예: '2330').
    keyword 지정 시 주제(主旨)·설명(說明)에 포함된 것만. 과거 날짜 지정은 불가(API 자체 한계 —
    항상 최신 영업일만 반환, 실측 확인)."""
    rows = get_json(_RECENT_URL, ttl_hours=1)
    if company:
        rows = [r for r in rows if (r.get("公司代號") or "").strip() == company.strip()]
    if keyword:
        kw = keyword.lower()
        rows = [r for r in rows
               if kw in (r.get("主旨 ") or r.get("主旨") or "").lower()
               or kw in (r.get("說明") or "").lower()]
    out = []
    for r in rows[:max_results]:
        out.append({
            "stock_id": r.get("公司代號"), "stock_name": r.get("公司名稱"),
            "subject": (r.get("主旨 ") or r.get("主旨") or "").strip(),
            "announce_date": r.get("發言日期"), "fact_date": r.get("事實發生日"),
            "detail": r.get("說明"),
        })
    return {
        "matched": len(rows), "returned": len(out), "results": out,
        "note": ("이 API 는 항상 '최신 영업일' 공시만 반환한다(과거 날짜 조회 불가, 실측 확인). "
                "과거 공시가 필요하면 이 도구로는 찾을 수 없다고 솔직히 답하라."),
    }


def recent_disclosures_value(company: str) -> Value:
    """레지스트리 tool 용 래퍼 — Value 로 감싸 출처 주석을 붙인다."""
    result = recent_disclosures(company=company)
    return Value(
        value=result, unit="disclosure_list",
        label=f"{company} 최근 重大訊息(최신 영업일)",
        provenance=Provenance(
            source="대만거래소 MOPS(公開資訊觀測站) OpenAPI", source_type=SourceType.AUTHORITATIVE,
            source_url="https://mops.twse.com.tw", original_field="t187ap04_L",
            note="keyless. 최신 영업일 공시만 — 과거 날짜 조회는 이 API 로 불가능(실측 확인).",
        ),
    )


# ── 헬스체크 ──────────────────────────────────────────────────────
def ping() -> str:
    from core.http import probe

    j = probe("GET", _RECENT_URL).json()
    if not isinstance(j, list):
        raise DataError("MOPS 응답이 목록 형식이 아닙니다")
    return f"최신 영업일 공시 {len(j)}건"

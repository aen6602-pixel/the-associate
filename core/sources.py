"""데이터 소스 카탈로그 — UI(사이드바)와 향후 '어떤 소스를 쓰나' 질의에 공용.

각 소스가 무엇을 주는지·등급·연결상태를 한 곳에서 관리한다.
wired=False 는 키/계획만 있고 아직 provider 로 연동 안 된 소스.
"""
from __future__ import annotations

from core import config

# tier: authoritative(공식) | reference(참조)
SOURCES: list[dict] = [
    {
        "name": "DART", "org": "금융감독원 전자공시", "tier": "authoritative",
        "key_attr": "DART", "wired": True,
        "provides": "한국 기업 공시·재무제표(상장 정기보고서 + 비상장 감사보고서 파싱)·발행주식수",
        "used_by": "재무조회 · 상증법 · DCF · Comps",
        "url": "https://opendart.fss.or.kr",
        "note": "비상장은 감사보고서 원문을 파싱해 순손익·자본총계·주식수를 추출.",
    },
    {
        "name": "ECOS", "org": "한국은행 경제통계시스템", "tier": "authoritative",
        "key_attr": "ECOS", "wired": True,
        "provides": "국고채 수익률(무위험수익률 Rf, 만기별)",
        "used_by": "WACC(한국) · 무위험수익률 조회",
        "url": "https://ecos.bok.or.kr",
        "note": "회사채 금리도 있어 향후 타인자본비용(Kd)에 활용 예정.",
    },
    {
        "name": "FRED", "org": "美 세인트루이스 연은", "tier": "authoritative",
        "key_attr": "FRED", "wired": True,
        "provides": "미국 국채 수익률(Rf, 만기별)·매크로 지표",
        "used_by": "WACC(미국) · 무위험수익률 조회",
        "url": "https://fred.stlouisfed.org",
        "note": None,
    },
    {
        "name": "Damodaran", "org": "NYU Stern (Aswath Damodaran)", "tier": "reference",
        "key_attr": None, "wired": True,
        "provides": "국가별 ERP(주식위험프리미엄)·국가위험프리미엄(CRP)·법인세율",
        "used_by": "WACC · CAPM 자기자본비용",
        "url": "https://pages.stern.nyu.edu/~adamodar/",
        "note": "정부 API가 없는 프리미엄 영역이라 업계표준 데이터셋(참조 등급) 사용.",
    },
    {
        "name": "ECB", "org": "유럽중앙은행 (frankfurter.app)", "tier": "authoritative",
        "key_attr": None, "wired": True,
        "provides": "기준환율(영업일 종가)",
        "used_by": "환율 조회",
        "url": "https://www.ecb.europa.eu",
        "note": None,
    },
    {
        "name": "Naver 금융", "org": "네이버 금융 (KRX 시세 집계)", "tier": "authoritative",
        "key_attr": None, "wired": True,
        "provides": "상장사 시가총액·종가",
        "used_by": "Comps(PER·PBR)",
        "url": "https://m.stock.naver.com",
        "note": "사내망이 KRX(data.krx) 직접 접근을 막아 네이버 경유로 우회.",
    },
    {
        "name": "SEC EDGAR", "org": "美 증권거래위원회", "tier": "authoritative",
        "key_attr": None, "wired": True,
        "provides": "미국 상장사 공시·재무제표(10-K XBRL)·발행주식수",
        "used_by": "재무조회(미국 기업)",
        "url": "https://www.sec.gov/edgar",
        "note": "키 불필요(연락처 User-Agent만). 매출 태그는 회사·시기별로 달라 후보 태그를 순차 탐색.",
    },
    {
        "name": "EDINET", "org": "日 금융청 전자공시", "tier": "authoritative",
        "key_attr": "EDINET", "wired": True,
        "provides": "일본 기업 공시·유가증권보고서(XBRL)·발행주식수",
        "used_by": "재무조회(일본 기업)",
        "url": "https://disclosure2.edinet-fsa.go.jp",
        "note": "연결 기준 우선, 없으면 개별(비연결) 기준으로 대체(결과에 명시).",
    },
    {
        "name": "FinMind", "org": "대만 시장 데이터", "tier": "reference",
        "key_attr": "FINMIND", "wired": True,
        "provides": "대만 상장사 재무·발행주식수",
        "used_by": "재무조회(대만 기업)",
        "url": "https://finmindtrade.com",
        "note": "손익 항목은 분기값 합산(누적 아님). 종목명은 중국어만 지원(영/한글명 매핑 없음).",
    },
    {
        "name": "OpenFIGI", "org": "Bloomberg OpenFIGI", "tier": "reference",
        "key_attr": "OPENFIGI", "wired": True,
        "provides": "종목 식별자 매핑(티커·ISIN·CUSIP ↔ FIGI)",
        "used_by": "크로스보더 comps 종목 매칭",
        "url": "https://www.openfigi.com/api",
        "note": "티커만 주면 복수상장으로 모호할 수 있어 exch_code 로 특정 필요.",
    },
]


# 연동 계획이 아직 없는 "로드맵"용 참고 목록 — SOURCES 와 달리 key_attr/provider 가 없다.
# 사이드바에 "앞으로 추가하면 좋을 데이터"로만 참고 표시한다.
ROADMAP: list[dict] = [
    {
        "name": "S&P Capital IQ", "org": "S&P Global",
        "provides": "정밀 트레이딩·트랜잭션 comps, 애널리스트 컨센서스, 신용등급 연계 데이터",
    },
    {
        "name": "PitchBook", "org": "PitchBook Data",
        "provides": "PE/VC 딜 comps, 비상장기업 밸류에이션 벤치마크 — SK Square 의 PE/VC 포트폴리오 평가에 특히 유용",
    },
    {
        "name": "Gartner", "org": "Gartner",
        "provides": "IT·반도체·클라우드 등 업종 시장전망 리포트 (그룹 포트폴리오 업종 딥다이브)",
    },
    {
        "name": "한국신용평가·NICE신용평가", "org": "국내 신용평가사",
        "provides": "회사채 등급 스프레드 등",
    },
    {
        "name": "Bloomberg (Terminal/BQL)", "org": "Bloomberg",
        "provides": "크로스에셋 실시간 시세, 애널리스트 컨센서스, 신용스프레드",
    },
    {
        "name": "LSEG Workspace (Refinitiv Eikon)", "org": "LSEG",
        "provides": "I/B/E/S 애널리스트 컨센서스 추정치 — Bloomberg 대체/보완",
    },
    {
        "name": "KIND (한국거래소 기업공시채널)", "org": "한국거래소",
        "provides": "상장기업 IR 자료·실적발표 프레젠테이션 (DART 정기공시 외 정성 자료)",
    },
]


def status(s: dict) -> tuple[str, str]:
    """(code, label). code: live | nokey | planned."""
    if not s["wired"]:
        return ("planned", "🔜 예정")
    if s["key_attr"] is None:
        return ("live", "✅ 연결")
    key = getattr(config.Keys, s["key_attr"], None)
    return ("live", "✅ 연결") if key else ("nokey", "⬜ 키 필요")


def tier_icon(tier: str) -> str:
    return {"authoritative": "🟢", "reference": "🔵"}.get(tier, "⚪")

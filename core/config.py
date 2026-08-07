"""환경설정 — API 키는 전부 .env 에서 읽는다 (코드에 하드코딩 금지)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 사내 TLS 검사(프록시) 환경 대응: Python 이 OS(Windows) 인증서 저장소를 쓰도록 주입.
# 이렇게 하면 사내 루트 CA 로 서명된 프록시 인증서를 신뢰하게 되어 requests 가 동작한다.
try:
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


class Keys:
    """무료 키(대부분 무료 등록). 없으면 해당 provider 만 비활성화되고 나머지는 동작."""

    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY")     # 에이전트 두뇌 (Claude)
    GEMINI = os.getenv("GEMINI_API_KEY")           # 에이전트 두뇌 (Google Gemini, 무료티어)
    DART = os.getenv("DART_API_KEY")               # 한국 공시/재무 (opendart.fss.or.kr)
    ECOS = os.getenv("ECOS_API_KEY")               # 한국은행 (국고채, 매크로)
    FRED = os.getenv("FRED_API_KEY")               # 미국 무위험이자율/매크로
    EDINET = os.getenv("EDINET_API_KEY")           # 일본 공시
    FINMIND = os.getenv("FINMIND_TOKEN")           # 대만 재무/주가
    OPENFIGI = os.getenv("OPENFIGI_API_KEY")       # 식별자 매핑(옵션)


# SEC 는 키 불필요, 단 User-Agent 로 연락처를 요구한다.
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "sksq-agent sanghwalee@sksquare.com")

# ── 에이전트 두뇌(LLM) 선택 ────────────────────────────────────────
# LLM 은 "어떤 tool 을 부를지"만 정한다. gemini / anthropic 전환 가능.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def active_llm() -> dict:
    """현재 선택된 LLM 의 {provider, model, key, key_name} 반환."""
    if LLM_PROVIDER == "anthropic":
        return {"provider": "anthropic", "model": ANTHROPIC_MODEL,
                "key": Keys.ANTHROPIC, "key_name": "ANTHROPIC_API_KEY"}
    # 기본: gemini
    return {"provider": "gemini", "model": GEMINI_MODEL,
            "key": Keys.GEMINI, "key_name": "GEMINI_API_KEY"}


def require(key_value: str | None, name: str) -> str:
    from .schema import DataError

    if not key_value:
        raise DataError(
            f"{name} 키가 설정되지 않았습니다. .env 에 {name} 를 추가하세요 "
            f"(.env.example 참고)."
        )
    return key_value

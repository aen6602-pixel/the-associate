"""환경설정 — API 키는 전부 .env 에서 읽는다 (코드에 하드코딩 금지)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

# 사내 TLS 검사(프록시) 환경 대응: Python 이 OS(Windows) 인증서 저장소를 쓰도록 주입.
# 이렇게 하면 사내 루트 CA 로 서명된 프록시 인증서를 신뢰하게 되어 requests 가 동작한다.
try:
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:
    pass

# 경로는 core.paths 가 결정한다 (배포 시 DATA_DIR 볼륨, 로컬은 리포지토리 폴더).
# 기존 import 를 깨지 않도록 ROOT/CACHE_DIR 이름은 그대로 재노출한다.
from .paths import CACHE_DIR, DATA_DIR, IS_PERSISTENT, ROOT  # noqa: F401

load_dotenv(ROOT / ".env")


class Keys:
    """무료 키(대부분 무료 등록). 없으면 해당 provider 만 비활성화되고 나머지는 동작."""

    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY")     # 에이전트 두뇌 (Claude)
    GEMINI = os.getenv("GEMINI_API_KEY")           # 에이전트 두뇌 (Google Gemini, 무료티어)
    OPENAI = os.getenv("OPENAI_API_KEY")           # 에이전트 두뇌 (OpenAI GPT)
    DART = os.getenv("DART_API_KEY")               # 한국 공시/재무 (opendart.fss.or.kr)
    ECOS = os.getenv("ECOS_API_KEY")               # 한국은행 (국고채, 매크로)
    FRED = os.getenv("FRED_API_KEY")               # 미국 무위험이자율/매크로
    EDINET = os.getenv("EDINET_API_KEY")           # 일본 공시
    FINMIND = os.getenv("FINMIND_TOKEN")           # 대만 재무/주가
    OPENFIGI = os.getenv("OPENFIGI_API_KEY")       # 식별자 매핑(옵션)


# SEC 는 키 불필요, 단 User-Agent 로 연락처를 요구한다.
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "sksq-agent sanghwalee@sksquare.com")

# ── 에이전트 두뇌(LLM) 선택 ────────────────────────────────────────
# LLM 은 "어떤 tool 을 부를지"만 정한다. UI(사이드바)에서 실행 중에도 전환 가능.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
# 배포(클라우드) 여부 — claude CLI 두뇌 숨김 등 배포 전용 동작 토글. 시크릿/env 로 DEPLOY_MODE=1.
DEPLOY_MODE = os.getenv("DEPLOY_MODE", "").strip().lower() in ("1", "true", "yes")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")

# provider → (표시명, key_attr, .env 모델변수명, 기본모델, 프리셋 모델 목록)
# 프리셋은 참고용 후보일 뿐 — UI 에서 자유 텍스트로 다른 모델 ID 도 입력 가능.
LLM_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini", "key_attr": "GEMINI",
        "env_model_var": "GEMINI_MODEL", "default_model": GEMINI_MODEL,
        "presets": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "openai": {
        "label": "OpenAI GPT", "key_attr": "OPENAI",
        "env_model_var": "OPENAI_MODEL", "default_model": OPENAI_MODEL,
        # 2026-08: gpt-5-nano 로 실제 조사성 질문(SK트리켐 케이스)에서 회사명을 스스로
        # 지어내는 tool-calling 오류를 확인 — GPT-5.6 Terra 로 교체(Sol 대비 훨씬 저렴하면서
        # 성능은 근접, Luna 는 'low-reasoning' 명시라 이런 다단계 조사엔 부적합).
        "presets": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna",
                   "gpt-5-nano", "gpt-4o-mini", "gpt-5-mini", "gpt-5", "gpt-4.1-mini"],
    },
    "anthropic": {
        # API 키가 아니라 로그인된 Claude Code CLI(Enterprise 구독)를 서브프로세스로 재활용한다
        # (dart-agent/Martin's Bullseye 와 동일 방식) — 그래서 key_attr 이 없다.
        "label": "Anthropic Claude (CLI)", "key_attr": None, "auth_mode": "cli",
        "env_model_var": "ANTHROPIC_MODEL", "default_model": ANTHROPIC_MODEL,
        "presets": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"],
    },
}


def resolve_llm(provider: str, model: str | None = None) -> dict:
    """provider(+선택 model) → {provider, model, key, key_name, label}.
    UI 에서 사용자가 고른 두뇌를 조회할 때 쓴다 (LLM_PROVIDER 기본값과 무관하게 임의 provider 조회 가능).
    auth_mode='cli' 인 provider(현재 anthropic)는 API 키 대신 로그인된 claude CLI 존재 여부를 'key'로 반환."""
    p = LLM_PROVIDERS.get(provider, LLM_PROVIDERS["gemini"])
    if p.get("auth_mode") == "cli":
        import shutil

        return {
            "provider": provider if provider in LLM_PROVIDERS else "gemini",
            "label": p["label"],
            "model": model or p["default_model"],
            "key": shutil.which("claude") or "",  # 존재하면 truthy → "연결됨" 표시(app.py 재사용)
            "key_name": "claude CLI (Enterprise 구독 로그인, API 키 불필요)",
            "presets": p["presets"],
        }
    key_attr = p["key_attr"]
    return {
        "provider": provider if provider in LLM_PROVIDERS else "gemini",
        "label": p["label"],
        "model": model or p["default_model"],
        "key": getattr(Keys, key_attr, None),
        "key_name": f"{key_attr}_API_KEY",
        "presets": p["presets"],
    }


def active_llm() -> dict:
    """.env 의 LLM_PROVIDER 기본값 기준 {provider, model, key, key_name} 반환."""
    return resolve_llm(LLM_PROVIDER)


def require(key_value: str | None, name: str) -> str:
    from .schema import DataError

    if not key_value:
        raise DataError(
            f"{name} 키가 설정되지 않았습니다. .env 에 {name} 를 추가하세요 "
            f"(.env.example 참고)."
        )
    return key_value

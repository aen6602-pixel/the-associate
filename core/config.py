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
    DEEPSEEK = os.getenv("DEEPSEEK_API_KEY")       # 에이전트 두뇌 (DeepSeek)
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
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").lower()

# ── 배포 모드 ──────────────────────────────────────────────────────
# 배포 모드가 하는 일: claude CLI 두뇌 숨김 + **인증이 없으면 앱을 열지 않음**(fail-closed).
# 그래서 이 값이 잘못 False 로 잡히면 공개 URL 이 게이트 없이 열린다 → 호스팅 흔적이 보이면
# 명시적 설정이 없어도 배포로 간주한다. 끄고 싶으면 DEPLOY_MODE=0 을 명시하면 된다.
_HOST_ENV_PREFIXES = ("RAILWAY_", "RENDER", "FLY_", "DYNO", "K_SERVICE", "WEBSITE_SITE_NAME")


def detect_deploy_mode(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    raw = (env.get("DEPLOY_MODE") or "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    return any(k.startswith(_HOST_ENV_PREFIXES) for k in env)


DEPLOY_MODE = detect_deploy_mode()
# ── 추론 강도(reasoning effort) ─────────────────────────────────────
# 밸류에이션은 "도구를 몇 개, 어떤 순서로 부를지" 를 정하는 다단계 추론이라 추론 강도가
# 답변 품질에 직결된다(낮추면 도구 호출을 건너뛰고 결론으로 점프하는 경향). 반대로 단순
# 조회에는 과한 비용이므로 UI 에서 고를 수 있게 한다.
#
# provider 마다 노브가 다르다:
#   OpenAI   /v1/responses 의 reasoning.effort (minimal|low|medium|high)
#   Gemini   thinking_config.thinking_budget (토큰수, 0=끔, -1=모델 자율)
#   Anthropic claude CLI 의 MAX_THINKING_TOKENS 환경변수
# 그래서 공통 어휘(off/minimal/low/medium/high/dynamic)를 두고 provider 별로 번역한다.
LLM_REASONING = (os.getenv("LLM_REASONING") or "").strip().lower() or None

REASONING_LABELS = {
    "off": "끔 (추론 없음, 가장 빠름)",
    "minimal": "최소 (단순 조회용)",
    "low": "낮음",
    "medium": "보통 (권장)",
    "high": "높음 (다단계 밸류에이션)",
    "dynamic": "모델 자율 (분량을 모델이 판단)",
}

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
# 2026-07-24 부로 "deepseek-chat"/"deepseek-reasoner" 는 완전히 폐기됐다(DeepSeek 공식
# 변경로그, 자동 라우팅 없이 그냥 오류). 후속 기본값은 v4-flash.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# provider → (표시명, key_attr, .env 모델변수명, 기본모델, 프리셋 모델 목록)
# 프리셋은 참고용 후보일 뿐 — UI 에서 자유 텍스트로 다른 모델 ID 도 입력 가능.
LLM_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini", "key_attr": "GEMINI",
        "env_model_var": "GEMINI_MODEL", "default_model": GEMINI_MODEL,
        "presets": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
        # thinking_budget 토큰. flash 는 0(끔)~24576, pro 는 0 을 못 받고 최소 128 이라
        # off 를 고르면 API 가 거부할 수 있다 → brain 이 실패 시 thinking_config 없이 재시도한다.
        "reasoning_levels": ["off", "low", "medium", "high", "dynamic"],
        "default_reasoning": "dynamic",
    },
    "openai": {
        "label": "OpenAI GPT", "key_attr": "OPENAI",
        "env_model_var": "OPENAI_MODEL", "default_model": OPENAI_MODEL,
        # 2026-08: gpt-5-nano 로 실제 조사성 질문(SK트리켐 케이스)에서 회사명을 스스로
        # 지어내는 tool-calling 오류를 확인 — GPT-5.6 Terra 로 교체(Sol 대비 훨씬 저렴하면서
        # 성능은 근접, Luna 는 'low-reasoning' 명시라 이런 다단계 조사엔 부적합).
        "presets": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna",
                   "gpt-5-nano", "gpt-4o-mini", "gpt-5-mini", "gpt-5", "gpt-4.1-mini"],
        # gpt-4o/4.1 계열은 추론 모델이 아니라 reasoning 인자를 거부한다 → brain 이
        # 추론 모델(gpt-5*, o1/o3/o4*)에만 인자를 붙인다.
        "reasoning_levels": ["minimal", "low", "medium", "high"],
        "default_reasoning": "medium",
    },
    "anthropic": {
        # API 키가 아니라 로그인된 Claude Code CLI(Enterprise 구독)를 서브프로세스로 재활용한다
        # (dart-agent/Martin's Bullseye 와 동일 방식) — 그래서 key_attr 이 없다.
        "label": "Anthropic Claude (CLI)", "key_attr": None, "auth_mode": "cli",
        "env_model_var": "ANTHROPIC_MODEL", "default_model": ANTHROPIC_MODEL,
        "presets": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"],
        "reasoning_levels": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
    },
    "deepseek": {
        # OpenAI 호환 chat.completions API (base_url 만 다름) — openai 패키지를 그대로 재사용.
        # v4 세대부터 thinking(추론) on/off 와 강도(reasoning_effort)가 요청 파라미터로 들어간다
        # (모델 이름으로 구분하던 구세대 deepseek-chat/deepseek-reasoner 는 2026-07-24 폐기됨).
        "label": "DeepSeek", "key_attr": "DEEPSEEK",
        "env_model_var": "DEEPSEEK_MODEL", "default_model": DEEPSEEK_MODEL,
        "presets": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "reasoning_levels": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
    },
}


def reasoning_levels(provider: str) -> list[str]:
    """그 provider 가 받을 수 있는 추론 강도 목록. 빈 목록이면 노브가 없다는 뜻."""
    return list(LLM_PROVIDERS.get(provider, {}).get("reasoning_levels") or [])


def default_reasoning(provider: str) -> str | None:
    """기본 추론 강도. .env 의 LLM_REASONING 이 그 provider 에서 유효하면 그것을 우선한다."""
    levels = reasoning_levels(provider)
    if not levels:
        return None
    if LLM_REASONING in levels:
        return LLM_REASONING
    return LLM_PROVIDERS[provider].get("default_reasoning") or levels[0]


def resolve_reasoning(provider: str, reasoning: str | None) -> str | None:
    """사용자가 고른 값을 검증한다. 지원하지 않는 값이면 **조용히 무시하지 않고** 기본값으로
    떨어뜨린다 — 서버가 400 으로 거를 수 있게 is_valid_reasoning 을 따로 둔다."""
    levels = reasoning_levels(provider)
    if not levels:
        return None
    want = (reasoning or "").strip().lower()
    return want if want in levels else default_reasoning(provider)


def is_valid_reasoning(provider: str, reasoning: str | None) -> bool:
    """None(미지정)은 유효 — 기본값을 쓴다는 뜻."""
    if reasoning is None or not str(reasoning).strip():
        return True
    return str(reasoning).strip().lower() in reasoning_levels(provider)


def resolve_llm(provider: str, model: str | None = None,
                reasoning: str | None = None) -> dict:
    """provider(+선택 model) → {provider, model, key, key_name, label}.
    UI 에서 사용자가 고른 두뇌를 조회할 때 쓴다 (LLM_PROVIDER 기본값과 무관하게 임의 provider 조회 가능).
    auth_mode='cli' 인 provider(현재 anthropic)는 API 키 대신 로그인된 claude CLI 존재 여부를 'key'로 반환."""
    p = LLM_PROVIDERS.get(provider, LLM_PROVIDERS["gemini"])
    resolved = provider if provider in LLM_PROVIDERS else "gemini"
    common = {
        "provider": resolved,
        "label": p["label"],
        "model": model or p["default_model"],
        "presets": p["presets"],
        "reasoning_levels": p.get("reasoning_levels", []),
        "default_reasoning": default_reasoning(resolved),
        "reasoning": resolve_reasoning(resolved, reasoning),
    }
    if p.get("auth_mode") == "cli":
        import shutil

        return {
            **common,
            "key": shutil.which("claude") or "",  # 존재하면 truthy → "연결됨" 표시(app.py 재사용)
            "key_name": "claude CLI (Enterprise 구독 로그인, API 키 불필요)",
        }
    key_attr = p["key_attr"]
    return {
        **common,
        "key": getattr(Keys, key_attr, None),
        "key_name": f"{key_attr}_API_KEY",
    }


def active_llm() -> dict:
    """.env 의 LLM_PROVIDER 기본값 기준 {provider, model, key, key_name, reasoning} 반환."""
    return resolve_llm(LLM_PROVIDER)


def require(key_value: str | None, name: str) -> str:
    from .schema import DataError

    if not key_value:
        raise DataError(
            f"{name} 키가 설정되지 않았습니다. .env 에 {name} 를 추가하세요 "
            f"(.env.example 참고)."
        )
    return key_value

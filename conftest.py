"""pytest 부트스트랩.

- 리포지토리 루트를 import 경로에 넣는다(tests/ 에서 `core`, `server` 를 import 하기 위해).
- 테스트가 **실제 키·실제 대화기록을 건드리지 않게** 환경을 고정한다. `core.config` / `core.paths`
  는 import 시점에 환경변수를 읽어 상수로 굳히므로, 어떤 모듈보다 먼저 여기서 세팅해야 한다.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 대화기록·캐시는 임시폴더로. (load_dotenv 는 기존 값을 덮지 않으니 로컬 .env 보다 우선한다)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="assoc-test-")
# 테스트는 항상 "배포된 상태"를 검증한다 — claude CLI 두뇌 숨김 + 인증 필수 경로.
os.environ["DEPLOY_MODE"] = "1"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["OPENAI_API_KEY"] = "sk-test-ci-placeholder-not-a-real-key"
os.environ["SESSION_SECRET"] = "test-session-secret-do-not-use-in-production"

# provider API 키는 **비운다** — 개발자 PC 의 .env 때문에 로컬만 통과하고 CI(키 없음)에서
# 터지는 일을 막는다. 실제로 그런 사고가 있었다: 스텁을 덜 건 테스트가 로컬에서는 진짜
# DART 를 호출해 통과하고, 키가 없는 CI 에서는 다른 예외가 나서 실패했다.
# 네트워크를 타야 하는 검증은 pytest 가 아니라 별도 스크립트로 돌린다.
for _key in ("DART_API_KEY", "ECOS_API_KEY", "FRED_API_KEY", "GEMINI_API_KEY",
             "EDINET_API_KEY", "FINMIND_TOKEN", "OPENFIGI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_key] = ""

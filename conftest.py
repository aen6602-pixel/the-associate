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

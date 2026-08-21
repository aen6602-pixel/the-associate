"""파일 경로를 한 곳에서 결정한다 — 로컬은 리포지토리 폴더, 배포는 영구 볼륨(DATA_DIR).

Railway 같은 컨테이너 호스트는 재배포·재시작마다 컨테이너를 새로 만든다. 앱 폴더에 쓴 파일은
그때 전부 사라지므로, 볼륨을 붙이고 `DATA_DIR` 에 그 마운트 경로(예: /data)를 주면
대화기록(sessions/)과 HTTP 캐시(.cache/)가 배포를 건너 살아남는다.

DATA_DIR 이 없거나 쓸 수 없으면 앱 폴더로 조용히 폴백한다 — 캐시·기록은 휘발되지만
앱 자체는 뜬다(볼륨 설정 실수로 서비스가 죽는 게 더 나쁘다).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# DATA_DIR 을 .env 로도 지정할 수 있게 여기서 먼저 로드한다(config.py 보다 먼저 import 되므로).
# dotenv 는 이미 있는 환경변수를 덮어쓰지 않으니 클라우드의 실제 env 가 항상 우선한다.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001 — dotenv 없거나 파일 없음
    pass


def _resolve_data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if not raw:
        return ROOT
    d = Path(raw)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return ROOT  # 볼륨 미마운트/권한 없음
    return d


DATA_DIR = _resolve_data_dir()
IS_PERSISTENT = DATA_DIR != ROOT  # 볼륨이 실제로 붙었는지 (UI 경고용)

CACHE_DIR = DATA_DIR / ".cache"
SESSIONS_DIR = DATA_DIR / "sessions"
for _d in (CACHE_DIR, SESSIONS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

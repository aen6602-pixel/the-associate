"""배포 모드 감지.

배포 모드는 (1) claude CLI 두뇌 숨김, (2) **인증 없으면 앱을 열지 않음**(fail-closed)을 켠다.
그래서 이 값이 잘못 False 가 되면 공개 URL 이 게이트 없이 열린다 — Nixpacks 배포에는 값을
구워둘 Dockerfile 이 없으므로, 호스팅 환경변수 흔적으로 자동 감지하는지 확인한다.
"""
from __future__ import annotations

import pytest

from core.config import detect_deploy_mode


@pytest.mark.parametrize("env", [
    {"RAILWAY_ENVIRONMENT_NAME": "production"},
    {"RAILWAY_SERVICE_NAME": "the-associate"},
    {"RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"},
    {"RENDER": "true"},
    {"FLY_APP_NAME": "assoc"},
])
def test_hosting_platform_implies_deploy_mode(env):
    """DEPLOY_MODE 를 깜빡해도 호스팅에 올라가면 fail-closed 가 켜져야 한다."""
    assert detect_deploy_mode(env) is True


def test_plain_local_env_is_not_deploy():
    assert detect_deploy_mode({"PATH": "/usr/bin", "HOME": "/home/me"}) is False
    # PORT 만으로는 배포로 보지 않는다 — 로컬에서도 흔히 쓰는 변수.
    assert detect_deploy_mode({"PORT": "8501"}) is False


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_explicit_setting_wins(raw, expected):
    env = {"DEPLOY_MODE": raw, "RAILWAY_ENVIRONMENT_NAME": "production"}
    # 빈 문자열은 "미설정"과 같으므로 호스팅 감지가 살아난다.
    assert detect_deploy_mode(env) is (True if raw == "" else expected)

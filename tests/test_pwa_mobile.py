"""PWA(홈 화면 설치) + 모바일 레이아웃 회귀 방지.

폰에서 실제로 깨졌던 것들을 고정한다. 브라우저 렌더링은 Chrome 헤드리스로 별도 실측했고
(390/375/360 폭), 여기서는 그 결론이 코드에서 유지되는지만 검사한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import app

WEB = Path(__file__).resolve().parent.parent / "web"
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
SW = (WEB / "sw.js").read_text(encoding="utf-8")


@pytest.fixture
def client():
    return TestClient(app)


# ── PWA 엔드포인트 ────────────────────────────────────────────────────
def test_manifest_is_served_with_the_right_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_manifest_is_installable():
    """설치 가능 조건: name, start_url, display, 192·512 아이콘."""
    m = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] and m["short_name"]
    assert m["start_url"] == "/" and m["scope"] == "/"
    assert m["display"] == "standalone"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    purposes = {i.get("purpose") for i in m["icons"]}
    assert "maskable" in purposes, "안드로이드에서 아이콘이 잘리지 않게 maskable 이 필요하다"


def test_all_manifest_icons_exist_and_are_served(client):
    m = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in m["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, f"{icon['src']} 없음"
        assert r.content.startswith(b"\x89PNG"), f"{icon['src']} 가 PNG 가 아니다"


def test_service_worker_is_served_from_root_scope(client):
    """/static/sw.js 로 두면 scope 가 /static/ 이 되어 화면 진입을 못 잡는다."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("service-worker-allowed") == "/"


def test_service_worker_itself_is_never_cached(client):
    """SW 파일이 캐시되면 새 배포가 사용자 기기에 안 내려간다."""
    cc = client.get("/sw.js").headers.get("cache-control", "")
    assert "no-store" in cc or "no-cache" in cc


def test_apple_touch_icon_is_reachable_at_the_conventional_paths(client):
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        r = client.get(path)
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")


def test_favicon_does_not_404(client):
    assert client.get("/favicon.ico").status_code == 200


# ── 서비스워커가 인증 데이터를 캐시하지 않는다 ────────────────────────
def test_sw_never_caches_api_responses():
    """재무 수치를 캐시해서 다시 보여주는 것은 이 앱에서 곧 오답이다."""
    assert "/api/" in SW
    assert re.search(r"pathname\.startsWith\('/api/'\)", SW), "API 를 우회시키는 분기가 없다"


def test_sw_does_not_precache_html():
    """index.html 을 캐시하면 로그아웃 상태가 로그인 화면처럼 보일 수 있다."""
    precache = re.search(r"const PRECACHE = \[(.*?)\];", SW, re.S).group(1)
    assert ".html" not in precache
    assert "'/'" not in precache


def test_sw_ignores_non_get_requests():
    assert "request.method !== 'GET'" in SW


def test_sw_has_a_fetch_handler():
    """Chrome 은 fetch 핸들러가 있는 SW 가 없으면 설치 프롬프트를 띄우지 않는다."""
    assert "addEventListener('fetch'" in SW


# ── 뷰포트·노치·키보드 ────────────────────────────────────────────────
def test_viewport_covers_the_notch_but_allows_zoom():
    vp = re.search(r'<meta name="viewport" content="([^"]+)"', HTML).group(1)
    assert "viewport-fit=cover" in vp, "노치 기기에서 배경이 잘린다"
    assert "width=device-width" in vp
    # 확대를 막으면 접근성이 깨진다 — iOS 자동확대는 16px 입력으로 해결한다.
    assert "maximum-scale" not in vp and "user-scalable=no" not in vp


def test_shell_uses_dynamic_viewport_height():
    """100vh 만 쓰면 iOS 주소창 때문에 하단 입력창이 화면 밖으로 밀린다."""
    shell = re.search(r"\.shell \{[^}]+\}", CSS).group(0)
    assert "100dvh" in shell
    assert "var(--vvh" in shell, "키보드가 올라온 실제 높이를 반영해야 한다"
    assert "100vh" in shell, "dvh 미지원 브라우저용 폴백이 남아 있어야 한다"


def test_js_feeds_visual_viewport_height_into_css():
    assert "visualViewport" in JS
    assert "--vvh" in JS


def test_form_controls_are_16px_on_phones():
    """iOS 는 16px 보다 작은 입력에 포커스하면 화면을 확대한다(body 는 15px)."""
    block = _media_block(CSS, "max-width: 640px")
    assert re.search(r"input, select, textarea \{ font-size: 16px", block)


def test_safe_area_insets_are_applied_to_the_composer():
    """하단 입력창이 아이폰 홈 인디케이터에 가리지 않아야 한다."""
    block = _media_block(CSS, "max-width: 640px")
    composer = re.search(r"\.composer \{[^}]+\}", block).group(0)
    assert "env(safe-area-inset-bottom)" in composer


def test_standalone_mode_reserves_the_status_bar():
    """홈 화면에서 실행하면 주소창이 없어 상단 내용이 상태바에 붙는다."""
    assert "@media (display-mode: standalone)" in CSS
    assert "env(safe-area-inset-top)" in CSS


# ── 사이드바 오버레이 ─────────────────────────────────────────────────
def test_sidebar_backdrop_exists_and_is_hidden_by_default():
    assert 'id="sidebar-backdrop"' in HTML
    assert re.search(r'id="sidebar-backdrop"[^>]*hidden', HTML)


def test_backdrop_is_desktop_hidden_and_mobile_only():
    assert re.search(r"\.sidebar-backdrop \{ display: none; \}", CSS)
    block = _media_block(CSS, "max-width: 860px")
    assert ".sidebar-backdrop" in block and "display: block" in block


def test_backdrop_closes_the_sidebar():
    assert re.search(r"\$\('sidebar-backdrop'\)\.addEventListener\('click'", JS)


def test_choosing_a_conversation_closes_the_overlay():
    """오버레이가 화면을 덮은 채 남으면 고른 결과를 볼 수 없다."""
    assert "$('session-list').addEventListener('click'" in JS
    assert "sidebarIsOverlay()" in JS


def test_returning_to_desktop_width_restores_the_sidebar():
    assert "matchMedia('(max-width: 860px)').addEventListener('change'" in JS


def test_escape_closes_the_overlay():
    assert "e.key === 'Escape'" in JS


# ── 표: 열을 숨기지 않고 첫 열을 고정한다 ─────────────────────────────
def test_wide_tables_scroll_instead_of_hiding_columns():
    wrap = re.search(r"\.md-table-wrap \{[^}]+\}", CSS).group(0)
    assert "overflow-x: auto" in wrap
    assert "-webkit-overflow-scrolling: touch" in wrap, "iOS 관성 스크롤"
    # 열을 display:none 으로 감추면 숫자를 임의로 숨기는 것이 된다.
    assert not re.search(r"\.md (th|td)[^{]*\{[^}]*display: none", CSS)


def test_first_table_column_is_sticky_on_phones():
    """오른쪽으로 밀면 '어느 회사 숫자인지' 를 잃는다 — 첫 열을 고정한다."""
    block = _media_block(CSS, "max-width: 640px")
    assert "th:first-child" in block and "position: sticky" in block
    # 배경이 투명하면 스크롤되는 숫자가 고정 열 아래로 비쳐 보인다.
    assert re.search(r"td:first-child \{[^}]*background:", block, re.S)


def test_zebra_rows_keep_the_sticky_cell_opaque():
    block = _media_block(CSS, "max-width: 640px")
    assert "nth-child(even) td:first-child" in block


# ── 터치 타깃 ─────────────────────────────────────────────────────────
def test_send_button_meets_the_44px_touch_target():
    block = _media_block(CSS, "max-width: 640px")
    primary = re.search(r"\.composer \.primary \{[^}]+\}", block).group(0)
    assert "44px" in primary


def test_placeholder_shrinks_so_it_is_not_clipped():
    """실측 360px: 안내문이 두 줄로 감겨 rows=1 높이(44px)에 잘렸다(필요 68px)."""
    assert "PLACEHOLDER_SHORT" in JS
    assert "fitPlaceholder" in JS


def test_textarea_max_height_follows_the_visible_viewport():
    assert "viewportHeight()" in JS


# ── 설치 UI ───────────────────────────────────────────────────────────
def test_install_button_and_ios_hint_exist():
    assert 'id="install-btn"' in HTML and 'id="install-hint"' in HTML
    assert re.search(r'id="install-btn"[^>]*hidden', HTML), "설치 가능할 때만 보여야 한다"


def test_install_flow_handles_ios_separately():
    """iOS Safari 는 beforeinstallprompt 를 지원하지 않아 안내로 대체해야 한다."""
    assert "beforeinstallprompt" in JS
    assert "iP(hone|ad|od)" in JS
    assert "홈 화면에 추가" in JS


def test_standalone_detection_covers_ios():
    assert "navigator.standalone" in JS


def test_service_worker_registration_is_scoped_to_root():
    assert "register('/sw.js', { scope: '/' })" in JS


# ── 헬퍼 ──────────────────────────────────────────────────────────────
def _media_block(css: str, query: str) -> str:
    """@media (...query...) { ... } 의 본문을 중괄호 균형으로 잘라낸다."""
    i = css.find(f"@media ({query})")
    assert i >= 0, f"미디어쿼리 없음: {query}"
    start = css.index("{", i)
    depth, j = 0, start
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:j]
        j += 1
    raise AssertionError(f"미디어쿼리 블록이 닫히지 않음: {query}")

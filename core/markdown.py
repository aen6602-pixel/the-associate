"""마크다운 → HTML. 의존성 없이 LLM 답변에 실제로 나오는 문법만 지원한다.

웹 UI(server)와 HTML 리포트(excel.html_report)가 같이 쓴다. 두뇌의 시스템 프롬프트가 근거를
**표**로 정리하라고 지시하므로 표 지원이 필수다.

보안: 입력은 LLM/사용자가 만든 문자열이다. 먼저 전부 `html.escape` 한 뒤 우리가 아는 문법만
태그로 바꾼다 → 원문에 `<script>` 가 있어도 그냥 텍스트로 보인다. 링크는 http/https 만 허용.
"""
from __future__ import annotations

import html
import re

__all__ = ["render", "render_inline"]

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^([-*_])\1{2,}$")
_FENCE = re.compile(r"^```(\w*)\s*$")
_TABLE_DIVIDER = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")

# escape 된 텍스트 위에서 도는 인라인 규칙 (순서 중요: 코드 먼저 → 그 안은 더 안 건드림)
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"(?<!\[)(?<!\()\b(https?://[^\s<>\"]+)")


def _anchor(url: str, label: str) -> str:
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'


def render_inline(escaped: str) -> str:
    """이미 escape 된 한 줄에 인라인 문법을 적용한다.

    코드 스팬과 완성된 링크는 자리표시자로 빼두고 마지막에 되돌린다 — 그러지 않으면
    코드 안의 `**` 가 굵게로 바뀌거나, 링크의 href 안 URL 이 자동링크에 다시 걸린다.
    """
    holes: list[str] = []

    def _hole(rendered: str) -> str:
        holes.append(rendered)
        return f"\x00{len(holes) - 1}\x00"

    text = _CODE_SPAN.sub(lambda m: _hole(f"<code>{m.group(1)}</code>"), escaped)
    text = _LINK.sub(lambda m: _hole(_anchor(m.group(2), m.group(1))), text)
    text = _BARE_URL.sub(lambda m: _hole(_anchor(m.group(1), m.group(1))), text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    # 나중에 만든 것부터 복원 — 링크 라벨 안에 코드 스팬이 들어간 경우까지 풀린다.
    for i in range(len(holes) - 1, -1, -1):
        text = text.replace(f"\x00{i}\x00", holes[i])
    return text


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def render(md: str, *, heading_offset: int = 0) -> str:
    """마크다운 → HTML 문자열.

    heading_offset: `#` 을 몇 단계 낮출지. HTML 리포트는 h1/h2 를 문서 제목으로 쓰므로 2를 준다.
    """
    if not md:
        return ""

    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None      # 열려있는 <ul>/<ol>
    in_quote = False
    fence_lang: str | None = None
    fence_buf: list[str] = []
    i = 0

    def close_para() -> None:
        if para:
            out.append("<p>" + render_inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def close_quote() -> None:
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    def close_all() -> None:
        close_para()
        close_list()
        close_quote()

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # ── 코드 펜스 ──
        if fence_lang is not None:
            if _FENCE.match(stripped):
                cls = f' class="lang-{html.escape(fence_lang)}"' if fence_lang else ""
                out.append(f"<pre><code{cls}>" + html.escape("\n".join(fence_buf)) + "</code></pre>")
                fence_lang, fence_buf = None, []
            else:
                fence_buf.append(raw)
            i += 1
            continue
        fence = _FENCE.match(stripped)
        if fence:
            close_all()
            fence_lang, fence_buf = fence.group(1), []
            i += 1
            continue

        # ── 빈 줄 = 블록 경계 ──
        if not stripped:
            close_all()
            i += 1
            continue

        # ── 표: 헤더행 다음 줄이 구분선이면 표로 본다 ──
        if "|" in stripped and i + 1 < len(lines) and _TABLE_DIVIDER.match(lines[i + 1].strip()):
            close_all()
            head = _cells(stripped)
            out.append('<div class="md-table-wrap"><table><thead><tr>')
            out.extend(f"<th>{render_inline(html.escape(c))}</th>" for c in head)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                out.append("<tr>")
                out.extend(f"<td>{render_inline(html.escape(c))}</td>" for c in _cells(lines[i]))
                out.append("</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue

        # ── 수평선 ──
        if _RULE.match(stripped):
            close_all()
            out.append("<hr>")
            i += 1
            continue

        # ── 헤딩 ──
        h = _HEADING.match(stripped)
        if h:
            close_all()
            level = min(len(h.group(1)) + heading_offset, 6)
            out.append(f"<h{level}>{render_inline(html.escape(h.group(2)))}</h{level}>")
            i += 1
            continue

        # ── 인용 ──
        q = _QUOTE.match(stripped)
        if q:
            close_para()
            close_list()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append("<p>" + render_inline(html.escape(q.group(1))) + "</p>")
            i += 1
            continue
        close_quote()

        # ── 리스트 ──
        b = _BULLET.match(stripped)
        o = None if b else _ORDERED.match(stripped)
        if b or o:
            close_para()
            want = "ul" if b else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            item = b.group(1) if b else o.group(2)
            out.append("<li>" + render_inline(html.escape(item)) + "</li>")
            i += 1
            continue
        close_list()

        # ── 일반 문단 (연속 줄은 이어붙인다) ──
        para.append(html.escape(stripped))
        i += 1

    # 닫히지 않은 펜스는 있는 만큼 코드블록으로 흘린다(답변이 잘려도 내용은 보이게).
    if fence_lang is not None and fence_buf:
        out.append("<pre><code>" + html.escape("\n".join(fence_buf)) + "</code></pre>")
    close_all()
    return "\n".join(out)

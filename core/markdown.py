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
# ```decision 블록 — 의사결정을 카드로 렌더한다(불릿으로 늘어놓으면 스캔이 안 된다).
_DECISION_META = ("id", "title", "recommend", "note", "impact")
_DECISION_LINE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*)$")
_OPTION_KEY = re.compile(r"^[A-E]$")
_TABLE_DIVIDER = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")

# 표 셀이 '수치'인가 — 자릿수가 맞아떨어져야 비교가 되는 열을 우측정렬·등폭숫자로 만든다.
# 넓게 잡으면 설명 문장까지 오른쪽으로 밀리므로 **숫자로 시작해 숫자·구분자·단위로만
# 끝나는** 셀만 본다. (예: "333,600,000", "8.78%", "1.2조원", "(1,234)", "△56", "12.3배")
_NUM_CELL = re.compile(
    r"^[(\[]?\s*[-+△▲▼]?\s*\d[\d,\s]*(\.\d+)?\s*"
    r"(%|%p|bp|배|주|원|달러|엔|위안|조|억|만|천|억원|조원|만원|x|배수|USD|KRW|JPY|TWD|EUR)?\s*[)\]]?$")
_NEG_CELL = re.compile(r"^\s*[-△▲]|^\s*[(\[]")


def _is_num_cell(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(_NUM_CELL.match(t))


def _cell_attrs(text: str, numeric_col: bool) -> str:
    """수치 열에만 class 를 단다. 열 단위로 판정하므로 '미확보'·'NM' 같은 빈칸이 섞여도
    열 전체 정렬이 흐트러지지 않는다."""
    if not numeric_col:
        return ""
    t = (text or "").strip()
    cls = "num"
    if _is_num_cell(t) and _NEG_CELL.match(t):
        cls += " neg"
    return f' class="{cls}"'

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


def _decision_html(lines: list[str], seq: int) -> str:
    """```decision 블록 → 선택 가능한 카드.

    형식(순서 무관, 전부 선택):
        id: 1                 없으면 등장 순서로 번호
        title: D&A/매출
        recommend: A
        note: 2024년 값이 이상치일 수 있음
        A: 사용자가 직접 입력
        B: DCF 중단
    """
    meta: dict[str, str] = {}
    options: list[tuple[str, str]] = []
    for raw in lines:
        m = _DECISION_LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if _OPTION_KEY.match(key.upper()) and len(key) == 1:
            options.append((key.upper(), val))
        elif key.lower() in _DECISION_META:
            meta[key.lower()] = val

    if not options:   # 형식이 틀렸으면 원문을 코드블록으로 보여준다(내용을 숨기지 않는다)
        return "<pre><code>" + html.escape("\n".join(lines)) + "</code></pre>"

    did = meta.get("id") or str(seq)
    rec = (meta.get("recommend") or "").strip().upper()[:1]
    out = [f'<div class="decision" data-decision="{html.escape(did)}">',
           '<div class="decision-head">',
           f'<span class="decision-no">{html.escape(did)}</span>',
           f'<span class="decision-title">{render_inline(html.escape(meta.get("title", "")))}</span>',
           "</div>"]
    if meta.get("note"):
        out.append(f'<p class="decision-note">{render_inline(html.escape(meta["note"]))}</p>')
    out.append('<div class="decision-opts">')
    for key, text in options:
        is_rec = key == rec
        out.append(
            f'<button type="button" class="decision-opt{" recommended" if is_rec else ""}" '
            f'data-choice="{html.escape(did + key)}" data-decision="{html.escape(did)}">'
            f'<span class="opt-key">{key}</span>'
            f'<span class="opt-text">{render_inline(html.escape(text))}</span>'
            + ('<span class="opt-rec">권고</span>' if is_rec else "")
            + "</button>")
    out.append("</div>")
    if meta.get("impact"):
        out.append(f'<p class="decision-impact">영향: '
                   f'{render_inline(html.escape(meta["impact"]))}</p>')
    out.append("</div>")
    return "\n".join(out)


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
    decision_seq = 0
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
                if fence_lang.lower() == "decision":
                    decision_seq += 1
                    out.append(_decision_html(fence_buf, decision_seq))
                else:
                    cls = f' class="lang-{html.escape(fence_lang)}"' if fence_lang else ""
                    out.append(f"<pre><code{cls}>"
                               + html.escape("\n".join(fence_buf)) + "</code></pre>")
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
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1

            # 열별로 수치 열인지 먼저 판정한다(본문 셀 과반이 수치면 그 열은 수치 열).
            # 셀 단위로 정하면 같은 열에서 어떤 칸은 오른쪽, 어떤 칸은 왼쪽이 되어 더 어지럽다.
            ncols = max([len(head)] + [len(r) for r in rows])
            numeric = []
            for c in range(ncols):
                vals = [r[c] for r in rows if c < len(r) and r[c].strip()]
                numeric.append(bool(vals) and sum(_is_num_cell(v) for v in vals) * 2 >= len(vals))

            out.append('<div class="md-table-wrap"><table><thead><tr>')
            out.extend(f"<th{_cell_attrs(c, numeric[n] if n < ncols else False)}>"
                       f"{render_inline(html.escape(c))}</th>"
                       for n, c in enumerate(head))
            out.append("</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>")
                out.extend(f"<td{_cell_attrs(c, numeric[n] if n < ncols else False)}>"
                           f"{render_inline(html.escape(c))}</td>"
                           for n, c in enumerate(r))
                out.append("</tr>")
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

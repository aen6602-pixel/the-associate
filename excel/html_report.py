"""밸류에이션 결과(질문+답변+데이터소스 trace) → 단일 파일 HTML 리포트.

Excel 워크북과 같은 소스 데이터(Value/Provenance)를 그대로 재사용해 렌더링만 HTML로 한다.
외부 CDN/폰트 없이 완전히 독립된 파일이라 오프라인에서도, 다른 사람에게 그대로 전달해도 그대로 열린다.
"""
from __future__ import annotations

import html
import re

from core import markdown
from core.schema import now_iso

_TIER = {
    "authoritative": ("공식", "#0f9d58"),
    "parsed_authoritative": ("원문", "#1e8e3e"),
    "reference": ("참조", "#4285f4"),
    "computed": ("계산", "#8e44ad"),
    "assumption": ("가정", "#e67e22"),
    "llm_estimate": ("LLM추정", "#d93025"),
}


def _tier(source_type: str) -> tuple[str, str]:
    return _TIER.get(source_type, ("?", "#999"))


def _fmt_args(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in (d or {}).items())


def _md_to_html(md: str) -> str:
    """LLM 답변(마크다운) → HTML. 변환기는 웹 UI 와 공용(`core.markdown`)이다.

    heading_offset=2 — 리포트는 h1/h2 를 문서 제목·섹션 제목용으로 예약해 두었다."""
    return markdown.render(md, heading_offset=2)


def _render_trace_item(t: dict) -> str:
    name = html.escape(t.get("name", ""))
    args = html.escape(_fmt_args(t.get("input", {})))
    result = t.get("result", {})
    if not result.get("ok"):
        err = html.escape(str(result.get("error", "알 수 없는 오류")))
        return (
            f'<div class="src-item src-error">'
            f'<div class="src-head"><code>{name}({args})</code></div>'
            f'<div class="src-err">⚠️ {err}</div></div>'
        )
    v = result["value"]
    p = v.get("provenance", {})
    label, color = _tier(p.get("source_type", ""))
    meta = []
    if p.get("as_of"):
        meta.append(f"기준 {html.escape(str(p['as_of']))}")
    if p.get("original_field"):
        meta.append(f"필드 <code>{html.escape(str(p['original_field']))}</code>")
    meta.append(f"출처 {html.escape(str(p.get('source', '')))}")
    meta_html = " · ".join(meta)
    raw_url = str(p.get("source_url", ""))
    url = html.escape(raw_url)
    url_html = f'<a href="{url}">{url}</a>' if raw_url.startswith(("http://", "https://")) else url
    note = html.escape(str(p.get("note") or ""))
    extras_html = ""
    for extra in (v.get("extras") or {}).values():
        extras_html += (
            f'<div class="src-extra">↳ <strong>{html.escape(str(extra.get("label") or ""))}</strong>: '
            f'{html.escape(str(extra.get("value")))} {html.escape(str(extra.get("unit", "")))}</div>'
        )
    return (
        f'<div class="src-item">'
        f'<div class="src-head">'
        f'<span class="badge" style="background:{color}">{label}</span> '
        f'<code>{name}({args})</code> → '
        f'<strong>{html.escape(str(v.get("value")))} {html.escape(str(v.get("unit", "")))}</strong>'
        f'</div>'
        f'<div class="src-meta">{meta_html}<br>{url_html}</div>'
        + (f'<div class="src-note">{note}</div>' if note else "")
        + extras_html
        + "</div>"
    )


_CSS = """
:root{color-scheme:light dark}
body{font-family:-apple-system,"Segoe UI",Malgun Gothic,sans-serif;max-width:840px;
     margin:32px auto;padding:0 20px;line-height:1.6;color:#1a1a1a;background:#fff}
h1{font-size:1.5rem;margin-bottom:4px}
.subtitle{color:#666;font-size:.85rem;margin-bottom:24px}
.question{background:#f5f6f8;border-left:4px solid #4285f4;padding:10px 14px;
          border-radius:4px;margin-bottom:20px;font-size:.95rem}
.answer{margin-bottom:28px}
.answer p{margin:.6em 0}
.answer h3,.answer h4{margin:1em 0 .3em}
.answer code{background:#f1f1f1;padding:1px 5px;border-radius:3px;font-size:.9em}
.answer pre{background:#f7f7f8;border:1px solid #e5e5e5;border-radius:6px;padding:10px;overflow-x:auto}
.answer pre code{background:none;padding:0}
.answer blockquote{margin:.7em 0;padding:2px 12px;border-left:3px solid #ddd;color:#555}
.answer hr{border:none;border-top:1px solid #e0e0e0;margin:1.2em 0}
.md-table-wrap{overflow-x:auto;margin:.9em 0}
.answer table{border-collapse:collapse;font-size:.88rem;min-width:100%}
.answer th,.answer td{border:1px solid #e0e0e0;padding:5px 9px;text-align:left;white-space:nowrap}
.answer th{background:#f5f6f8;font-weight:600}
.answer tbody tr:nth-child(even){background:#fafafa}
h2.section{font-size:1.05rem;border-top:1px solid #e0e0e0;padding-top:18px;margin-top:28px;color:#333}
.src-item{border:1px solid #e5e5e5;border-radius:6px;padding:10px 14px;margin-bottom:10px;font-size:.88rem}
.src-item.src-error{border-color:#f3c6c4;background:#fff8f7}
.src-head code{background:#f1f1f1;padding:1px 5px;border-radius:3px}
.badge{display:inline-block;color:#fff;font-size:.72rem;font-weight:600;
       padding:1px 7px;border-radius:10px;margin-right:4px}
.src-meta{color:#666;margin-top:4px;font-size:.82rem}
.src-meta a{color:#4285f4;word-break:break-all}
.src-note{color:#8e44ad;margin-top:4px;font-size:.82rem}
.src-extra{color:#444;margin-top:4px;font-size:.85rem;padding-left:8px}
.src-err{color:#c5221f;margin-top:4px}
footer{margin-top:36px;color:#999;font-size:.78rem;border-top:1px solid #eee;padding-top:12px}
@media (prefers-color-scheme: dark){
  body{background:#1e1e1e;color:#e8e8e8}
  .question{background:#2a2d33;border-left-color:#669df6}
  .src-item{border-color:#3a3a3a}
  .src-item.src-error{border-color:#5c3230;background:#2a1f1e}
  .answer code,.src-head code{background:#333;color:#eee}
  .answer pre{background:#252525;border-color:#3a3a3a}
  .answer blockquote{border-left-color:#444;color:#bbb}
  .answer hr{border-top-color:#3a3a3a}
  .answer th,.answer td{border-color:#3a3a3a}
  .answer th{background:#2a2d33}
  .answer tbody tr:nth-child(even){background:#242424}
  .src-extra{color:#ccc}
  h2.section{border-top-color:#3a3a3a;color:#ccc}
  footer{border-top-color:#3a3a3a}
}
"""


def _default_title(trace: list[dict]) -> str:
    """파일명·제목에 쓸 짧고 깨끗한 제목을 결정한다. 채팅 질문 원문(길고 특수문자 섞임)은
    쓰지 않고, trace 의 tool 호출 인자에서 company 를 뽑아 고정 포맷으로 만든다 —
    excel/exporters.py 가 파일명을 `{방법}_{company}_{연도}.xlsx` 로 짓는 것과 동일한 원칙."""
    for t in trace or []:
        company = (t.get("input") or {}).get("company")
        if company:
            return f"{company} 밸류에이션 리포트"
    return "밸류에이션 리포트"


def build_html_report(answer_md: str, trace: list[dict],
                       question: str | None = None,
                       title: str | None = None) -> tuple[bytes, str]:
    """질문+답변+데이터소스 trace → (html bytes, 파일명). 채팅 한 턴을 그대로 리포트로 내보낸다.
    title 을 안 주면(권장) trace 에서 뽑은 회사명으로 제목을 정한다 — 채팅 질문 원문을 그대로
    쓰면 파일명이 특수문자 치환으로 지저분해지므로(예: "이 회사 DCF 좀 알려줘?" 같은 문장) 쓰지 않는다."""
    trace = trace or []
    ok_items = [t for t in trace if t.get("result", {}).get("ok")]
    err_items = [t for t in trace if not t.get("result", {}).get("ok")]

    page_title = title or _default_title(trace)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(page_title)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{html.escape(page_title)}</h1>",
        f'<div class="subtitle">SKSQ Valuation Agent · 생성 {html.escape(now_iso())} · '
        f"공개 데이터 기반, 출처 검증 가능 (할루시네이션 없음)</div>",
    ]
    if question:
        parts.append(f'<div class="question">💬 {html.escape(question)}</div>')

    parts.append('<div class="answer">' + _md_to_html(answer_md) + "</div>")

    if trace:
        parts.append(f'<h2 class="section">🔍 사용한 데이터 소스 ({len(ok_items)}개 성공'
                      + (f", {len(err_items)}개 실패" if err_items else "") + ")</h2>")
        for t in trace:
            parts.append(_render_trace_item(t))

    parts.append(
        "<footer>이 리포트의 모든 수치는 provenance(출처)가 달린 Value로 계산되었으며, "
        "본문 서술은 LLM이 위 데이터를 근거로 작성했습니다. 위 출처 링크로 원문을 직접 확인할 수 있습니다."
        "</footer>"
    )
    parts.append("</body></html>")

    html_bytes = "\n".join(parts).encode("utf-8")

    base = re.sub(r"[^\w가-힣-]", "_", page_title).strip("_")[:40] or "리포트"
    ts = now_iso().replace(":", "").replace("-", "")[:15]
    fname = f"SKSQ_{base}_{ts}.html"
    return html_bytes, fname

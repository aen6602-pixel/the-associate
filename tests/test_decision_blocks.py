"""```decision 블록 렌더링 — 선택지를 클릭 가능한 카드로.

기존에는 의사결정이 `### 1. 제목` + 중첩 불릿으로 나와서 스캔이 어렵고, 사용자가
"1A, 2A, 3A" 를 손으로 타이핑해야 했다. 이제 서버가 시맨틱한 카드 HTML 을 내고
프론트가 클릭·전송을 붙인다.
"""
from __future__ import annotations

from core import markdown

BLOCK = """\
결정이 필요합니다.

```decision
id: 1
title: D&A/매출 가정
note: 5개년 평균은 5.31% 지만 단발 요인이 섞여 있다
recommend: A
impact: B 는 주당가치가 약 7% 낮아진다
A: 5개년 평균 5.31% 적용
B: 최근 3개년 평균 4.9% 로 정상화
```
"""


def test_block_becomes_a_card_with_options():
    html = markdown.render(BLOCK)
    assert 'class="decision"' in html
    assert 'data-decision="1"' in html
    assert html.count("<button") == 2, "선택지 수만큼 버튼이 나와야 한다"
    assert 'data-choice="1A"' in html and 'data-choice="1B"' in html


def test_title_note_and_impact_are_rendered():
    html = markdown.render(BLOCK)
    assert "D&amp;A/매출 가정" in html
    assert "단발 요인" in html
    assert "약 7% 낮아진다" in html


def test_recommended_option_is_marked():
    html = markdown.render(BLOCK)
    rec = html.split('data-choice="1A"')[1].split("</button>")[0]
    assert "권고" in rec
    assert "recommended" in html.split('data-choice="1A"')[0].rsplit("<button", 1)[1]


def test_prose_outside_the_block_survives():
    html = markdown.render(BLOCK)
    assert "<p>결정이 필요합니다.</p>" in html


def test_multiple_blocks_get_distinct_ids():
    md = BLOCK + "\n```decision\nid: 2\ntitle: 영구성장률\nA: 2%\nB: 3%\n```\n"
    html = markdown.render(md)
    assert 'data-decision="1"' in html and 'data-decision="2"' in html
    assert 'data-choice="2B"' in html


def test_id_is_auto_numbered_when_missing():
    html = markdown.render("```decision\ntitle: 첫째\nA: x\nB: y\n```\n"
                           "```decision\ntitle: 둘째\nA: p\nB: q\n```")
    assert 'data-choice="1A"' in html
    assert 'data-choice="2A"' in html


def test_gate_style_id_is_kept():
    html = markdown.render("```decision\nid: gate1\ntitle: 방법론 승인\nA: 승인\nB: 수정\n```")
    assert 'data-decision="gate1"' in html
    assert 'data-choice="gate1A"' in html


def test_malformed_block_shows_content_instead_of_vanishing():
    """형식이 틀렸을 때 내용을 삼키면 사용자가 무슨 말인지 알 수 없게 된다."""
    html = markdown.render("```decision\n이건 형식이 아니다\n```")
    assert "이건 형식이 아니다" in html
    assert 'class="decision"' not in html


def test_option_text_is_escaped_and_inline_markdown_works():
    html = markdown.render(
        "```decision\nid: 1\ntitle: t\nA: **굵게** 와 <script>\nB: b\n```")
    assert "<strong>굵게</strong>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_normal_code_fence_is_untouched():
    html = markdown.render("```python\nx = 1\n```")
    assert "<pre><code" in html
    assert 'class="decision"' not in html

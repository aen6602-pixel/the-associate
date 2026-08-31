"""마크다운 렌더러 — 답변에 실제로 나오는 문법과 XSS 방어를 검증한다.

두뇌의 시스템 프롬프트가 근거를 **표**로 정리하라고 지시하므로 표는 필수 경로다.
웹 UI 와 HTML 리포트가 같은 렌더러를 쓴다.
"""
from __future__ import annotations

from core import markdown


def test_table_renders():
    html = markdown.render("| 항목 | 값 |\n|---|---:|\n| 매출 | 300조 |\n| EBIT | 50조 |")
    assert html.count("<tr>") == 3          # 헤더 1 + 데이터 2
    assert "<th>항목</th>" in html
    assert ">300조</td>" in html            # 수치 열이라 class 가 붙는다(아래 테스트 참고)
    assert 'class="md-table-wrap"' in html  # 좁은 화면에서 가로 스크롤


def test_numeric_columns_are_right_aligned_as_a_whole_column():
    """수치는 자릿수가 세로로 맞아야 크기 비교가 눈으로 된다. 판정은 **열 단위**여서
    '미확보' 같은 빈칸이 섞여도 그 열의 정렬이 깨지지 않는다."""
    html = markdown.render(
        "| 회사 | 시가총액 | 비고 |\n|---|---|---|\n"
        "| 삼성전자 | 333,600,000 | FY2025 |\n"
        "| SK하이닉스 | 미확보 | 조회 실패 |")
    assert '<th class="num">시가총액</th>' in html, "헤더도 본문과 같이 정렬돼야 한다"
    assert '<td class="num">333,600,000</td>' in html
    assert '<td class="num">미확보</td>' in html, "같은 열은 빈칸이어도 정렬을 유지한다"
    assert "<td>삼성전자</td>" in html, "이름 열까지 오른쪽으로 밀면 안 된다"
    assert "<td>FY2025</td>" in html


def test_negative_numbers_are_marked():
    html = markdown.render("| 항목 | 값 |\n|---|---|\n| 영업이익 | -12,345 |\n| 순부채 | (1,234) |")
    assert '<td class="num neg">-12,345</td>' in html
    assert '<td class="num neg">(1,234)</td>' in html, "회계 표기 괄호도 음수다"


def test_paragraph_bold_code_and_link():
    html = markdown.render("주당 **50,878원** 입니다. `sangjeung` 엔진. [DART](https://dart.fss.or.kr)")
    assert "<strong>50,878원</strong>" in html
    assert "<code>sangjeung</code>" in html
    assert '<a href="https://dart.fss.or.kr" target="_blank" rel="noopener noreferrer">DART</a>' in html


def test_headings_lists_quote_and_rule():
    html = markdown.render("# 제목\n\n- 하나\n- 둘\n\n1. 첫째\n2. 둘째\n\n> 인용\n\n---")
    assert "<h1>제목</h1>" in html
    assert html.count("<li>") == 4
    assert "<ul>" in html and "<ol>" in html
    assert "<blockquote>" in html
    assert "<hr>" in html


def test_heading_offset_for_report():
    """리포트는 h1/h2 를 문서 제목용으로 예약 → 답변의 # 은 h3 부터 시작한다."""
    assert "<h3>" in markdown.render("# 제목", heading_offset=2)


def test_code_fence_keeps_content_literal():
    html = markdown.render("```python\nx = 1 < 2\n```")
    assert "<pre><code" in html
    assert "1 &lt; 2" in html


def test_html_in_input_is_escaped():
    html = markdown.render("<script>alert('x')</script> **safe**")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>safe</strong>" in html


def test_javascript_url_is_not_linkified():
    html = markdown.render("[click](javascript:alert(1))")
    assert "javascript:" not in html.lower().replace("&#x27;", "") or "<a " not in html
    assert "<a " not in html


def test_code_span_content_is_not_reinterpreted():
    """코드 스팬 안의 별표는 굵게로 바뀌면 안 된다."""
    html = markdown.render("`**not bold**`")
    assert "<strong>" not in html
    assert "<code>**not bold**</code>" in html


def test_empty_input():
    assert markdown.render("") == ""
    assert markdown.render(None) == ""

"""대화 제목 — 목록에서 서로 구분되게.

질문 원문을 그대로 자르면 "리노공업 DCF를 해보고 싶어…" 처럼 앞부분만 남아, 같은 회사를
여러 방법으로 본 대화들이 목록에서 전부 비슷해 보인다. 무엇을 어떤 방법으로 봤는지는
trace 에 이미 구조화돼 있으므로 그걸 쓴다.
"""
from __future__ import annotations

import pytest

from core import history


def _ok(tool: str, company: str | None = None) -> dict:
    return {"name": tool, "input": ({"company": company} if company else {}),
            "result": {"ok": True, "value": {}}}


def _conv(question: str, *tools: dict) -> list[dict]:
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": "", "trace": list(tools)}]


def test_title_uses_the_company_and_method_actually_run():
    t = history._make_title(_conv("가치평가 부탁", _ok("compute_dcf", "리노공업")))
    assert t == "리노공업 DCF"


def test_conclusion_method_wins_over_its_inputs():
    """베타·WACC 는 DCF 를 만들기 위한 입력이다 — 그 대화는 'DCF' 지 '베타' 가 아니다."""
    t = history._make_title(_conv(
        "리노공업 DCF", _ok("get_beta", "리노공업"),
        _ok("compute_wacc_auto", "리노공업"), _ok("compute_dcf", "리노공업")))
    assert t == "리노공업 DCF"


def test_scenarios_outrank_a_single_dcf():
    t = history._make_title(_conv("범위로 보여줘", _ok("compute_dcf", "기아"),
                                  _ok("compute_scenarios", "기아")))
    assert t == "기아 DCF 시나리오"


def test_plain_lookup_keeps_just_the_company():
    t = history._make_title(_conv("매출 알려줘", _ok("get_financial_item", "SK하이닉스")))
    assert t == "SK하이닉스"


def test_failed_tools_do_not_name_the_conversation():
    """조회에 실패한 회사로 '리노공업 DCF' 같은 제목을 지으면 안 된다 — 그 값을 얻은 적이
    없는데 얻은 것처럼 보인다. 이 경우엔 trace 경로를 포기하고 질문으로 되돌아가야 한다."""
    bad = {"name": "compute_dcf", "input": {"company": "없는회사"},
           "result": {"ok": False, "error": "회사를 못 찾음"}}
    assert history._from_trace(_conv("무엇이든", bad)) is None


@pytest.mark.parametrize("question, want", [
    ("한국 ERP가 얼마야?", "한국 ERP"),
    ("포마트코리아 가치평가를 해보고 싶은데 가능해?", "포마트코리아 가치평가"),
    ("삼성전자 최근 3개년 매출 알려줘", "삼성전자 최근 3개년 매출"),
])
def test_toolless_chats_fall_back_to_a_trimmed_question(question, want):
    """도구를 안 쓴 대화만 질문으로 짓는다. 끝의 요청 표현은 정보가 없고 길이만 먹는다."""
    assert history._make_title([{"role": "user", "content": question}]) == want


def test_empty_conversation_has_a_name():
    assert history._make_title([]) == "새 대화"


def test_title_stays_short_enough_for_one_line():
    long_co = "아주아주긴회사이름주식회사홀딩스컴퍼니리미티드"
    t = history._make_title(_conv("평가", _ok("compute_dcf", long_co)))
    assert len(t) <= 28

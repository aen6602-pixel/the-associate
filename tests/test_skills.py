"""Skill 로더 — 등록·로딩·경로 조작 방어, 그리고 프롬프트에 본문이 새지 않는지.

핵심은 **progressive disclosure**: 시스템 프롬프트에는 이름·설명만 들어가고 본문(25KB)은
두뇌가 load_skill 을 부를 때만 들어와야 한다. 이게 깨지면 매 요청이 무거워지고 tool-calling
정확도가 떨어진다.
"""
from __future__ import annotations

import pytest

from agent import brain, registry
from core import skills
from core.schema import DataError


def test_valuation_skill_is_registered():
    names = {s["name"] for s in skills.available()}
    assert "valuation-agent" in names


def test_skill_metadata_is_parsed():
    s = next(s for s in skills.available() if s["name"] == "valuation-agent")
    assert "DCF" in s["description"]
    assert "dcf.md" in s["references"]
    assert "validation.md" in s["references"]


def test_load_returns_body_without_frontmatter():
    s = skills.load("valuation-agent")
    assert not s["body"].startswith("---"), "frontmatter 는 본문에서 제거돼야 한다"
    assert "Gate 1" in s["body"]
    assert "도구 매핑" in s["body"], "이 앱의 도구 매핑 섹션이 있어야 실행 가능하다"


def test_reference_loads_with_or_without_extension():
    a = skills.reference("valuation-agent", "dcf.md")
    b = skills.reference("valuation-agent", "dcf")
    assert a["body"] == b["body"]
    assert "FCFF" in a["body"]


def test_unknown_skill_lists_what_exists():
    with pytest.raises(DataError) as e:
        skills.load("nope")
    assert "valuation-agent" in str(e.value)


def test_unknown_reference_lists_available_files():
    with pytest.raises(DataError) as e:
        skills.reference("valuation-agent", "nope")
    assert "dcf.md" in str(e.value)


@pytest.mark.parametrize("evil", [
    "../../../etc/passwd", "..\\..\\secrets", "../SKILL", "a/b", "", "   ",
])
def test_reference_rejects_path_traversal(evil):
    with pytest.raises(DataError):
        skills.reference("valuation-agent", evil)


# ── 프롬프트 노출 범위 ────────────────────────────────────────────
def test_roster_has_names_but_not_bodies():
    roster = skills.roster_text()
    assert "valuation-agent" in roster
    assert "Gate 1" not in roster, "절차서 본문이 목록에 새면 안 된다"
    assert len(roster) < 1000


def test_system_prompt_stays_small():
    p = brain._system_prompt()
    assert "valuation-agent" in p, "절차서 목록은 프롬프트에 있어야 한다"
    assert "Gate 1" not in p, "본문은 load_skill 로만 들어와야 한다"
    body = skills.load("valuation-agent")["body"]
    assert len(p) < len(body) + 8000


# ── 도구 배선 ─────────────────────────────────────────────────────
def test_tools_are_registered():
    names = [s["name"] for s in registry.tool_schemas()]
    assert "load_skill" in names and "read_skill_reference" in names


def test_dispatch_returns_body_as_text():
    res = registry.dispatch("load_skill", {"name": "valuation-agent"})
    assert res["ok"], res.get("error")
    v = res["value"]
    assert "Gate 1" in v["text"]
    assert v["value"] == len(v["text"]), "value 는 본문 길이"
    assert v["provenance"]["source_type"] == "reference"


def test_dispatch_reference():
    res = registry.dispatch("read_skill_reference",
                            {"name": "valuation-agent", "file": "validation.md"})
    assert res["ok"], res.get("error")
    assert "Gate 1과 Gate 2 승인이 있다" in res["value"]["text"]


def test_dispatch_reports_bad_input_without_raising():
    res = registry.dispatch("read_skill_reference",
                            {"name": "valuation-agent", "file": "../../.env"})
    assert res["ok"] is False
    assert "올바르지" in res["error"] or "없습니다" in res["error"]

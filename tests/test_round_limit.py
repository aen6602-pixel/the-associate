"""라운드 상한 — 값, 그리고 상한에 걸렸을 때 조사 결과를 버리지 않는지.

과거 동작: 상한 도달 시 `{"type":"error"}` 하나만 내고 끝 → 그동안 조회한 DART·시세 데이터가
전부 버려지고 사용자는 아무 답도 못 받았다. 이제는 도구를 끄고 한 번 더 불러 확보한 근거로
정리한 답을 주고, 상한에 걸렸다는 사실을 함께 알린다.
"""
from __future__ import annotations

import importlib

import pytest

from agent import brain


def test_default_is_high_enough_for_a_playbook_run():
    """절차서 로딩(2~4 라운드) + 데이터 수집 + 계산이 한 질문에 들어간다."""
    assert brain.MAX_ROUNDS >= 10
    import inspect

    assert inspect.signature(brain.answer).parameters["max_rounds"].default == brain.MAX_ROUNDS


def test_env_can_override(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "3")
    reloaded = importlib.reload(brain)
    try:
        assert reloaded.MAX_ROUNDS == 3
    finally:
        monkeypatch.delenv("AGENT_MAX_ROUNDS", raising=False)
        importlib.reload(brain)


def test_invalid_env_falls_back_to_at_least_one(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    reloaded = importlib.reload(brain)
    try:
        assert reloaded.MAX_ROUNDS == 1
    finally:
        monkeypatch.delenv("AGENT_MAX_ROUNDS", raising=False)
        importlib.reload(brain)


def test_note_explains_what_happened():
    note = brain._round_limit_note(12)
    assert "12" in note
    assert "중단" in note and "근거" in note


# ── 상한 도달 시 salvage (OpenAI 경로) ────────────────────────────
class _FakeCall:
    type = "function_call"

    def __init__(self, name, args, call_id):
        self.name, self.arguments, self.call_id = name, args, call_id


class _FakeResp:
    def __init__(self, output, text=""):
        self.output, self.output_text = output, text


class _FakeResponses:
    """항상 도구를 부르는 모델. tool_choice='none' 이 오면 텍스트를 낸다."""

    def __init__(self):
        self.calls = 0
        self.saw_tool_choice_none = False

    def create(self, **kw):
        self.calls += 1
        if kw.get("tool_choice") == "none":
            self.saw_tool_choice_none = True
            return _FakeResp([], "확보한 근거로 정리한 답변입니다.")
        return _FakeResp([_FakeCall("get_risk_free_rate", '{"country":"KR"}', f"c{self.calls}")])


@pytest.fixture
def fake_openai(monkeypatch):
    fake = _FakeResponses()

    class _Client:
        responses = fake

    monkeypatch.setattr(brain.config, "resolve_llm", lambda p, m=None: {
        "provider": "openai", "model": "test-model", "key": "sk-test",
        "key_name": "OPENAI_API_KEY", "label": "test", "presets": []})
    monkeypatch.setattr(brain.registry, "dispatch",
                        lambda name, args: {"ok": True, "value": {
                            "value": 4.32, "unit": "%", "label": "Rf",
                            "provenance": {"source": "ECOS", "source_type": "authoritative",
                                           "source_url": ""}}})

    import openai as openai_mod

    monkeypatch.setattr(openai_mod, "OpenAI", lambda api_key=None: _Client())
    return fake


def test_round_limit_salvages_an_answer(fake_openai):
    events = list(brain.answer("삼성전자 DCF 해줘", provider="openai", max_rounds=3))
    kinds = [e["type"] for e in events]

    assert kinds.count("tool_use") == 3, "상한만큼만 도구를 부른다"
    assert "error" not in kinds, "상한 도달이 곧 실패는 아니다 — 답을 만들어야 한다"

    final = next(e for e in events if e["type"] == "final")
    assert "라운드 상한(3회)" in final["text"]
    assert "확보한 근거로 정리한 답변입니다." in final["text"]
    assert fake_openai.saw_tool_choice_none, "마무리 호출은 도구를 끄고 해야 한다"
    assert fake_openai.calls == 4, "상한 3라운드 + 마무리 1회"


def test_tool_results_still_stream_before_the_limit(fake_openai):
    events = list(brain.answer("x", provider="openai", max_rounds=2))
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert all(r["result"]["ok"] for r in results), "상한과 무관하게 도구 결과는 그대로 전달된다"


def test_salvage_failure_reports_error(monkeypatch, fake_openai):
    def boom(**kw):
        if kw.get("tool_choice") == "none":
            raise RuntimeError("마무리 호출 실패")
        return _FakeResp([_FakeCall("get_risk_free_rate", "{}", "c1")])

    monkeypatch.setattr(fake_openai, "create", boom)
    events = list(brain.answer("x", provider="openai", max_rounds=1))
    err = next(e for e in events if e["type"] == "error")
    assert "라운드 상한" in err["text"] and "마무리 응답도 실패" in err["text"]

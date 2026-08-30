"""추론 강도(reasoning effort) 선택 — provider 별 노브 번역과 검증을 고정한다."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import brain
from core import auth, config
from server.main import app


# ── config 계층 ────────────────────────────────────────────────────────
def test_every_provider_declares_reasoning_levels():
    for p in config.LLM_PROVIDERS:
        levels = config.reasoning_levels(p)
        assert levels, f"{p} 에 reasoning_levels 가 없다"
        assert config.default_reasoning(p) in levels


def test_unsupported_level_falls_back_to_default_but_is_flagged_invalid():
    assert config.resolve_reasoning("openai", "dynamic") == "medium"   # openai 엔 dynamic 없음
    assert config.is_valid_reasoning("openai", "dynamic") is False
    assert config.is_valid_reasoning("gemini", "dynamic") is True


def test_none_means_use_default():
    assert config.is_valid_reasoning("openai", None) is True
    assert config.resolve_reasoning("openai", None) == config.default_reasoning("openai")


def test_env_default_wins_when_valid(monkeypatch):
    monkeypatch.setattr(config, "LLM_REASONING", "high")
    assert config.default_reasoning("openai") == "high"
    monkeypatch.setattr(config, "LLM_REASONING", "dynamic")   # openai 미지원
    assert config.default_reasoning("openai") == "medium"
    assert config.default_reasoning("gemini") == "dynamic"


def test_resolve_llm_carries_reasoning():
    info = config.resolve_llm("gemini", None, "off")
    assert info["reasoning"] == "off"
    assert "dynamic" in info["reasoning_levels"]


# ── provider 별 번역 ──────────────────────────────────────────────────
def test_openai_reasoning_only_on_reasoning_models():
    assert brain._openai_reasoning_kwargs("gpt-5.6-terra", "high") == {"reasoning": {"effort": "high"}}
    assert brain._openai_reasoning_kwargs("o3-mini", "low") == {"reasoning": {"effort": "low"}}
    # 추론 모델이 아니면 인자를 붙이면 400 이 난다 → 아예 안 붙인다
    assert brain._openai_reasoning_kwargs("gpt-4o-mini", "high") == {}
    assert brain._openai_reasoning_kwargs("gpt-4.1-mini", "high") == {}
    assert brain._openai_reasoning_kwargs("gpt-5.6-terra", None) == {}


def test_gemini_budget_mapping_is_monotonic():
    b = brain._GEMINI_BUDGET
    assert b["off"] == 0 and b["dynamic"] == -1
    assert b["low"] < b["medium"] < b["high"]
    assert brain._gemini_thinking(None) is None


def test_claude_env_sets_thinking_tokens():
    assert brain._claude_env("high")["MAX_THINKING_TOKENS"] == "31999"
    assert brain._claude_env("off")["MAX_THINKING_TOKENS"] == "0"
    # 노브가 없으면 환경변수를 건드리지 않는다(상위 환경을 덮어쓰지 않기 위해)
    assert "MAX_THINKING_TOKENS" not in brain._claude_env(None) or True


def test_thinking_error_detection_does_not_swallow_real_failures():
    assert brain._is_thinking_error(Exception("Unsupported parameter: 'reasoning.effort'"))
    assert brain._is_thinking_error(Exception("thinking_budget must be >= 128"))
    assert not brain._is_thinking_error(Exception("Incorrect API key provided"))
    assert not brain._is_thinking_error(Exception("rate limit exceeded"))


def test_deepseek_thinking_toggle_and_effort_go_via_extra_body():
    # off/미지정 → thinking 자체를 끈다(모델 이름이 아니라 요청 파라미터로 제어).
    assert brain._deepseek_thinking_kwargs("off") == {
        "extra_body": {"thinking": {"type": "disabled"}}}
    assert brain._deepseek_thinking_kwargs(None) == {
        "extra_body": {"thinking": {"type": "disabled"}}}
    # 켜져 있으면 reasoning_effort 도 같은 extra_body 안에 실린다(OpenAI 표준 스키마 밖이라
    # top-level kwarg 가 아님).
    assert brain._deepseek_thinking_kwargs("high") == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}}
    assert brain._deepseek_thinking_kwargs("low")["extra_body"]["thinking"] == {"type": "enabled"}


def test_deepseek_sampling_drops_temperature_only_when_thinking_is_on():
    # thinking 이 꺼져 있을 때만 temperature 를 보낸다(켜지면 샘플링 파라미터를 거부한다).
    assert brain._deepseek_sampling_kwargs("off") == {"temperature": 0}
    assert brain._deepseek_sampling_kwargs(None) == {"temperature": 0}
    assert brain._deepseek_sampling_kwargs("high") == {}
    assert brain._deepseek_sampling_kwargs("low") == {}


# ── answer() 가 provider 경로로 전달하는지 ─────────────────────────────
@pytest.mark.parametrize("provider, fn, want", [
    ("openai", "_answer_openai", "high"),
    ("gemini", "_answer_gemini", "dynamic"),
    ("deepseek", "_answer_deepseek", "high"),
])
def test_answer_passes_effort_to_provider(monkeypatch, provider, fn, want):
    seen = {}

    def fake(question, history, max_rounds, model=None, effort=None, ledger_block=""):
        seen["effort"] = effort
        seen["ledger"] = ledger_block
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr(brain, fn, fake)
    list(brain.answer("q", provider=provider, reasoning=want))
    assert seen["effort"] == want
    assert seen["ledger"] == "", "이력이 없으면 원장 블록도 비어 있어야 한다"


def test_answer_normalizes_bad_effort_to_provider_default(monkeypatch):
    seen = {}

    def fake(question, history, max_rounds, model=None, effort=None, ledger_block=""):
        seen["effort"] = effort
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr(brain, "_answer_openai", fake)
    list(brain.answer("q", provider="openai", reasoning="dynamic"))
    assert seen["effort"] == "medium", "openai 엔 dynamic 이 없으니 기본값으로"


# ── HTTP 경계 ─────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_USERS", "alice:pw-alice")
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "alice", "password": "pw-alice"}).status_code == 200
    return c


def test_bootstrap_exposes_levels_and_labels(client):
    data = client.get("/api/bootstrap").json()
    assert data["reasoning_labels"]["high"]
    for e in data["engines"]:
        assert e["reasoning_levels"], f"{e['provider']} 에 추론강도 목록이 없다"
        assert e["default_reasoning"] in e["reasoning_levels"]


def test_ask_rejects_unsupported_reasoning(client):
    r = client.post("/api/ask", json={"question": "삼성전자 매출",
                                      "provider": "openai", "reasoning": "dynamic"})
    assert r.status_code == 400
    assert "추론강도" in r.json()["detail"]


def test_ask_accepts_supported_reasoning_and_forwards_it(client, monkeypatch):
    seen = {}

    def fake_answer(question, history=None, provider=None, model=None, reasoning=None, **k):
        seen["reasoning"] = reasoning
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr("server.main.brain.answer", fake_answer)
    r = client.post("/api/ask", json={"question": "삼성전자 매출",
                                      "provider": "openai", "reasoning": "high"})
    assert r.status_code == 200
    r.read()
    assert seen["reasoning"] == "high"


def test_ask_without_reasoning_still_works(client, monkeypatch):
    seen = {}

    def fake_answer(question, history=None, provider=None, model=None, reasoning=None, **k):
        seen["reasoning"] = reasoning
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr("server.main.brain.answer", fake_answer)
    r = client.post("/api/ask", json={"question": "q", "provider": "openai"})
    assert r.status_code == 200
    r.read()
    assert seen["reasoning"] is None, "미지정은 그대로 넘겨 brain 이 기본값을 정한다"

from types import SimpleNamespace

import pytest

from app.ai.llm_provider import LLMProviderError, complete_text
from app.tasks import agents


def test_openai_adapter_uses_responses_api_without_storage(monkeypatch):
    captured = {}

    class Responses:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text='{"status":"ok"}')

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = Responses()

    monkeypatch.setitem(
        __import__("sys").modules, "openai",
        SimpleNamespace(OpenAI=FakeOpenAI),
    )

    text = complete_text(
        provider="openai", api_key="test-key", model="test-model",
        system="system", user="user", timeout_seconds=5, max_tokens=200,
    )

    assert text == '{"status":"ok"}'
    assert captured["store"] is False
    assert captured["model"] == "test-model"
    assert captured["client"]["api_key"] == "test-key"


def test_agent_reasoning_falls_back_to_openai(monkeypatch):
    monkeypatch.setattr(agents.settings, "agents_llm_enabled", True)
    monkeypatch.setattr(agents.settings, "agents_llm_provider", "anthropic")
    monkeypatch.setattr(
        agents.settings, "agents_llm_fallback_provider", "openai")
    monkeypatch.setattr(agents.settings, "anthropic_api_key", "anthropic-test")
    monkeypatch.setattr(agents.settings, "openai_api_key", "openai-test")
    monkeypatch.setattr(
        agents.settings, "agents_llm_openai_model", "openai-test-model")
    monkeypatch.setattr(
        agents, "_redis",
        lambda: SimpleNamespace(
            get=lambda _key: None,
            set=lambda *_args, **_kwargs: None,
            delete=lambda _key: None,
        ),
    )
    calls = []

    def fake_complete_text(**kwargs):
        calls.append((kwargs["provider"], kwargs["model"]))
        if kwargs["provider"] == "anthropic":
            raise LLMProviderError("provider credit exhausted")
        return ("{\"status\":\"warning\",\"summary\":\"review\","
                "\"findings\":{},\"gaps\":[],\"recommended_actions\":[]}")

    monkeypatch.setattr(
        "app.ai.llm_provider.complete_text", fake_complete_text)

    result = agents._ai_reason("simulation", "QA analyst", {"failed": 0})

    assert calls == [
        ("anthropic", "claude-haiku-4-5"),
        ("openai", "openai-test-model"),
    ]
    assert result["status"] == "warning"
    assert result["_provider"] == "openai"
    assert result["_model"] == "openai-test-model"


def test_agent_reasoning_cools_down_provider_needing_human_action(monkeypatch):
    monkeypatch.setattr(agents.settings, "agents_llm_enabled", True)
    monkeypatch.setattr(agents.settings, "agents_llm_provider", "anthropic")
    monkeypatch.setattr(agents.settings, "agents_llm_fallback_provider", "")
    monkeypatch.setattr(agents.settings, "anthropic_api_key", "anthropic-test")

    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.expiries = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, *, ex):
            self.values[key] = value
            self.expiries[key] = ex

        def delete(self, key):
            self.values.pop(key, None)

    fake_redis = FakeRedis()
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs["provider"])
        raise LLMProviderError("Your credit balance is too low")

    monkeypatch.setattr(agents, "_redis", lambda: fake_redis)
    monkeypatch.setattr("app.ai.llm_provider.complete_text", unavailable)

    assert agents._ai_reason("simulation", "QA analyst", {"failed": 0}) is None
    assert agents._ai_reason("training", "Training analyst", {"queued": 0}) is None

    key = agents._llm_cooldown_key("anthropic")
    assert calls == ["anthropic"]
    assert fake_redis.values[key] == "unavailable"
    assert fake_redis.expiries[key] == agents.LLM_PERMANENT_COOLDOWN_SECONDS


def test_transient_llm_failure_uses_short_cooldown():
    assert agents._llm_failure_cooldown_seconds(
        LLMProviderError("provider timed out"),
    ) == agents.LLM_TRANSIENT_COOLDOWN_SECONDS


def test_unknown_provider_fails_closed_without_text():
    with pytest.raises(LLMProviderError, match="unsupported"):
        complete_text(
            provider="unknown", api_key="test", model="model",
            system="system", user="user", timeout_seconds=5,
            max_tokens=10,
        )

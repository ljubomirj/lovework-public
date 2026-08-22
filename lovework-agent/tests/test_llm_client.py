"""Tests for visible-content handling in the OpenAI-compatible client."""

from __future__ import annotations

from types import SimpleNamespace

import llm_client


class _FakeCompletions:
    def create(self, **kwargs):
        del kwargs
        message = SimpleNamespace(
            content="",
            reasoning_content="private chain-of-thought that must not leak",
        )
        choice = SimpleNamespace(message=message, finish_reason="length")
        return SimpleNamespace(choices=[choice])


class _FakeOpenAI:
    def __init__(self, **kwargs):
        del kwargs
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def test_chat_does_not_promote_reasoning_to_visible_content(monkeypatch):
    monkeypatch.setattr(llm_client, "OpenAI", _FakeOpenAI)
    client = llm_client.LLMClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
    )

    result = client.chat(
        messages=[{"role": "user", "content": "Answer briefly."}],
        retries=1,
    )

    assert result == ""


def test_opencode_mimo_v25_uses_chat_completions(monkeypatch):
    monkeypatch.setattr(llm_client, "OpenAI", _FakeOpenAI)
    client = llm_client.LLMClient(
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1",
        model="mimo-v2.5",
    )

    assert client.provider == "opencode-go"
    assert client.model == "mimo-v2.5"
    assert client.api_style == "OpenAI-compatible Chat Completions"


def test_opencode_reasoning_effort_is_opt_in(monkeypatch):
    monkeypatch.setattr(llm_client, "OpenAI", _FakeOpenAI)
    client = llm_client.LLMClient(
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1",
        model="deepseek-v4-flash",
    )

    assert client.provider == "opencode-go"
    assert client.reasoning_effort is None


def test_opencode_reasoning_effort_is_sent_when_requested(monkeypatch):
    calls = []

    class CapturingCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            message = SimpleNamespace(content="OK", reasoning_content="")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")]
            )

    class CapturingOpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.chat = SimpleNamespace(completions=CapturingCompletions())

    monkeypatch.setattr(llm_client, "OpenAI", CapturingOpenAI)
    client = llm_client.LLMClient(
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1",
        model="deepseek-v4-flash",
    )
    client.chat(
        messages=[{"role": "user", "content": "Answer."}],
        max_tokens=32,
        reasoning_effort="high",
        retries=1,
    )

    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["max_tokens"] == 32


def test_openai_reasoning_model_uses_responses_api(monkeypatch):
    calls = []

    class CapturingResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text="OK", status="completed")

    class ResponsesOpenAI:
        def __init__(self, **kwargs):
            del kwargs
            self.responses = CapturingResponses()

    monkeypatch.setattr(llm_client, "OpenAI", ResponsesOpenAI)
    client = llm_client.LLMClient(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        model="gpt-5-mini",
        reasoning_effort="high",
    )
    result = client.chat(
        messages=[{"role": "user", "content": "Answer."}],
        max_tokens=64,
        retries=1,
    )

    assert result == "OK"
    assert client.api_style == "OpenAI Responses API"
    assert client.max_output_parameter == "max_output_tokens"
    assert calls[0]["max_output_tokens"] == 64
    assert calls[0]["reasoning"] == {"effort": "high"}

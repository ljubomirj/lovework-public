"""Tests for private one-turn-delayed advisor comments."""

from __future__ import annotations

import json

from interview_providers.side_advisor import SideAdvisor


class StubAdvisorClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps(self.payload)


def test_advisor_comment_is_available_only_on_next_turn():
    client = StubAdvisorClient({
        "intervene": True,
        "action": "clarify",
        "guidance": "Ask for the concrete trade-off.",
        "evidence_refs": [],
    })
    advisor = SideAdvisor(
        side="interviewer",
        llm_client=client,
        role_title="Product Engineer",
        company_name="Hyperspell",
    )

    advisor.observe(1, [{"role": "interviewer", "content": "Question"}])
    prompt = advisor.take_pending_prompt()

    assert prompt is not None
    assert "Ask for the concrete trade-off." in prompt
    assert advisor.take_pending_prompt() is None
    assert client.calls[0]["kwargs"]["reasoning_effort"] == "none"


def test_candidate_advisor_prompt_is_private_and_non_evidentiary():
    client = StubAdvisorClient({
        "intervene": True,
        "action": "answer",
        "guidance": "Use the permitted Python claim only.",
        "evidence_refs": ["python"],
    })
    advisor = SideAdvisor(
        side="candidate",
        llm_client=client,
        role_title="Product Engineer",
        company_name="Hyperspell",
        permitted_claims=[{"id": "python", "claim": "Uses Python."}],
    )

    advisor.observe(4, [{"role": "interviewer", "content": "Tell me about Python."}])
    prompt = advisor.take_pending_prompt()
    event_text = json.dumps(advisor.events)

    assert prompt is not None
    assert "same side only" in prompt
    assert "cannot authorize any claim" in prompt
    assert "Use the permitted Python claim only." in prompt
    assert "Use the permitted Python claim only." not in event_text
    generated = next(event for event in advisor.events if event["kind"] == "advisor_comment_generated")
    assert generated["available_next_turn"] == 5

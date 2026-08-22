"""Phase E.1 tests for the MockInterviewer.

Tests use a stub LLM client to avoid real API calls.
The MockInterviewer should produce correct stage transitions, event logging,
and style enforcement without depending on a real LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from interview_providers.mock_interviewer import (
    MockInterviewer,
    STYLE_INSTRUCTIONS,
    _body_digest,
)
from interview_providers.rehearsal_settings import INTERVIEWER_MAX_TOKENS


# ── Stub LLM client ──────────────────────────────────────────────────────


class StubLLMClient:
    """A deterministic LLM client that returns canned responses.

    Supports:
    - Default response for all calls
    - Per-call responses via a queue
    - Response templates that vary by turn count
    """

    def __init__(self, responses: list[str] | None = None, default: str | None = None):
        self._responses = list(responses) if responses else []
        self._default = default or "[Stub interviewer response]"
        self._call_count = 0
        self._calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self._call_count += 1
        self._calls.append({"messages": messages, "kwargs": kwargs})
        if self._responses:
            return self._responses.pop(0)
        return self._default

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def last_messages(self) -> list[dict[str, str]]:
        return self._calls[-1]["messages"] if self._calls else []


# ── Role text fixture ────────────────────────────────────────────────────

SAMPLE_ROLE_TEXT = (
    "Product Engineer at Hyperspell\n\n"
    "Hyperspell is the Memory & Context Layer for AI Agents.\n\n"
    "As a Product Engineer, your mission is to set our customers up for success "
    "and advance what's possible with AI agents.\n\n"
    "What you'll do:\n"
    "- Evolving the self-integrating SDK\n"
    "- Owning customer relationships\n"
    "- Full-stack feature ownership\n"
    "- Shaping the product direction\n\n"
    "What we're looking for:\n"
    "- 3+ years of experience in product engineering\n"
    "- Fluency in Python and TypeScript\n"
    "- Clarity of thought and excellent communication\n"
    "- A genuine joy in helping other engineers succeed\n"
    "- Ability to work on location in San Francisco"
)

SAMPLE_PREP_TEXT = (
    "Agent Setup — Product Engineer at Hyperspell\n\n"
    "The interview is a series of stages. Each stage is a separate conversation "
    "between the interviewer agent and your agent.\n\n"
    "Interviewer agent → sends task → Your agent responds → follow-ups → next stage"
)


# ── Constructor tests ────────────────────────────────────────────────────


class TestMockInterviewerConstruction:
    def test_valid_styles_accepted(self):
        for style in ("neutral", "skeptical", "adversarial", "rushed", "friendly"):
            interviewer = MockInterviewer(
                role_text=SAMPLE_ROLE_TEXT, style=style
            )
            assert interviewer.style == style

    def test_invalid_style_raises(self):
        with pytest.raises(ValueError, match="Invalid style"):
            MockInterviewer(role_text=SAMPLE_ROLE_TEXT, style="mean")

    def test_default_stage_state(self):
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT)
        stages = interviewer.stages
        assert len(stages) == 5  # system_init + 4 manual
        assert stages[0]["stage_name"] == "system_init"
        assert stages[0]["status"] == "completed"
        assert stages[1]["status"] == "pending"
        assert stages[2]["status"] == "pending"
        assert stages[3]["status"] == "pending"

    def test_current_stage_starts_at_1(self):
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT)
        assert interviewer.current_stage == 1


# ── open_stage tests ─────────────────────────────────────────────────────


class TestOpenStage:
    def test_open_stage_returns_llm_response(self):
        llm = StubLLMClient(responses=["Tell me about your experience with AI agents."])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        opening = interviewer.open_stage(1)
        assert opening == "Tell me about your experience with AI agents."
        assert llm.call_count == 1
        assert llm._calls[-1]["kwargs"]["max_tokens"] == INTERVIEWER_MAX_TOKENS

    def test_open_stage_sets_status_to_awaiting_input(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        assert interviewer.stages[1]["status"] == "awaiting_input"

    def test_open_stage_logs_event(self):
        llm = StubLLMClient(responses=["Opening question"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        events = interviewer.events
        assert len(events) == 1
        assert events[0]["kind"] == "interviewer_open_stage"
        assert events[0]["stage_number"] == 1
        assert events[0]["style"] == "neutral"

    def test_open_stage_rejects_automated_stage(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        with pytest.raises(ValueError, match="not a manual stage"):
            interviewer.open_stage(0)  # system_init is automated

    def test_open_stage_strips_close_stage_marker(self):
        llm = StubLLMClient(responses=["Welcome!\n\n[CLOSE_STAGE]"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        opening = interviewer.open_stage(1)
        assert "[CLOSE_STAGE]" not in opening
        assert "Welcome!" in opening

    def test_open_stage_builds_system_prompt_with_role_text(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        system_msg = llm.last_messages[0]
        assert system_msg["role"] == "system"
        assert "Hyperspell" in system_msg["content"]
        assert "Product Engineer" in system_msg["content"]

    def test_open_stage_includes_prep_text_when_provided(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(
            role_text=SAMPLE_ROLE_TEXT,
            prep_text=SAMPLE_PREP_TEXT,
            llm_client=llm,
        )
        interviewer.open_stage(1)
        system_msg = llm.last_messages[0]
        assert "INTERVIEW PREPARATION GUIDE" in system_msg["content"]
        assert "series of stages" in system_msg["content"]


# ── respond tests ────────────────────────────────────────────────────────


class TestRespond:
    def test_respond_returns_llm_reply(self):
        llm = StubLLMClient(responses=[
            "Opening",  # open_stage
            "What specific AI systems have you built?",  # respond
        ])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        reply = interviewer.respond(1, "I'm an ML engineer with 12 years of experience.")
        assert reply == "What specific AI systems have you built?"

    def test_respond_records_candidate_message_in_transcript(self):
        llm = StubLLMClient(responses=["Opening", "Follow-up"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        interviewer.respond(1, "I worked on speech recognition systems.")
        transcript = interviewer.get_transcript(1)
        # Should have: assistant (opening), user (candidate), assistant (reply)
        assert len(transcript) == 3
        assert transcript[0]["role"] == "assistant"
        assert transcript[1]["role"] == "user"
        assert transcript[1]["content"] == "I worked on speech recognition systems."
        assert transcript[2]["role"] == "assistant"

    def test_respond_logs_event_with_turn_count(self):
        llm = StubLLMClient(responses=["Opening", "Follow-up"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        interviewer.respond(1, "My answer")
        events = interviewer.events
        respond_events = [e for e in events if e["kind"] == "interviewer_respond"]
        assert len(respond_events) == 1
        assert respond_events[0]["body"]["turn"] == 1

    def test_respond_closes_stage_on_marker(self):
        llm = StubLLMClient(responses=[
            "Opening",
            "Thanks, that covers it. [CLOSE_STAGE]",
        ])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        reply = interviewer.respond(1, "My comprehensive answer.")
        assert "[CLOSE_STAGE]" not in (reply or "")
        assert interviewer.stages[1]["status"] == "completed"

    def test_respond_rejects_completed_stage(self):
        llm = StubLLMClient(responses=[
            "Opening",
            "Done. [CLOSE_STAGE]",
            "Too late",
        ])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        interviewer.respond(1, "Answer 1")
        with pytest.raises(ValueError, match="already completed"):
            interviewer.respond(1, "Answer 2")

    def test_respond_force_closes_at_max_turns(self):
        llm = StubLLMClient(responses=[
            "Opening",
            "Turn 1",  # turn 1
            "Turn 2",  # turn 2 — should force close
        ])
        interviewer = MockInterviewer(
            role_text=SAMPLE_ROLE_TEXT,
            max_turns_per_stage=2,
            llm_client=llm,
        )
        interviewer.open_stage(1)
        interviewer.respond(1, "Answer 1")
        interviewer.respond(1, "Answer 2")
        # Stage should be completed after max turns
        assert interviewer.stages[1]["status"] == "completed"


# ── Style tests ──────────────────────────────────────────────────────────


class TestStyle:
    def test_style_appears_in_system_prompt(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(
            role_text=SAMPLE_ROLE_TEXT, style="skeptical", llm_client=llm
        )
        interviewer.open_stage(1)
        system_msg = llm.last_messages[0]
        # The style instruction text should appear in the system prompt
        assert STYLE_INSTRUCTIONS["skeptical"] in system_msg["content"]

    def test_adversarial_style_adds_pressure_instructions(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(
            role_text=SAMPLE_ROLE_TEXT, style="adversarial", llm_client=llm
        )
        interviewer.open_stage(1)
        system_msg = llm.last_messages[0]
        # Adversarial-specific instructions should appear
        assert "stress-testing" in system_msg["content"].lower()
        assert "ADVERSARIAL INSTRUCTIONS" in system_msg["content"]

    def test_all_styles_in_event_log(self):
        for style in ("neutral", "skeptical", "adversarial", "rushed", "friendly"):
            llm = StubLLMClient(responses=["Opening"])
            interviewer = MockInterviewer(
                role_text=SAMPLE_ROLE_TEXT, style=style, llm_client=llm
            )
            interviewer.open_stage(1)
            assert interviewer.events[0]["style"] == style


# ── Event persistence tests ──────────────────────────────────────────────


class TestEventPersistence:
    def test_write_events_jsonl(self, tmp_path: Path):
        llm = StubLLMClient(responses=["Opening"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        out_path = tmp_path / "events.jsonl"
        count = interviewer.write_events(out_path)
        assert count == 1
        lines = out_path.read_text().strip().split("\n")
        row = json.loads(lines[0])
        assert row["kind"] == "interviewer_open_stage"
        assert row["style"] == "neutral"

    def test_write_events_creates_parent_dirs(self, tmp_path: Path):
        llm = StubLLMClient(responses=["Opening"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        out_path = tmp_path / "nested" / "dir" / "events.jsonl"
        interviewer.write_events(out_path)
        assert out_path.exists()


# ── Transcript tests ─────────────────────────────────────────────────────


class TestTranscript:
    def test_transcript_empty_before_open(self):
        llm = StubLLMClient()
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        assert interviewer.get_transcript(1) == []

    def test_transcript_after_open_and_respond(self):
        llm = StubLLMClient(responses=["Opening", "Follow-up"])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        interviewer.respond(1, "My answer")
        transcript = interviewer.get_transcript(1)
        assert len(transcript) == 3
        assert transcript[0]["role"] == "assistant"  # opening
        assert transcript[1]["role"] == "user"       # candidate
        assert transcript[2]["role"] == "assistant"   # reply

    def test_transcript_all_stages(self):
        llm = StubLLMClient(responses=[
            "Opening 1", "Reply 1",
            "Opening 2", "Reply 2",
        ])
        interviewer = MockInterviewer(role_text=SAMPLE_ROLE_TEXT, llm_client=llm)
        interviewer.open_stage(1)
        interviewer.respond(1, "Answer 1")
        # Stage 1 not closed yet — open stage 2 manually
        # (In real use, close_stage would advance; here we test transcript aggregation)
        interviewer.open_stage(2)
        interviewer.respond(2, "Answer 2")
        all_transcript = interviewer.get_transcript()
        # Should have entries from both stages
        assert len(all_transcript) >= 4


# ── body_digest helper ──────────────────────────────────────────────────


class TestBodyDigest:
    def test_deterministic(self):
        assert _body_digest("hello") == _body_digest("hello")
        assert _body_digest("hello") != _body_digest("world")

    def test_prefix(self):
        assert _body_digest("test").startswith("sha256:")


# ── Integration-style test ───────────────────────────────────────────────


class TestFullStageFlow:
    def test_three_stage_interview(self):
        """Run a complete 3-stage interview with canned responses."""
        responses = [
            # Stage 1: intro (open + 2 turns + close)
            "Welcome to Hyperspell. Tell me about your background in AI.",
            "Interesting. Tell me more about the specific AI systems you've built.",
            "That's thorough. Let's move on. [CLOSE_STAGE]",
            # Stage 2: technical (open + 2 turns + close)
            "How would you design a memory layer for AI agents?",
            "What about retrieval — how would you handle long-context queries?",
            "Good. Let's continue. [CLOSE_STAGE]",
            # Stage 3: customer (open + 1 turn + close)
            "A customer reports their AI agent is forgetting context mid-conversation. How do you diagnose this?",
            "That's a solid approach. [CLOSE_STAGE]",
        ]
        llm = StubLLMClient(responses=responses)
        interviewer = MockInterviewer(
            role_text=SAMPLE_ROLE_TEXT,
            prep_text=SAMPLE_PREP_TEXT,
            style="neutral",
            llm_client=llm,
        )

        # Stage 1
        opening1 = interviewer.open_stage(1)
        assert "background" in opening1.lower() or "ai" in opening1.lower()
        interviewer.respond(1, "I have 12 years in ML, including speech recognition at Canon Research.")
        interviewer.respond(1, "I built production ASR systems and quant trading platforms.")
        # Stage should close
        assert interviewer.stages[1]["status"] == "completed"

        # Stage 2
        opening2 = interviewer.open_stage(2)
        assert "memory" in opening2.lower() or "design" in opening2.lower()
        interviewer.respond(2, "I'd use a vector store with hierarchical retrieval and decay weighting.")
        interviewer.respond(2, "I'd use chunked retrieval with relevance reranking for long contexts.")
        assert interviewer.stages[2]["status"] == "completed"

        # Stage 3
        opening3 = interviewer.open_stage(3)
        assert "customer" in opening3.lower() or "forgetting" in opening3.lower()
        interviewer.respond(3, "I'd check the context window limits, retrieval pipeline, and session state management.")
        assert interviewer.stages[3]["status"] == "completed"

        # Verify events
        events = interviewer.events
        open_events = [e for e in events if e["kind"] == "interviewer_open_stage"]
        respond_events = [e for e in events if e["kind"] == "interviewer_respond"]
        close_events = [e for e in events if e["kind"] == "interviewer_close_stage"]
        assert len(open_events) == 3
        assert len(respond_events) == 5  # 2 + 2 + 1
        assert len(close_events) == 3

        # Verify LLM calls
        assert llm.call_count == 8  # 3 opens + 5 responds


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

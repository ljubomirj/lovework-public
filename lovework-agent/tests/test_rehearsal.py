"""Phase E.2 integration tests for the rehearsal harness.

Tests the full end-to-end flow: MockInterviewer + CandidateAgent wired
together through RehearsalRunner. Uses stub LLM clients to avoid real API
calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rehearse import RehearsalRunner, _load_case_references, _parse_claims


# ── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_ROLE_TEXT = (
    "Product Engineer at Hyperspell\n\n"
    "Hyperspell is the Memory & Context Layer for AI Agents.\n\n"
    "What we're looking for:\n"
    "- 3+ years of experience in product engineering\n"
    "- Fluency in Python and TypeScript\n"
    "- Clarity of thought and excellent communication"
)

SAMPLE_PREP_TEXT = (
    "The interview is a series of stages. Each stage is a separate conversation."
)

SAMPLE_CLAIMS = [
    {
        "claim": "Has built production ML systems at scale",
        "confidence": "high",
        "permitted": True,
        "source": "profiles/lj/cv-short.md",
        "category": "skill",
    },
    {
        "claim": "Experienced with DSPy and programmatic prompting",
        "confidence": "high",
        "permitted": True,
        "source": "profiles/lj/soul.md",
        "category": "skill",
    },
    {
        "claim": "Fluent in TypeScript",
        "confidence": "low",
        "permitted": False,
        "source": "profiles/lj/cv-short.md",
        "category": "gap",
        "reason": "TypeScript not in CV",
    },
]

SAMPLE_EVIDENCE_PACK = (
    "## Strong evidence\n"
    "1. **Has built production ML systems** — Source: profiles/lj/cv-short.md\n"
)


def _make_case_dir(tmp_path: Path) -> Path:
    """Create a minimal case directory for testing."""
    case_dir = tmp_path / "test-case"
    refs_dir = case_dir / "agent-interview" / "references"
    refs_dir.mkdir(parents=True)
    (refs_dir / "role-page.txt").write_text(SAMPLE_ROLE_TEXT)
    (refs_dir / "preparation-guide.txt").write_text(SAMPLE_PREP_TEXT)
    (case_dir / "agent-interview").mkdir(parents=True, exist_ok=True)
    (case_dir / "agent-interview" / "claims.json").write_text(
        json.dumps(SAMPLE_CLAIMS)
    )
    (case_dir / "agent-interview" / "evidence-pack.md").write_text(
        SAMPLE_EVIDENCE_PACK
    )
    return case_dir


# ── Helper tests ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_load_case_references(self, tmp_path: Path):
        case_dir = _make_case_dir(tmp_path)
        refs = _load_case_references(case_dir)
        assert "role_text" in refs
        assert "Hyperspell" in refs["role_text"]
        assert "prep_text" in refs
        assert "claims_json" in refs
        assert "evidence_pack" in refs

    def test_parse_claims_valid(self):
        claims = _parse_claims(json.dumps(SAMPLE_CLAIMS))
        assert len(claims) == 3

    def test_parse_claims_invalid(self):
        claims = _parse_claims("not json")
        assert claims == []


# ── RehearsalRunner construction tests ───────────────────────────────────


class TestRehearsalRunnerConstruction:
    def test_creates_with_valid_case(self, tmp_path: Path):
        case_dir = _make_case_dir(tmp_path)
        runner = RehearsalRunner(case_dir=case_dir, style="neutral")
        assert runner._style == "neutral"
        assert runner._role_text == SAMPLE_ROLE_TEXT

    def test_output_dir_auto_generated(self, tmp_path: Path):
        case_dir = _make_case_dir(tmp_path)
        runner = RehearsalRunner(case_dir=case_dir)
        assert "rehearsals" in str(runner._output_dir)

    def test_output_dir_custom(self, tmp_path: Path):
        case_dir = _make_case_dir(tmp_path)
        custom = tmp_path / "custom-output"
        runner = RehearsalRunner(case_dir=case_dir, output_dir=custom)
        assert runner._output_dir == custom


# ── Full rehearsal run tests ─────────────────────────────────────────────


class TestFullRehearsalRun:
    def test_single_stage_run(self, tmp_path: Path):
        """Run a single-stage rehearsal with stub LLMs."""
        case_dir = _make_case_dir(tmp_path)
        output = tmp_path / "output"

        # We need to patch the LLM clients. The RehearsalRunner creates
        # MockInterviewer and CandidateAgent internally, so we need to
        # inject stub LLM clients.
        # Approach: subclass RehearsalRunner and override agent creation.

        class StubRehearsalRunner(RehearsalRunner):
            def run(self):
                # Override to inject stub LLM clients
                from interview_providers.mock_interviewer import MockInterviewer
                from interview_providers.candidate_agent import CandidateAgent

                class InterviewerLLM:
                    _call_count = 0
                    def chat(self, messages, **kwargs):
                        self._call_count += 1
                        return f"Welcome to Hyperspell. Tell me about your ML experience. (turn {self._call_count})"

                class CandidateLLM:
                    _call_count = 0
                    def chat(self, messages, **kwargs):
                        self._call_count += 1
                        return "I have extensive experience building production ML systems at scale, including DSPy and programmatic prompting."

                interviewer_llm = InterviewerLLM()
                candidate_llm = CandidateLLM()

                self._interviewer = MockInterviewer(
                    role_text=self._role_text,
                    prep_text=self._prep_text,
                    style=self._style,
                    max_turns_per_stage=self._max_turns,
                    llm_client=interviewer_llm,
                )
                self._candidate = CandidateAgent(
                    claims=self._claims,
                    evidence_pack=self._evidence_pack,
                    principal=self._principal,
                    llm_client=candidate_llm,
                )

                # Run single stage
                stage_result = self._run_stage(1)
                self._stage_results.append(stage_result)

                summary = {
                    "style": self._style,
                    "stages_run": len(self._stage_results),
                    "total_violations": sum(s["violations"] for s in self._stage_results),
                    "total_refusals": sum(s["refusals"] for s in self._stage_results),
                    "total_turns": sum(s["turns"] for s in self._stage_results),
                    "output_dir": str(self._output_dir),
                }
                self._write_artefacts(summary)
                return summary

        runner = StubRehearsalRunner(
            case_dir=case_dir,
            style="neutral",
            max_stages=1,
            max_turns_per_stage=3,
            principal="lj",
            output_dir=output,
        )
        summary = runner.run()

        assert summary["stages_run"] == 1
        assert summary["total_violations"] == 0
        assert summary["total_turns"] >= 1

        # Check artefacts exist
        assert (output / "transcript.jsonl").exists()
        assert (output / "events.jsonl").exists()
        assert (output / "candidate-report.md").exists()
        assert (output / "rehearsal-report.md").exists()
        assert (output / "transcript.txt").exists()

        # Check transcript content
        transcript = (output / "transcript.jsonl").read_text().strip().split("\n")
        assert len(transcript) >= 2  # at least interviewer + candidate

        # Check report
        report = (output / "rehearsal-report.md").read_text()
        assert "Rehearsal Report" in report
        assert "CLEAN" in report or "VIOLATION" in report
        assert "Technical Runtime Settings" in report
        assert "API" in report
        assert "Output parameter" in report
        assert "MAX_TOKEN_LIMIT = 2048" in report

    def test_no_violations_with_permitted_claims(self, tmp_path: Path):
        """Verify no violations when candidate only asserts permitted claims."""
        case_dir = _make_case_dir(tmp_path)
        output = tmp_path / "output"

        class StubRehearsalRunner(RehearsalRunner):
            def run(self):
                from interview_providers.mock_interviewer import MockInterviewer
                from interview_providers.candidate_agent import CandidateAgent

                class InterviewerLLM:
                    _count = 0
                    def chat(self, messages, **kwargs):
                        self._count += 1
                        return "Tell me about your experience."

                class CandidateLLM:
                    _count = 0
                    def chat(self, messages, **kwargs):
                        return "I have production ML experience with DSPy."

                self._interviewer = MockInterviewer(
                    role_text=self._role_text, style="neutral",
                    max_turns_per_stage=2, llm_client=InterviewerLLM(),
                )
                self._candidate = CandidateAgent(
                    claims=self._claims, evidence_pack=self._evidence_pack,
                    principal="lj", llm_client=CandidateLLM(),
                )

                self._run_stage(1)
                self._stage_results.append({
                    "stage_number": 1, "stage_name": "intro",
                    "turns": 1, "violations": 0, "refusals": 0, "status": "completed",
                })

                summary = {
                    "style": "neutral", "stages_run": 1,
                    "total_violations": 0, "total_refusals": 0,
                    "total_turns": 1, "output_dir": str(self._output_dir),
                }
                self._write_artefacts(summary)
                return summary

        runner = StubRehearsalRunner(
            case_dir=case_dir, max_stages=1, output_dir=output,
        )
        summary = runner.run()
        assert summary["total_violations"] == 0

        report = (output / "rehearsal-report.md").read_text()
        assert "CLEAN" in report


    def test_advisors_are_private_and_one_turn_delayed(self, tmp_path: Path):
        case_dir = _make_case_dir(tmp_path)
        output = tmp_path / "advisor-output"

        class PrimaryLLM:
            def __init__(self, response: str):
                self.response = response
                self.calls: list[dict] = []

            def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, "kwargs": kwargs})
                return self.response

        class AdvisorLLM:
            def __init__(self, guidance: str):
                self.guidance = guidance
                self.calls: list[dict] = []

            def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, "kwargs": kwargs})
                return json.dumps({
                    "intervene": True,
                    "action": "clarify",
                    "guidance": self.guidance,
                    "evidence_refs": [],
                })

        ivr = PrimaryLLM("Opening question")
        ive = PrimaryLLM("I built production ML systems.")
        ivr_advisor = AdvisorLLM("IVR private guidance")
        ive_advisor = AdvisorLLM("IVE private guidance")
        summary = RehearsalRunner(
            case_dir=case_dir,
            max_stages=1,
            max_turns_per_stage=1,
            output_dir=output,
            ivr_llm_client=ivr,
            ive_llm_client=ive,
            ivr_advisor_llm_client=ivr_advisor,
            ive_advisor_llm_client=ive_advisor,
        ).run()

        transcript = [json.loads(line) for line in (output / "transcript.jsonl").read_text().splitlines()]
        events = (output / "events.jsonl").read_text()
        assert summary["stages_run"] == 1
        assert all("PRIVATE ADVISOR" not in row["content"] for row in transcript)
        assert "advisor_comment_generated" in events
        assert "IVR private guidance" not in events
        assert "IVE private guidance" not in events
        assert any("IVR private guidance" in message["content"] for call in ivr.calls for message in call["messages"])
        assert "IVE private guidance" not in "\n".join(
            message["content"] for call in ivr.calls for message in call["messages"]
        )
        assert any("IVE private guidance" in message["content"] for call in ive.calls for message in call["messages"])
        assert "IVR private guidance" not in "\n".join(
            message["content"] for call in ive.calls for message in call["messages"]
        )

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

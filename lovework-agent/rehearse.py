"""Rehearsal harness — runs a full mock interview (Phase E.2).

Wires MockInterviewer (company side) and CandidateAgent (our side) into a
closed-loop conversation. Produces transcript, events, and a report with
claim-adherence analysis.

Usage::

    ../venv/bin/python3 rehearse.py \\
        --case applications/20260723-Hyperspell-Product_Engineer-LoveWork-ATA/ \\
        --style neutral \\
        --max-stages 3

    # Multi-style matrix
    ../venv/bin/python3 rehearse.py \\
        --case applications/20260723-Hyperspell-Product_Engineer-LoveWork-ATA/ \\
        --matrix --runs-per-style 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolve project root for imports
_AGENT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _AGENT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from interview_providers.mock_interviewer import MockInterviewer  # noqa: E402
from interview_providers.candidate_agent import CandidateAgent  # noqa: E402
from interview_providers.side_advisor import SideAdvisor  # noqa: E402
from interview_providers.rehearsal_settings import (  # noqa: E402
    ADVISOR_MAX_TOKENS,
    ADVISOR_REASONING_EFFORT,
    ADVISOR_TEMPERATURE,
    ADVISOR_DELAY_TURNS,
    AUDIT_REASONING_EFFORT,
    PRIMARY_REASONING_EFFORT,
    CANDIDATE_MAX_TOKENS,
    CANDIDATE_TEMPERATURE,
    CANDIDATE_VISIBLE_WORD_LIMIT,
    DEFAULT_MAX_STAGES,
    DEFAULT_MAX_TURNS_PER_STAGE,
    EVIDENCE_PACK_MAX_CHARS,
    INTERLEAVED_THINKING,
    INTERVIEWER_MAX_TOKENS,
    INTERVIEWER_TEMPERATURE,
    INTERVIEWER_VISIBLE_WORD_LIMIT,
    LLM_REQUEST_RETRIES,
    MAX_TOKEN_LIMIT,
    MAX_TOKEN_REASONING_LIMIT,
    PREPARATION_CONTEXT_MAX_CHARS,
    ROLE_CONTEXT_MAX_CHARS,
    VIOLATION_AUDIT_MAX_TOKENS,
    VIOLATION_AUDIT_TEMPERATURE,
)

logger = logging.getLogger(__name__)


# ── Model configuration ──────────────────────────────────────────────────
#
# Provider routing rule of thumb:
#   1. Opencode-go (subscription, already paid) — preferred
#   2. OpenRouter (PAYG) — for models not on Opencode-go
#   3. DeepSeek API (PAYG) — only for DeepSeek models, rarely needed
#
# A model like deepseek-chat is available on all 3. Prefer Opencode-go.
# Anthropic/OpenAI models are only on OpenRouter.


# Provider definitions: (base_url, env_var_for_key)
PROVIDERS: dict[str, tuple[str, str]] = {
    "opencode-go": ("https://opencode.ai/zen/go/v1", "OPENCODE_GO_LJ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
}

# Model availability: which provider serves which model
# Key = model name as passed to --ivr-model/--ive-model
# Value = list of providers that serve it, in preference order
MODEL_PROVIDERS: dict[str, list[str]] = {
    # Mimo is the default crawl model on the OpenCode Go subscription.
    "mimo-v2.5": ["opencode-go"],
    # DeepSeek models — available on all 3, prefer opencode-go
    "deepseek-chat": ["opencode-go", "deepseek", "openrouter"],
    "deepseek-v4-flash": ["opencode-go", "deepseek", "openrouter"],
    "deepseek-reasoner": ["opencode-go", "deepseek", "openrouter"],
    # OpenRouter models — only on openrouter
    "openrouter/anthropic/claude-sonnet-4": ["openrouter"],
    "openrouter/anthropic/claude-haiku-3.5": ["openrouter"],
    "openrouter/openai/gpt-4o": ["openrouter"],
    "openrouter/google/gemini-2.0-flash": ["openrouter"],
    "openrouter/deepseek/deepseek-chat-v3-0324": ["openrouter"],
    "openrouter/deepseek/deepseek-chat-v3-0324:free": ["openrouter"],
}

# Model name mapping: provider → (generic_name → provider-specific name)
# OpenCode Go uses different model names than DeepSeek API.
MODEL_NAME_MAP: dict[str, dict[str, str]] = {
    "opencode-go": {
        "mimo-v2.5": "mimo-v2.5",
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-reasoner",
    },
    "deepseek": {
        "deepseek-chat": "deepseek-chat",
        "deepseek-v4-flash": "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
    },
    "openrouter": {
        "openrouter/anthropic/claude-sonnet-4": "anthropic/claude-sonnet-4",
        "openrouter/anthropic/claude-haiku-3.5": "anthropic/claude-haiku-3.5",
        "openrouter/openai/gpt-4o": "openai/gpt-4o",
        "openrouter/google/gemini-2.0-flash": "google/gemini-2.0-flash",
        "openrouter/deepseek/deepseek-chat-v3-0324": "deepseek/deepseek-chat-v3-0324",
        "openrouter/deepseek/deepseek-chat-v3-0324:free": "deepseek/deepseek-chat-v3-0324:free",
    },
}
def _resolve_provider(model: str) -> tuple[str, str, str]:
    """Resolve (model_name, base_url, api_key) for a model.

    Uses MODEL_PROVIDERS to pick the best available provider.
    Applies MODEL_NAME_MAP to translate model names per provider.
    Falls back to env vars.
    """
    providers = MODEL_PROVIDERS.get(model, [])

    for provider_name in providers:
        base_url, env_var = PROVIDERS[provider_name]
        api_key = os.getenv(env_var)
        if api_key:
            # Apply model name mapping for this provider
            name_map = MODEL_NAME_MAP.get(provider_name, {})
            resolved_model = name_map.get(model, model)
            return resolved_model, base_url, api_key

    # No provider found — fall back to config defaults
    return model, "", ""


def _make_llm_client(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    """Create an LLMClient with optional overrides.

    If only model is provided (no base_url/api_key), resolves the best
    provider automatically. Falls back to config defaults if nothing specified.
    Lazy-imports LLMClient to avoid openai import-time network checks.
    """
    from llm_client import LLMClient

    kwargs: dict[str, Any] = {}
    if model and not base_url:
        # Auto-resolve provider
        resolved_model, resolved_url, resolved_key = _resolve_provider(model)
        kwargs["model"] = resolved_model
        if resolved_url:
            kwargs["base_url"] = resolved_url
        if resolved_key:
            kwargs["api_key"] = resolved_key
    else:
        # Explicit overrides
        if model:
            kwargs["model"] = model
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
    return LLMClient(**kwargs)


# Scenario presets: (ivr_model, ive_model)
# Models are resolved via MODEL_PROVIDERS — cheapest provider wins.
# NOTE: Reasoning models (deepseek-reasoner) produce chain-of-thought in
# reasoning_content, not clean answers in content. Use non-reasoning models
# for agent-to-agent rehearsal.
SCENARIO_PRESETS: dict[str, tuple[str, str]] = {
    "balanced": ("deepseek-chat", "deepseek-chat"),
    "strong-ivr": ("openrouter/anthropic/claude-sonnet-4", "deepseek-chat"),
    "weak-ive": ("deepseek-chat", "openrouter/deepseek/deepseek-chat-v3-0324:free"),
    "both-deepseek": ("deepseek-chat", "deepseek-chat"),
    "claude-vs-deepseek": ("openrouter/anthropic/claude-sonnet-4", "deepseek-chat"),
    "claude-vs-claude": ("openrouter/anthropic/claude-sonnet-4", "openrouter/anthropic/claude-sonnet-4"),
    "gpt4o-vs-deepseek": ("openrouter/openai/gpt-4o", "deepseek-chat"),
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_dir() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _load_case_references(case_dir: Path) -> dict[str, str]:
    """Load role page, preparation guide, claims, and manifest from a case directory."""
    refs_dir = case_dir / "agent-interview" / "references"
    result: dict[str, str] = {}

    role_path = refs_dir / "role-page.txt"
    if role_path.exists():
        result["role_text"] = role_path.read_text(encoding="utf-8")

    prep_path = refs_dir / "preparation-guide.txt"
    if prep_path.exists():
        result["prep_text"] = prep_path.read_text(encoding="utf-8")

    claims_path = case_dir / "agent-interview" / "claims.json"
    if claims_path.exists():
        result["claims_json"] = claims_path.read_text(encoding="utf-8")

    evidence_path = case_dir / "agent-interview" / "evidence-pack.md"
    if evidence_path.exists():
        result["evidence_pack"] = evidence_path.read_text(encoding="utf-8")

    # Manifest carries company + position — needed so the LLM prompts
    # don't hardcode "Hyperspell" / "Product Engineer" (generalisation bug
    # caught 2026-07-29 review).
    manifest_path = case_dir / "agent-interview" / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result["company_name"] = manifest.get("company", "the company")
            result["role_title"] = manifest.get("position", "the role")
        except json.JSONDecodeError:
            logger.warning("Failed to parse manifest.json; using generic role/company")

    return result


def _parse_claims(claims_json: str) -> list[dict[str, Any]]:
    """Parse claims.json content into a list of dicts."""
    try:
        return json.loads(claims_json)
    except json.JSONDecodeError:
        logger.warning("Failed to parse claims.json, using empty claims")
        return []

def _provider_name(base_url: str) -> str:
    """Return a safe provider label without exposing credentials."""
    normalized = base_url.lower()
    if "opencode.ai/zen/go" in normalized:
        return "OpenCode Go"
    if "openrouter.ai" in normalized:
        return "OpenRouter"
    if "api.deepseek.com" in normalized:
        return "DeepSeek API"
    return base_url or "not exposed by client"


def _client_runtime(client: Any | None) -> tuple[str, str, str, str, str, str, str]:
    """Read provider, transport, model, effort, and output parameter."""
    if client is None:
        return (
            "not initialized",
            "not initialized",
            "not initialized",
            "not initialized",
            "not initialized",
            "not initialized",
            "not initialized",
        )
    base_url = str(getattr(client, "base_url", ""))
    model = str(getattr(client, "model", "not exposed by client"))
    return (
        _provider_name(base_url),
        base_url or "not exposed by client",
        model,
        str(getattr(client, "api_style", "not exposed by client")),
        str(getattr(client, "api_path", "not exposed by client")),
        str(getattr(client, "reasoning_effort", "provider default")),
        str(getattr(client, "max_output_parameter", "not exposed by client")),
    )
def _technical_settings_lines(
    interviewer_client: Any | None,
    candidate_client: Any | None,
    max_stages: int,
    max_turns_per_stage: int,
) -> list[str]:
    """Render the exact LLM settings used by this rehearsal."""
    ivr = _client_runtime(interviewer_client)
    ive = _client_runtime(candidate_client)
    audit = ive
    reasoning_budget = (
        "none; shared completion budget"
        if MAX_TOKEN_REASONING_LIMIT is None
        else str(MAX_TOKEN_REASONING_LIMIT)
    )
    return [
        "## Technical Runtime Settings",
        "",
        "| Setting | Interviewer (IVR) | Candidate (IVE) | Violation audit |",
        "|---|---|---|---|",
        f"| Provider | {ivr[0]} | {ive[0]} | {audit[0]} |",
        f"| Base URL | `{ivr[1]}` | `{ive[1]}` | `{audit[1]}` |",
        f"| Model | `{ivr[2]}` | `{ive[2]}` | `{audit[2]}` |",
        f"| API | `{ivr[3]}` → `{ivr[4]}` | `{ive[3]}` → `{ive[4]}` | `{audit[3]}` → `{audit[4]}` |",
        "| Request messages | `messages[]` (system + conversation) | `messages[]` (system + conversation) | `messages[]` (system + audit question) |",
        "| Response format | text | text | JSON object (`response_format`) |",
        "| Thinking | explicit primary effort; private reasoning suppressed from transcript | explicit primary effort; private reasoning suppressed from transcript | explicit no-thinking effort |",
        f"| Reasoning effort | `{PRIMARY_REASONING_EFFORT}` | `{PRIMARY_REASONING_EFFORT}` | `{AUDIT_REASONING_EFFORT}` |",
        f"| Interleaved thinking | `{INTERLEAVED_THINKING or 'not sent'}` | `{INTERLEAVED_THINKING or 'not sent'}` | `{INTERLEAVED_THINKING or 'not sent'}` |",
        f"| Temperature | `{INTERVIEWER_TEMPERATURE}` | `{CANDIDATE_TEMPERATURE}` | `{VIOLATION_AUDIT_TEMPERATURE}` |",
        f"| Output parameter | `{ivr[6]}` = `{INTERVIEWER_MAX_TOKENS}` | `{ive[6]}` = `{CANDIDATE_MAX_TOKENS}` | `{audit[6]}` = `{VIOLATION_AUDIT_MAX_TOKENS}` |",
        f"| Shared reasoning budget | `{reasoning_budget}` | `{reasoning_budget}` | `{reasoning_budget}` |",
        f"| Visible response limit | `{INTERVIEWER_VISIBLE_WORD_LIMIT}` words | `{CANDIDATE_VISIBLE_WORD_LIMIT}` words | JSON audit |",
        f"| Prompt context limit | role `{ROLE_CONTEXT_MAX_CHARS}` chars + prep `{PREPARATION_CONTEXT_MAX_CHARS}` chars | evidence pack `{EVIDENCE_PACK_MAX_CHARS}` chars | per-assertion audit |",
        f"| Harness bound | up to `{max_stages}` stages | up to `{max_turns_per_stage}` interviewer follow-ups per stage | n/a |",
        f"| Transport retries | `{LLM_REQUEST_RETRIES}` with exponential backoff | `{LLM_REQUEST_RETRIES}` with exponential backoff | `{LLM_REQUEST_RETRIES}` with exponential backoff |",
        "",
        f"`MAX_TOKEN_LIMIT = {MAX_TOKEN_LIMIT}` is the shared conversational completion budget; `MAX_TOKEN_REASONING_LIMIT = {MAX_TOKEN_REASONING_LIMIT}` means there is no separate reasoning budget. Reasoning, if emitted, and visible output consume the provider's output budget together.",
        "",
    ]


def _advisor_settings_lines(
    interviewer_advisor: Any | None,
    candidate_advisor: Any | None,
) -> list[str]:
    """Render advisor settings without rendering private advisor content."""
    if interviewer_advisor is None and candidate_advisor is None:
        return ["## Private Advisor Settings", "", "Disabled.", ""]
    ivr = _client_runtime(interviewer_advisor)
    ive = _client_runtime(candidate_advisor)
    return [
        "## Private Advisor Settings",
        "",
        "Advisor comments are generated one visible turn late, injected only into the matching primary model, and never written as transcript speaker rows.",
        "",
        "| Side | Provider | Model | API | Effort | Temperature | Output budget |",
        "|---|---|---|---|---:|---:|---:|",
        f"| IVR advisor | {ivr[0]} | `{ivr[2]}` | `{ivr[3]}` → `{ivr[4]}` | `{ADVISOR_REASONING_EFFORT}` | `{ADVISOR_TEMPERATURE}` | `{ADVISOR_MAX_TOKENS}` |",
        f"| IVE advisor | {ive[0]} | `{ive[2]}` | `{ive[3]}` → `{ive[4]}` | `{ADVISOR_REASONING_EFFORT}` | `{ADVISOR_TEMPERATURE}` | `{ADVISOR_MAX_TOKENS}` |",
        f"Delay: `{ADVISOR_DELAY_TURNS}` turn. Advisor output is side-local and private.",
        "",
    ]


# ── Rehearsal runner ─────────────────────────────────────────────────────


class RehearsalRunner:
    """Runs a full mock interview between MockInterviewer and CandidateAgent.

    Args:
        case_dir: Path to the ATA case directory.
        style: Interviewer style (neutral/skeptical/adversarial/rushed/friendly).
        max_stages: Maximum number of interview stages to run.
        max_turns_per_stage: Maximum conversational turns per stage.
        principal: Principal name for claim loading.
        output_dir: Where to write rehearsal artefacts. If None, auto-generated.
    """

    STYLES = ("neutral", "skeptical", "adversarial", "rushed", "friendly")

    def __init__(
        self,
        case_dir: Path,
        *,
        style: str = "neutral",
        max_stages: int = DEFAULT_MAX_STAGES,
        max_turns_per_stage: int = DEFAULT_MAX_TURNS_PER_STAGE,
        principal: str = "lj",
        output_dir: Path | None = None,
        ivr_llm_client: Any | None = None,
        ive_llm_client: Any | None = None,
        ivr_advisor_llm_client: Any | None = None,
        ive_advisor_llm_client: Any | None = None,
    ) -> None:
        self._case_dir = case_dir
        self._style = style
        self._max_stages = max_stages
        self._max_turns = max_turns_per_stage
        self._principal = principal
        self._ivr_llm_client = ivr_llm_client
        self._ive_llm_client = ive_llm_client
        self._ivr_advisor_llm_client = ivr_advisor_llm_client
        self._ive_advisor_llm_client = ive_advisor_llm_client

        # Load case data
        refs = _load_case_references(case_dir)
        self._role_text = refs.get("role_text", "")
        self._prep_text = refs.get("prep_text", "")
        self._evidence_pack = refs.get("evidence_pack", "")
        claims_raw = refs.get("claims_json", "[]")
        self._claims = _parse_claims(claims_raw)
        self._company_name = refs.get("company_name", "the company")
        self._role_title = refs.get("role_title", "the role")

        # Output directory
        ts = _timestamp_dir()
        if output_dir:
            self._output_dir = output_dir
        else:
            self._output_dir = (
                case_dir / "agent-interview" / "rehearsals" / f"{ts}-{style}"
            )

        self._interviewer: MockInterviewer | None = None
        self._candidate: CandidateAgent | None = None
        self._ivr_advisor: SideAdvisor | None = None
        self._ive_advisor: SideAdvisor | None = None

        # Results
        self._full_transcript: list[dict[str, str]] = []
        self._stage_results: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        """Run the full rehearsal. Returns a summary dict."""
        logger.info(
            "Starting rehearsal: style=%s, max_stages=%d, case=%s",
            self._style, self._max_stages, self._case_dir.name,
        )

        # Create agents with their LLM clients
        self._ivr_advisor = (
            SideAdvisor(
                side="interviewer",
                llm_client=self._ivr_advisor_llm_client,
                role_title=self._role_title,
                company_name=self._company_name,
            )
            if self._ivr_advisor_llm_client
            else None
        )
        self._ive_advisor = (
            SideAdvisor(
                side="candidate",
                llm_client=self._ive_advisor_llm_client,
                role_title=self._role_title,
                company_name=self._company_name,
                permitted_claims=self._claims,
            )
            if self._ive_advisor_llm_client
            else None
        )
        self._interviewer = MockInterviewer(
            role_text=self._role_text,
            prep_text=self._prep_text,
            style=self._style,
            max_turns_per_stage=self._max_turns,
            role_title=self._role_title,
            company_name=self._company_name,
            llm_client=self._ivr_llm_client,
            advisor=self._ivr_advisor,
        )
        self._candidate = CandidateAgent(
            claims=self._claims,
            evidence_pack=self._evidence_pack,
            principal=self._principal,
            role_title=self._role_title,
            company_name=self._company_name,
            llm_client=self._ive_llm_client,
            advisor=self._ive_advisor,
        )

        # Determine which stages to run (manual stages only)
        stages = self._interviewer.stages
        manual_stages = [
            s for s in stages
            if s.get("manual_intervention") and s["stage_number"] > 0
        ]
        stages_to_run = manual_stages[: self._max_stages]

        # Run each stage
        for stage_info in stages_to_run:
            stage_num = stage_info["stage_number"]
            stage_result = self._run_stage(stage_num)
            self._stage_results.append(stage_result)

        # Aggregate results
        total_violations = sum(
            s["violations"] for s in self._stage_results
        )
        total_refusals = sum(
            s["refusals"] for s in self._stage_results
        )

        summary = {
            "style": self._style,
            "stages_run": len(self._stage_results),
            "total_violations": total_violations,
            "total_refusals": total_refusals,
            "total_escalations": len(self._candidate.escalations) if self._candidate else 0,
            "total_turns": sum(s["turns"] for s in self._stage_results),
            "output_dir": str(self._output_dir),
        }

        # Write artefacts
        self._write_artefacts(summary)

        logger.info(
            "Rehearsal complete: %d stages, %d violations, %d refusals",
            summary["stages_run"], summary["total_violations"], summary["total_refusals"],
        )

        return summary

    def _observe_advisors(self) -> None:
        """Give both private advisors the public transcript only."""
        if not self._full_transcript:
            return
        turn_number = len(self._full_transcript)
        for advisor in (self._ivr_advisor, self._ive_advisor):
            if advisor:
                advisor.observe(turn_number, self._full_transcript)

    def _run_stage(self, stage_number: int) -> dict[str, Any]:
        """Run a single interview stage. Returns stage result."""
        assert self._interviewer is not None
        assert self._candidate is not None

        logger.info("Running stage %d", stage_number)

        # Interviewer opens the stage
        opening = self._interviewer.open_stage(stage_number)
        self._full_transcript.append({
            "role": "interviewer",
            "stage": stage_number,
            "content": opening,
        })
        self._observe_advisors()

        # Conversation loop
        turns = 0
        conversation_history: list[dict[str, str]] = []
        stage_violations = 0
        stage_refusals = 0
        refusals_before = len(self._candidate.refusals)
        escalations_before = len(self._candidate.escalations)

        while True:
            # Candidate responds
            candidate_response = self._candidate.respond(
                opening,
                conversation_so_far=conversation_history,
            )

            self._full_transcript.append({
                "role": "candidate",
                "stage": stage_number,
                "content": candidate_response,
            })
            self._observe_advisors()

            conversation_history.append({"role": "user", "content": opening})
            conversation_history.append({"role": "assistant", "content": candidate_response})
            turns += 1

            # Check if stage is completed after candidate's response
            stage_data = self._interviewer.stages[stage_number]
            if stage_data.get("status") == "completed":
                break

            # Interviewer responds
            interviewer_response = self._interviewer.respond(
                stage_number, candidate_response
            )

            if interviewer_response:
                self._full_transcript.append({
                    "role": "interviewer",
                    "stage": stage_number,
                    "content": interviewer_response,
                })
                self._observe_advisors()
                opening = interviewer_response
            else:
                # Stage closed
                break

        # Check violations after stage
        violations = self._candidate.check_violations()
        stage_violations = len(violations)
        stage_refusals = len(self._candidate.refusals) - refusals_before

        return {
            "stage_number": stage_number,
            "stage_name": self._interviewer.stages[stage_number]["stage_name"],
            "turns": turns,
            "violations": stage_violations,
            "refusals": stage_refusals,
            "escalations": len(self._candidate.escalations) - escalations_before,
            "status": "completed",
            "manual": bool(stage_data.get("manual_intervention")),
        }

    def _write_artefacts(self, summary: dict[str, Any]) -> None:
        """Write transcript, events, and report to the output directory."""
        out = self._output_dir
        out.mkdir(parents=True, exist_ok=True)

        # Transcript JSONL
        transcript_path = out / "transcript.jsonl"
        with transcript_path.open("w", encoding="utf-8") as f:
            for entry in self._full_transcript:
                f.write(json.dumps(entry, sort_keys=True) + "\n")

        # Events JSONL (merge interviewer + candidate events)
        events_path = out / "events.jsonl"
        all_events = []
        if self._interviewer:
            all_events.extend(self._interviewer.events)
        if self._candidate:
            all_events.extend(self._candidate.events)
        for advisor in (self._ivr_advisor, self._ive_advisor):
            if advisor:
                all_events.extend(advisor.events)
        all_events.sort(key=lambda e: e.get("at", ""))
        with events_path.open("w", encoding="utf-8") as f:
            for event in all_events:
                f.write(json.dumps(event, sort_keys=True, default=str) + "\n")

        # Candidate report
        if self._candidate:
            report_path = out / "candidate-report.md"
            self._candidate.write_report(report_path)

        # Rehearsal summary report
        summary_path = out / "rehearsal-report.md"
        interviewer_client = getattr(self._interviewer, "_llm_client", None)
        candidate_client = getattr(self._candidate, "_llm_client", None)
        _, _, ivr_model, _, _, _, _ = _client_runtime(interviewer_client)
        _, _, ive_model, _, _, _, _ = _client_runtime(candidate_client)
        lines = [
            "# Rehearsal Report",
            "",
            f"**Date:** {_iso_now()}",
            f"**Case:** {self._case_dir.name}",
            f"**Style:** {self._style}",
            f"**IVR model:** {ivr_model}",
            f"**IVE model:** {ive_model}",
            f"**Stages run:** {summary['stages_run']}",
            f"**Total turns:** {summary['total_turns']}",
            f"**Total violations:** {summary['total_violations']}",
            f"**Total refusals:** {summary['total_refusals']}",
            f"**Total escalations:** {summary.get('total_escalations', 0)}",
            f"**Principal:** {self._principal}",
            "",
        ]
        lines.extend(_technical_settings_lines(
            interviewer_client,
            candidate_client,
            self._max_stages,
            self._max_turns,
        ))
        lines.extend(_advisor_settings_lines(
            self._ivr_advisor_llm_client,
            self._ive_advisor_llm_client,
        ))
        lines.extend([
            "## Stage Results",
            "",
            "| Stage | Turns | Violations | Refusals | Escalations | Manual | Status |",
            "|-------|------:|----------:|--------:|------------:|:------:|--------|",
        ])
        for s in self._stage_results:
            lines.append(
                f"| {s['stage_name']} | {s['turns']} | {s['violations']} "
                f"| {s['refusals']} | {s.get('escalations', 0)} "
                f"| {'yes' if s.get('manual') else 'no'} | {s['status']} |"
            )
        lines.extend([
            "",
            "## Verdict",
            "",
        ])
        if summary["total_violations"] == 0:
            lines.append("**✅ CLEAN** — no unsupported claims asserted.")
        else:
            lines.append(
                f"**⚠️ {summary['total_violations']} VIOLATION(S)** — "
                f"check candidate-report.md for details."
            )
        summary_path.write_text("\n".join(lines), encoding="utf-8")

        # Transcript plaintext (human-readable)
        txt_path = out / "transcript.txt"
        readable = []
        for entry in self._full_transcript:
            direction = "→ INTERVIEWER" if entry["role"] == "interviewer" else "← CANDIDATE"
            readable.append(
                f"[{entry.get('stage', '?')}] {direction}\n"
                f"  {entry['content']}\n"
            )
        txt_path.write_text("\n".join(readable), encoding="utf-8")

        logger.info("Artefacts written to %s", out)


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a self-hosted ATA rehearsal (Phase E.2)"
    )
    parser.add_argument(
        "--case", type=Path, required=True,
        help="Path to the ATA case directory",
    )
    parser.add_argument(
        "--style", type=str, default="neutral",
        choices=RehearsalRunner.STYLES,
        help="Interviewer style (default: neutral)",
    )
    parser.add_argument(
        "--max-stages", type=int, default=DEFAULT_MAX_STAGES,
        help=f"Maximum interview stages to run (default: {DEFAULT_MAX_STAGES})",
    )
    parser.add_argument(
        "--max-turns", type=int, default=DEFAULT_MAX_TURNS_PER_STAGE,
        help=f"Maximum conversational turns per stage (default: {DEFAULT_MAX_TURNS_PER_STAGE})",
    )
    parser.add_argument(
        "--principal", type=str, default="lj",
        help="Principal name (default: lj)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory for rehearsal artefacts",
    )
    # Model configuration
    parser.add_argument(
        "--ivr-model", type=str, default=None,
        help="Model for the interviewer (IVR). Falls back to LLM_MODEL env/config.",
    )
    parser.add_argument(
        "--ivr-base-url", type=str, default=None,
        help="Base URL for IVR model API.",
    )
    parser.add_argument(
        "--ivr-api-key", type=str, default=None,
        help="API key for IVR model.",
    )
    parser.add_argument(
        "--ive-model", type=str, default=None,
        help="Model for the interviewee (IVE). Falls back to LLM_MODEL env/config.",
    )
    parser.add_argument(
        "--ive-base-url", type=str, default=None,
        help="Base URL for IVE model API.",
    )
    parser.add_argument(
        "--ive-api-key", type=str, default=None,
        help="API key for IVE model.",
    )
    parser.add_argument(
        "--advisor-model", type=str, default=None,
        help="Optional private advisor model used independently on both sides.",
    )
    parser.add_argument(
        "--advisor-base-url", type=str, default=None,
        help="Base URL for the optional advisor model.",
    )
    parser.add_argument(
        "--advisor-api-key", type=str, default=None,
        help="API key for the optional advisor model.",
    )
    parser.add_argument(
        "--scenario", type=str, default=None,
        choices=list(SCENARIO_PRESETS.keys()),
        help="Pre-configured model scenario (overrides --ivr-model/--ive-model).",
    )
    parser.add_argument(
        "--matrix", action="store_true",
        help="Run all styles (overrides --style)",
    )
    parser.add_argument(
        "--runs-per-style", type=int, default=1,
        help="Number of runs per style in matrix mode (default: 1)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Resolve model configuration
    ivr_model = args.ivr_model
    advisor_enabled = bool(args.advisor_model or args.advisor_base_url or args.advisor_api_key)
    ivr_advisor_client = (
        _make_llm_client(
            model=args.advisor_model,
            base_url=args.advisor_base_url,
            api_key=args.advisor_api_key,
        )
        if advisor_enabled
        else None
    )
    ive_advisor_client = (
        _make_llm_client(
            model=args.advisor_model,
            base_url=args.advisor_base_url,
            api_key=args.advisor_api_key,
        )
        if advisor_enabled
        else None
    )
    ive_model = args.ive_model
    if args.scenario:
        ivr_model, ive_model = SCENARIO_PRESETS[args.scenario]
        logger.info("Using scenario '%s'", args.scenario)

    # Create LLM clients (auto-resolves provider from model name)
    ivr_client = _make_llm_client(
        model=ivr_model, base_url=args.ivr_base_url, api_key=args.ivr_api_key,
    ) if (ivr_model or args.ivr_base_url or args.ivr_api_key) else None
    ive_client = _make_llm_client(
        model=ive_model, base_url=args.ive_base_url, api_key=args.ive_api_key,
    ) if (ive_model or args.ive_base_url or args.ive_api_key) else None

    # Log resolved providers
    ivr_info = f"{ivr_client.model} @ {ivr_client.base_url}" if ivr_client else "config default"
    ive_info = f"{ive_client.model} @ {ive_client.base_url}" if ive_client else "config default"
    logger.info("IVR: %s", ivr_info)
    logger.info("IVE: %s", ive_info)
    advisor_info = (
        f"{ivr_advisor_client.model} @ {ivr_advisor_client.base_url}"
        if ivr_advisor_client else "disabled"
    )
    logger.info("Advisor (both sides): %s", advisor_info)

    def _run_single(style: str, output_dir: Path | None = None) -> dict[str, Any]:
        runner = RehearsalRunner(
            case_dir=args.case,
            style=style,
            max_stages=args.max_stages,
            max_turns_per_stage=args.max_turns,
            principal=args.principal,
            output_dir=output_dir,
            ivr_llm_client=ivr_client,
            ive_llm_client=ive_client,
            ivr_advisor_llm_client=ivr_advisor_client,
            ive_advisor_llm_client=ive_advisor_client,
        )
        return runner.run()

    if args.matrix:
        # Multi-style matrix
        for style in RehearsalRunner.STYLES:
            for run_num in range(args.runs_per_style):
                logger.info(
                    "Matrix run %d/%d for style '%s'",
                    run_num + 1, args.runs_per_style, style,
                )
                summary = _run_single(style)
                print(
                    f"[{style}] Stages: {summary['stages_run']}, "
                    f"Violations: {summary['total_violations']}, "
                    f"Refusals: {summary['total_refusals']}"
                )
    else:
        # Single run
        summary = _run_single(args.style)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

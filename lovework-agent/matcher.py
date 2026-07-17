"""
Job matcher: score extracted jobs against a candidate profile using LLM.

Two implementations:
- JobMatcher (legacy): uses LLMClient.structured() with hand-written prompts
- JobMatcherDSPyAdapter: uses dspy_signatures.JobMatcherDSPy

Both produce MatchResult. The DSPy version is the future — typed signatures,
compileable, optimisable. The legacy version is kept for comparison and as
a fallback if DSPy fails to import.

The matcher also considers:
- Job registry status (new, still_open, long_lasting, disappeared)
- Prior contact history (applications + Gmail correspondence)
- Re-apply rules (don't re-apply within 6 months to same role)
"""

import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from llm_client import LLMClient

logger = logging.getLogger(__name__)

# Auto-DROP threshold: if LJ applied and got rejected within the last N months
# for the SAME role, don't recommend re-applying.
REAPPLY_COOLDOWN_MONTHS = 6

# Org-level re-apply kill: if LJ applied and got rejected within the last
# N months at the SAME ORG (any role, not just the same one), don't
# recommend re-applying. Catches the "different team, same company" case
# (e.g. Poolside Evaluations → Poolside Pre-training — different roles
# but the company already said no to LJ, so the org's door is closed for
# a while). Configurable via env (LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS).
# Default 18 months = 1.5 years (LJ's preference).
REAPPLY_ORG_COOLDOWN_MONTHS = int(
    os.getenv("LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS", "18")
)

# Work-authorization hard-kill. A job requiring work rights the candidate does NOT have
# (e.g. "US citizen/person only", "must be authorized to work in the US", "no visa
# sponsorship") is an instant DROP — no point paying for an LLM match. These negative
# forms are matched against the job's location/visa text; a bare "visa sponsorship
# available" is NOT a kill. See profiles/<name>/work_auth.md for the candidate's rights.
WORK_AUTH_KILL_PATTERNS = [
    r"\bus\s+(citizen|person|national)s?\s+only\b",
    r"\bcitizens?hip\b.*\brequired\b",
    r"\bauthorized\s+to\s+work\s+in\s+the\s+us\b",
    r"\bmust\s+be\s+(us|u\.s\.)\s+(based|citizen|person|national)",
    r"\bvisa\s+sponsorship\s+not\s+available\b",
    r"\bno\s+visa\s+sponsorship\b",
    r"\bcannot\s+(sponsor|provide\s+sponsorship)\b",
    r"\bunfortunately[^\n]{0,40}no\s+sponsorship",
    r"\bw-?2\s+only\b",
]


# ── Action vocabulary ──────────────────────────────────────────────────

ACTIONS_ORDERED = [
    "APPLY_NOW",          # strong fit, reachable, will enjoy — pursue immediately
    "WARM_INTRO_ONLY",    # high fit, low reach — needs referral or unusual angle
    "WATCH",              # promising but needs more data
    "USE_AS_GAP_SIGNAL",  # shows what proof assets to build (aspirational)
    "MONITOR",            # keep on radar, no action yet
    "DREAM",              # prestigious, very low reach — aspirational signal
    "DROP",               # clearly wrong
]


class MatchResult(BaseModel):
    """Result of matching a job against a profile.

    Multi-axis scoring: fit (skill alignment), reach (screening odds),
    flourish (day-to-day enjoyment). Combined via geometric mean with
    deterministic caps on low reach/flourish.
    """

    fit_score: float = Field(default=0.0, ge=0, le=10, description="Skill/intellect alignment 0-10")
    reach_score: float = Field(default=0.0, ge=0, le=10, description="Realistic screening odds 0-10")
    flourish_score: float = Field(default=0.0, ge=0, le=10, description="Day-to-day enjoyment 0-10")
    combined_score: float = Field(ge=0, le=10, default=0.0, description="Geometric mean with caps")
    recommended_action: str = Field(default="DROP", description="One of: " + ", ".join(ACTIONS_ORDERED))
    prestige_trap_risk: str = Field(default="low", description="One of: low, medium, high")
    screening_story: str = Field(default="", description="How LJ might get through screening")
    likely_day_to_day: str = Field(default="", description="What the actual work looks like")
    reasoning: str = Field(default="", description="Brief explanation tying axes to recommendation")
    primary_content_hash: str = Field(default="", description="Hash of fetched primary advert evidence")
    primary_fetched_at: str = Field(default="", description="UTC time primary advert was fetched")
    primary_fetch_method: str = Field(default="", description="Primary evidence fetch method")
    alignment_matrix: List[str] = Field(
        default_factory=list,
        description="Evidence-grounded job requirement to candidate fact alignments",
    )
    gaps: List[str] = Field(default_factory=list, description="Material requirements lacking evidence")
    application_angle: str = Field(default="", description="Specific truthful application narrative")
    assessment_status: str = Field(
        default="SCORED",
        description="SCORED for valid assessments; UNSCORED when the provider failed",
    )
    # Legacy fields kept for backwards compatibility with downstream consumers
    score: float = Field(ge=0, le=10, default=0.0, description="Legacy alias for combined_score")
    decision: str = Field(default="DROP", description="Legacy, derived from recommended_action")


# ── Geometric mean with caps ────────────────────────────────────────────

def _compute_combined(fit: float, reach: float, flourish: float) -> float:
    """Geometric mean with deterministic caps.

    Scores 0-10 are shifted by +1 into log-safe domain (>0).
    Weights: fit=0.40, reach=0.35, flourish=0.25.
    A low score on any axis crushes the combined score (property of
    geometric mean), which is the desired behaviour: a role that's
    unreachable OR unenjoyable should not rank highly.

    Additional hard caps:
    - reach < 3 or flourish < 3 → combined capped at 6
    - reach < 2 and flourish < 3 → combined capped at 4

    Test cases:
    Isomorphic RS: fit=8, reach=2, flourish=3 → ~4.0, action=USE_AS_GAP_SIGNAL
    Poetiq AI Scientist: fit=8, reach=7, flourish=8 → ~7.5, action=APPLY_NOW
    """
    # Shift into log-safe domain
    f = max(fit, 0.1) + 1.0
    r = max(reach, 0.1) + 1.0
    l = max(flourish, 0.1) + 1.0

    raw = math.exp(0.40 * math.log(f) + 0.35 * math.log(r) + 0.25 * math.log(l))
    raw -= 1.0  # shift back to 0-10

    # Deterministic caps
    if reach < 3 or flourish < 3:
        raw = min(raw, 6.0)
    if reach < 2 and flourish < 3:
        raw = min(raw, 4.0)

    return round(raw, 1)


def _action_from_scores(fit: float, reach: float, flourish: float) -> str:
    """Derive recommended action from axis scores.

    Decisions are gated: low reach or low flourish crush the action
    regardless of fit. Checks run from most constrained to least.
    - reach < 3: unreachable (gap signal or monitor)
    - flourish < 4: unenjoyable (monitor or drop)
    - then check positive signals
    """
    if fit >= 6 and reach < 3:
        return "USE_AS_GAP_SIGNAL"
    if fit >= 7 and flourish < 4:
        return "MONITOR"
    if fit >= 7 and reach >= 6 and flourish >= 7:
        return "APPLY_NOW"
    if fit >= 7 and reach < 6 and flourish >= 5:
        return "WARM_INTRO_ONLY"
    if fit >= 6:
        if flourish < 4:
            return "MONITOR"
        return "WATCH"
    if fit >= 4 and reach >= 4 and flourish >= 4:
        return "MONITOR"
    return "DROP"


def _decision_from_action(action: str) -> str:
    """Map rich product actions back to legacy report buckets.

    Reports, org-page parsing, the index, and cross-check tooling still use
    GO/MAYBE/FLAG/DROP. Keep that coarse contract stable while the new
    action vocabulary carries the more useful next step.
    """
    return {
        "APPLY_NOW": "GO",
        "WARM_INTRO_ONLY": "MAYBE",
        "WATCH": "MAYBE",
        "USE_AS_GAP_SIGNAL": "FLAG",
        "MONITOR": "FLAG",
        "DREAM": "FLAG",
        "DROP": "DROP",
    }.get(action, "FLAG")


def _build_context(org_name: str, job_title: str, job_url: str, registry, use_history: bool) -> tuple[str, Optional[str]]:
    """Build the additional context for the LLM and check the re-apply rule.

    Returns (context_text, reapply_kill_reason).
    """
    context_parts = []

    if registry is not None:
        try:
            record = registry.get(org_name, job_title, job_url)
            if record is not None:
                context_parts.append(
                    f"**Job registry status**: {record.status} "
                    f"(first seen {record.first_seen}, last seen {record.last_seen}, "
                    f"age {record.age_days} days)"
                )
        except Exception as e:
            logger.debug(f"Registry lookup failed: {e}")

    if use_history:
        try:
            from history import scan_history
            prior = scan_history(org_name, use_gmail=True)
            if prior.has_application or prior.gmail_events:
                context_parts.append(f"**Prior contact**: {prior.summary()}")
        except Exception as e:
            logger.debug(f"History scan failed: {e}")

    context = "\n".join(context_parts) if context_parts else "No prior context."
    reapply_kill = _check_reapply_kill(org_name, job_title)
    return context, reapply_kill


def _check_reapply_kill(
    org_name: str, job_title: str, applications_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return a reason string if we should auto-DROP this re-apply, else None.

    Two layers of protection:
      1. **Same-role cooldown** (REAPPLY_COOLDOWN_MONTHS, default 6): if
         the candidate applied for a similar role (title jaccard >= 0.6)
         and was rejected, DROP. Catches "applied to ACME-AI-Scientist
         and got rejected — don't apply to ACME-AI-Scientist again".
      2. **Org-level cooldown** (REAPPLY_ORG_COOLDOWN_MONTHS, default 18):
         if the candidate was rejected at the same org within the last N
         months (any role), DROP. Catches "Poolside rejected Evaluations
         2 months ago — don't apply to Poolside Pre-training today,
         even though the role title is different".

    Both are configurable via env. The org-level cooldown is the
    user-controlled knob: 12 = 1 year, 18 = 1.5y, 24 = 2 years.

    Args:
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
    """
    try:
        from history import scan_history
        # use_gmail=True so the kill catches rejections that were only seen
        # in the Gmail inbox (not yet recorded in the applications/ .txt
        # file). Gmail is the canonical rejection signal — applications/
        # txt files are updated after the fact. The Gmail accessor
        # gracefully no-ops if google_api.py is not set up.
        prior = scan_history(org_name, use_gmail=True, applications_dir=applications_dir)
    except Exception:
        return None

    if not prior.applications:
        return None

    # Layer 1: same-role short cooldown.
    role_cutoff = datetime.now() - timedelta(days=REAPPLY_COOLDOWN_MONTHS * 30)
    for app in prior.applications:
        try:
            app_date = datetime.fromisoformat(app.date)
        except (TypeError, ValueError):
            continue
        if app_date >= role_cutoff and app.has_rejection and _titles_similar(app.role, job_title):
            return (
                f"Applied {app.date} for similar role '{app.role}', "
                f"rejection found. Wait at least {REAPPLY_COOLDOWN_MONTHS} months "
                f"before re-applying to a similar role."
            )

    # Layer 2: org-level long cooldown (any role).
    if REAPPLY_ORG_COOLDOWN_MONTHS > 0:
        org_cutoff = datetime.now() - timedelta(days=REAPPLY_ORG_COOLDOWN_MONTHS * 30)
        for app in prior.applications:
            try:
                app_date = datetime.fromisoformat(app.date)
            except (TypeError, ValueError):
                continue
            if app_date >= org_cutoff and app.has_rejection:
                return (
                    f"Org-level cooldown: {org_name} rejected LJ on {app.date} "
                    f"(role '{app.role}'). No re-apply to this org for at least "
                    f"{REAPPLY_ORG_COOLDOWN_MONTHS} months."
                )

    return None


def _apply_reapply_kill(result: MatchResult, reapply_kill: Optional[str]) -> MatchResult:
    """If reapply_kill is set, override the result."""
    if reapply_kill:
        for f in ("fit_score", "reach_score", "flourish_score"):
            setattr(result, f, 0.0)
        result.combined_score = 0.0
        result.recommended_action = "DROP"
        result.score = 0.0
        result.decision = "DROP"
        result.reasoning = f"AUTO-DROP: {reapply_kill}. " + result.reasoning
    return result


def _check_work_auth_kill(location: str, job_description: str = "") -> Optional[str]:
    """Return a reason string if the job's location/visa text is a work-auth deal-breaker.

    Checked against WORK_AUTH_KILL_PATTERNS. Looks at the location field and the role
    description (HN/jobs buries "US citizen only" in the body). Returns None when OK.
    """
    import re

    text = f"{location or ''} {job_description or ''}".lower()
    if not text.strip():
        return None
    for pat in WORK_AUTH_KILL_PATTERNS:
        m = re.search(pat, text)
        if m:
            return f"work-authorization deal-breaker (matched '{m.group(0).strip()}')"
    return None


def _apply_work_auth_kill(result: MatchResult, work_auth_kill: Optional[str]) -> MatchResult:
    """If work_auth_kill is set, override the result (hard DROP)."""
    if work_auth_kill:
        for f in ("fit_score", "reach_score", "flourish_score"):
            setattr(result, f, 0.0)
        result.combined_score = 0.0
        result.recommended_action = "DROP"
        result.score = 0.0
        result.decision = "DROP"
        result.reasoning = f"AUTO-DROP: {work_auth_kill}. " + result.reasoning
    return result


def _titles_similar(a: str, b: str) -> bool:
    """Loose title matching for re-apply detection."""
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()
    if a_lower in b_lower or b_lower in a_lower:
        return True
    a_words = set(a_lower.split())
    b_words = set(b_lower.split())
    stopwords = {"the", "a", "an", "of", "for", "in", "at", "and", "or", "-", "/", "|",
                 "senior", "sr", "jr", "junior", "lead", "principal", "staff", "head"}
    a_words -= stopwords
    b_words -= stopwords
    if not a_words or not b_words:
        return False
    overlap = a_words & b_words
    union = a_words | b_words
    jaccard = len(overlap) / len(union)
    return jaccard >= 0.6


# ── Legacy matcher (hand-written prompts) ──────────────────────────────

class JobMatcher:
    """Legacy matcher using LLMClient.structured() with hand-written prompts.

    Kept for comparison and as a fallback. The DSPy version is preferred.
    """

    def __init__(
        self,
        llm: LLMClient,
        profile: str,
        registry: Optional["JobRegistry"] = None,
        use_history: bool = True,
    ):
        self.llm = llm
        self.profile = profile
        self.registry = registry
        self.use_history = use_history

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ) -> MatchResult:
        # Work-auth hard-kill fires BEFORE the LLM call (no point paying to match a role
        # the candidate isn't eligible to take, e.g. "US citizen only").
        work_auth_kill = _check_work_auth_kill(location, job_description)
        if work_auth_kill:
            return _apply_work_auth_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), work_auth_kill
            )

        context, reapply_kill = _build_context(org_name, job_title, job_url, self.registry, self.use_history)

        prompt = f"""You are a personal talent agent. Your client has this profile:

{self.profile}

A job posting has been found:
- Organization: {org_name}
- Title: {job_title}
- Location: {location or "unspecified"}
- Description / Snippet: {job_description}

Additional context:
{context}

Score this role on three independent axes (each 0-10):

1. fit_score — How well does the work match the client's skills and interests?
   Consider technical alignment, domain overlap, seniority match.

2. reach_score — How realistic is it that the client would pass screening?
   Consider the MARKET POSITION section of the profile (feasibility reality).
   A top-lab Research Scientist role with PhD/publication requirements → reach 1-3.
   An applied ML engineering role where production experience matters → reach 6-9.
   A small/early startup should NOT inherit the low reach of a star-researcher
   lab. If the candidate has rare direct domain experience plus evidence of
   shipped products or production systems, use reach 7-9 even if that domain
   is not their most recent job.

3. flourish_score — Would the client actually enjoy the day-to-day work?
   Consider the flourishing anti-patterns and signals in MARKET POSITION.
   Narrow benchmark chasing → flourish 1-3.
   Building working systems with high agency → flourish 7-9.

Also provide:
- prestige_trap_risk: "low", "medium", or "high" — is this a famous name
  that masks poor fit?
- screening_story: one sentence on how the client might (or might not)
  get through screening
- likely_day_to_day: what the actual work looks like beyond the JD
  (one sentence)
- reasoning: brief explanation tying the three axes together
- alignment_matrix: 3-6 concise strings in the form "job need -> candidate evidence"
- gaps: material job requirements for which the supplied profile/retrieved evidence
  contains no proof; do not invent experience
- application_angle: a concrete, truthful 1-2 sentence application angle using
  the strongest evidence (not generic enthusiasm)

Special signals:
- If the job has been open >30 days (long_lasting), reduce flourish_score
  by 1-2 — the org may be slow or disorganised.
- If the role aligns with an explicit branching possibility
  (see CANDIDATE POSSIBILITIES), add +1 to fit_score and name the branch.
- Rare direct-domain matches deserve decisive calibration. Roughly 8+ years in
  the advertised domain plus a concrete shipped/demoed artifact and a reachable
  startup role normally implies fit 9-10, reach 7-9, flourish 8-10. Do not
  reduce this to a generic "career transition" when the evidence is explicit.
- In the alignment matrix and application angle, prefer the most specific
  concrete artifact (what was built/demoed/shipped) over generic credentials.
  When retrieved evidence is marked [CONCRETE ARTIFACT] and is relevant, the
  application angle MUST lead with that artifact rather than a degree or a
  generic claim.

Respond with JSON containing: fit_score, reach_score, flourish_score,
prestige_trap_risk, screening_story, likely_day_to_day, reasoning.
Also include alignment_matrix, gaps, application_angle.
Be concise but specific."""

        messages = [{"role": "user", "content": prompt}]
        try:
            raw = self.llm.structured(
                messages, MatchResult,
                context=f"[{org_name}] match: {job_title}",
            )
        except Exception as e:
            logger.warning(f"Match failed for {job_title} @ {org_name}: {e}")
            return MatchResult(
                fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                combined_score=0.0, recommended_action="WATCH",
                score=0.0, decision="FLAG",
                reasoning=f"UNSCORED: LLM matching failed: {e}",
                assessment_status="UNSCORED",
            )

        # Post-process: compute combined score and derive action
        raw.combined_score = _compute_combined(raw.fit_score, raw.reach_score, raw.flourish_score)
        raw.recommended_action = _action_from_scores(raw.fit_score, raw.reach_score, raw.flourish_score)
        # Set legacy fields for downstream consumers
        raw.score = raw.combined_score
        raw.decision = _decision_from_action(raw.recommended_action)

        return _apply_reapply_kill(raw, reapply_kill)


# ── DSPy matcher (typed signatures, optimisable) ───────────────────────

class JobMatcherDSPyAdapter:
    """DSPy-based matcher using typed signatures.

    Same interface as JobMatcher (drop-in replacement), but uses DSPy
    signatures for typed prompts. Can be optimised with DSPy optimizers
    (BootstrapFewShot, MIPRO, GEPA) against a metric.
    """

    def __init__(
        self,
        profile: str,
        registry: Optional["JobRegistry"] = None,
        use_history: bool = True,
    ):
        self.profile = profile
        self.registry = registry
        self.use_history = use_history

        # Lazy import + configure
        try:
            from dspy_signatures import JobMatcherDSPy, configure_dspy
            configure_dspy()
            self._dspy_matcher = JobMatcherDSPy()
            self._dspy_available = True
        except Exception as e:
            logger.warning(f"DSPy not available, falling back to legacy: {e}")
            self._dspy_available = False

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ) -> MatchResult:
        # Work-auth hard-kill fires BEFORE any LLM call.
        work_auth_kill = _check_work_auth_kill(location, job_description)
        if work_auth_kill:
            return _apply_work_auth_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                work_auth_kill,
            )

        context, reapply_kill = _build_context(org_name, job_title, job_url, self.registry, self.use_history)

        if not self._dspy_available:
            return MatchResult(
                fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                combined_score=0.0, recommended_action="WATCH",
                score=0.0, decision="FLAG",
                reasoning="DSPy unavailable, no LLM call made",
                assessment_status="UNSCORED",
            )

        try:
            prediction = self._dspy_matcher.match(
                profile=self.profile,
                job_title=job_title,
                job_description=job_description,
                org_name=org_name,
                job_url=job_url,
                additional_context=context,
            )
            raw = MatchResult(
                fit_score=float(getattr(prediction, "fit_score", 0)),
                reach_score=float(getattr(prediction, "reach_score", 0)),
                flourish_score=float(getattr(prediction, "flourish_score", 0)),
                prestige_trap_risk=str(getattr(prediction, "prestige_trap_risk", "low")),
                screening_story=str(getattr(prediction, "screening_story", "")),
                likely_day_to_day=str(getattr(prediction, "likely_day_to_day", "")),
                reasoning=str(prediction.reasoning),
                alignment_matrix=list(getattr(prediction, "alignment_matrix", []) or []),
                gaps=list(getattr(prediction, "gaps", []) or []),
                application_angle=str(getattr(prediction, "application_angle", "")),
            )
            raw.combined_score = _compute_combined(raw.fit_score, raw.reach_score, raw.flourish_score)
            raw.recommended_action = _action_from_scores(raw.fit_score, raw.reach_score, raw.flourish_score)
            raw.score = raw.combined_score
            raw.decision = _decision_from_action(raw.recommended_action)
        except Exception as e:
            logger.warning(f"DSPy match failed for {job_title} @ {org_name}: {e}")
            return MatchResult(
                fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                combined_score=0.0, recommended_action="WATCH",
                score=0.0, decision="FLAG",
                reasoning=f"DSPy match failed: {e}",
                assessment_status="UNSCORED",
            )

        return _apply_reapply_kill(raw, reapply_kill)

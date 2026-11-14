"""
Job matcher: score extracted jobs against a principal profile using LLM.

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

import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

import config
import prompts  # the one place the prompts live (2026-10-15)
from calibration import apply_tier_bands, calibrate_result
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# Auto-DROP threshold: if the principal applied and got rejected within the last N
# months for the **same role**, don't recommend re-applying.
#
# **12 months, and deliberately longer than the org-level window below** (LJ,
# 2026-10-18: *"Think 18mo is too long. Let us do it 9mo for different role, and 12mo
# for the same role in the same organisation."*). The inversion is the point: a
# rejection of *this* role is direct evidence about this role — they saw the
# application and said no — while a rejection elsewhere in the org says less about a
# different team's opening. So the narrower case gets the longer window.
#
# Configurable via env (LOVEWORK_REAPPLY_COOLDOWN_MONTHS).
REAPPLY_COOLDOWN_MONTHS = int(
    os.getenv("LOVEWORK_REAPPLY_COOLDOWN_MONTHS", "12")
)

# Org-level re-apply kill: if LJ applied and got rejected within the last
# N months at the SAME ORG (any role, not just the same one), don't
# recommend re-applying. Catches the "different team, same company" case
# (e.g. Poolside Evaluations → Poolside Pre-training — different roles
# but the company already said no, so the org's door is closed for a while).
#
# **9 months** (LJ, 2026-10-18). Shorter than the same-role window above because a
# different team's role is a different question — the org said no once, not twice.
# Configurable via env (LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS); `0` disables it.
REAPPLY_ORG_COOLDOWN_MONTHS = int(
    os.getenv("LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS", "9")
)

# Work-authorization hard-kill. A job requiring work rights the principal does NOT have
# (e.g. "US citizen/person only", "must be authorized to work in the US", "no visa
# sponsorship") is an instant DROP — no point paying for an LLM match. These negative
# forms are matched against the job's location/visa text; a bare "visa sponsorship
# available" is NOT a kill. See profiles/<name>/work_auth.md for the principal's rights.
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
    # P3 — market-reach gates on landing pages: a site that serves only US
    # visitors is a deterministic kill even when the listing text is vague.
    r"\bthis\s+site\s+is\s+for\s+us\s+visitors\s+only\b",
    r"\bus\s+visitors\s+only\b",
    r"\bopen\s+to\s+us\s+(residents|candidates|applicants)\s+only\b",
    r"\b(?:u\.?s\.?)\s*only\b",
]

# A profile can opt into a narrow, deterministic preference exclusion with a
# marker in its soul.md. This is deliberately different from work
# authorisation: the principal *could* do the work, but has explicitly said it
# is the wrong profession. The narrow frontier-research condition prevents a
# city name, prestigious employer, or adjacent coursework from overriding that
# durable preference.
AI_ML_NLP_RESEARCH_EXCLUSION_MARKER = "lovework:exclude-ai-ml-nlp-research"

# A profile can also declare *employers* it will not work for, for ideological
# or cultural reasons, with a marker in its soul.md:
#
#     <!-- lovework:ideology-exclusion orgs: Anthropic | Anthropic Fellows -->
#
# This is P-0002 ("Anthropic / EA-aligned orgs downweighted on ideology"): the
# principal can do the work and may be well qualified, but would not want to.
# Until this existed the rule was prose only — living in the prompt and in the
# model's discretion — so culturally counter-aligned roles still surfaced with
# flourish 7.0 (LJ full sweep 2026-09-12). Declared orgs are matched as a word
# subset, so "Anthropic" also catches "Anthropic_Fellows" and "Anthropic PBC":
# org aliases are exactly how the org-level re-apply cooldown used to escape.
IDEOLOGY_EXCLUSION_MARKER = "lovework:ideology-exclusion"
_IDEOLOGY_EXCLUSION_RE = re.compile(
    r"<!--\s*lovework:ideology-exclusion\b(?P<orgs>[^>]*?)-->", re.I
)


def declared_ideology_exclusions(profile: str) -> List[str]:
    """The employer names a profile declares it will not work for."""
    text = profile or ""
    if IDEOLOGY_EXCLUSION_MARKER not in text.lower():
        return []
    match = _IDEOLOGY_EXCLUSION_RE.search(text)
    if not match:
        return []
    raw = match.group("orgs") or ""
    raw = re.sub(r"^\s*orgs\s*:", "", raw, flags=re.I)
    return [part.strip() for part in re.split(r"[|,;]", raw) if part.strip()]


def _org_words(name: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (name or "").lower())


def _check_ideology_exclusion(profile: str, org_name: str) -> Optional[str]:
    """Return a reason when the employer is one the principal refuses culturally."""
    declared = declared_ideology_exclusions(profile)
    if not declared or not org_name:
        return None
    detected = _org_words(org_name)
    if not detected:
        return None
    for name in declared:
        words = _org_words(name)
        if words and all(word in detected for word in words):
            return (
                f"ideological misalignment: {org_name} is on the principal's "
                f"declared exclusion list ('{name}')"
            )
    return None
AI_ML_NLP_TITLE_PATTERNS = [
    r"\b(machine learning|ml|artificial intelligence|ai|nlp|natural language|llm|large language)\b",
]
FRONTIER_AI_RESEARCH_PATTERNS = [
    r"\b(superintelligence|superlearner|reinforcement learning|frontier ai|frontier model|foundation model|large language model|ai safety)\b",
]
FRONTIER_RESEARCH_TITLE_PATTERNS = [
    r"\b(member of technical staff|research scientist|research engineer|applied scientist|researcher)\b",
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
        description="Evidence-grounded job requirement to principal fact alignments",
    )
    gaps: List[str] = Field(default_factory=list, description="Material requirements lacking evidence")
    application_angle: str = Field(default="", description="Specific truthful application narrative")
    assessment_status: str = Field(
        default="SCORED",
        description="SCORED for valid assessments; UNSCORED when the provider failed",
    )
    #: **Machine record only — never prose** (LJ, 2026-10-20: *"Even if the model used
    #: is no longer mimo-v2.5 — so any adjustment we are doing for 'mimo-v2.5' is
    #: wrong, and the above is misleading, cryptic and needless. Even if we were doing
    #: the same adjustment, and correctly — it should not be part of the user message
    #: and reporting!"*). The offset that was applied, so re-reading a cached verdict
    #: cannot shift it twice. It used to be done by prefixing `reasoning` with
    #: "CALIBRATED (+1.30 onto the mimo-v2.5 scale [deepseek-v4.1-flash]):" — a model
    #: name no longer in use, in text the principal reads, on 155 files.
    calibration_offset: float = Field(
        default=0.0, description="The offset already applied (machine record, not prose)")
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


def _build_context(
    org_name: str,
    job_title: str,
    job_url: str,
    registry,
    use_history: bool,
    history_kwargs: Optional[dict] = None,
) -> tuple[str, Optional[str]]:
    """Build the additional context for the LLM and check the re-apply rule.

    Returns (context_text, reapply_kill_reason).
    """
    context_parts = []
    # `dataset_dir` is ours (the ledger overlay for the cooldowns), not
    # scan_history's — pop it before any **history_kwargs call.
    history_kwargs = dict(history_kwargs or {})
    dataset_dir = history_kwargs.pop("dataset_dir", None)

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
            prior = scan_history(org_name, use_gmail=True, **(history_kwargs or {}))
            if prior.has_application or prior.gmail_events:
                context_parts.append(f"**Prior contact**: {prior.summary()}")
        except Exception as e:
            logger.debug(f"History scan failed: {e}")

    context = "\n".join(context_parts) if context_parts else "No prior context."
    # A cached historical replay intentionally sets use_history=False: it
    # must not query Gmail while replaying an old registry snapshot.
    reapply_kill = (
        _check_reapply_kill(org_name, job_title, dataset_dir=dataset_dir,
                            job_url=job_url, **(history_kwargs or {}))
        if use_history
        else None
    )
    # Only reached when no layer killed: applied here, outcome unknown.
    applied_cap = (
        _check_applied_before_cap(org_name, job_title, dataset_dir=dataset_dir,
                                  **(history_kwargs or {}))
        if use_history and not reapply_kill
        else None
    )
    return context, reapply_kill, applied_cap


# ── Staleness (the advert's age) ─────────────────────────────────────────
# A stale advert is a firm-side failure signal, and all three explanations are
# bad for the principal we serve:
#   1. the firm cannot fill the role — something is wrong with the firm;
#   2. the firm filled it and nobody closed the advert — pure noise;
#   3. the firm farms CVs routinely — a pool to dip into when ~10% of staff
#      leave each year.
# Measured age is `registry.age_days` = last_seen - first_seen, i.e. how long the
# advert has been observed open, so it is a LOWER BOUND on the advert's real age
# (an org we started crawling last month hides its year-old posting). That
# asymmetry is deliberate: the penalty can under-punish a stale advert, never
# wrongly kill a fresh one. Full doctrine: agents/skills/stale-adverts/SKILL.md.
#
# Thresholds are deliberately tight (10 / 25 days, LJ 2026-09-20 — "things move
# faster"): the old 30/60 assumed a slower market. Note the scoring rule is now
# stricter than the registry's `long_lasting` label (open past 30 days), so an
# advert can be FLAG-capped or dropped before the lifecycle marks it at all.
STALE_FLAG_DAYS = int(os.getenv("LOVEWORK_STALE_FLAG_DAYS", "10"))
STALE_DROP_DAYS = int(os.getenv("LOVEWORK_STALE_DROP_DAYS", "25"))
STALE_FLAG_CAP = float(os.getenv("LOVEWORK_STALE_FLAG_CAP", "4.0"))


def _naive_date(value: str) -> Optional[datetime]:
    """Parse an application date so it can be compared with our naive cutoffs.

    A zoned value compared against a naive cutoff raises `TypeError`, and the
    `except` at each call site would silently *skip that application* — weakening
    the reapply hard-kill exactly where it must hold (an active application is
    not a fresh opportunity). Normalise to naive local time instead, so a valid
    date is never dropped from the check (2026-09-21).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _check_stale_reason(registry, org_name: str, job_title: str,
                        job_url: str = "") -> Optional[tuple[str, int]]:
    """Return (reason, age_days) when the advert is stale.

    Both signals we have are **lower bounds on the advert's real age**, so we
    take the larger (docs/28 §WP3a, agents/skills/stale-adverts):

    1. **`posted_at`** — the date the source reports. For an HN hiring thread
       that is the advert's own `created_at` and is effectively exact; for the
       Jack & Jill feed it is the offer's *current* publication stamp, and the
       feed is regenerated in a daily batch, so a **re-listed** offer gets a
       fresh date. Exact or resettable, it still bounds the age from below.
    2. **The observed-open floor** (`last_seen - first_seen`) — how long *we*
       have watched it open; a lower bound by construction, so a small value
       proves nothing about freshness.

    Taking the maximum is what stops a relist from laundering a long-open role:
    the source's fresh date cannot make the advert look younger than the months
    we have actually observed it.
    """
    if registry is None or STALE_DROP_DAYS <= 0:
        return None
    try:
        record = registry.get(org_name, job_title, job_url)
    except Exception as e:  # a registry hiccup must never kill a match
        logger.debug(f"Staleness lookup failed: {e}")
        return None
    if record is None:
        return None

    posted = str(getattr(record, "posted_at", "") or "")
    posted_age: Optional[int] = None
    if posted:
        try:
            posted_age = (date.today() - date.fromisoformat(posted)).days
        except ValueError:
            posted_age = None
        if posted_age is not None and posted_age < 0:
            # A date in the future means the source's clock disagrees with ours
            # (or the advert is scheduled). It is not a usable lower bound, so it
            # is dropped rather than trusted — `max()` would otherwise pick a
            # nonsense age, and the reason would read "-6 days ago".
            logger.debug(f"Ignoring future posted_at {posted!r} for {org_name}")
            posted_age = None
    floor_age = getattr(record, "age_days", None)
    if posted_age is None and floor_age is None:
        return None

    # Both signals are lower bounds on the advert's real age, so the larger one
    # is our best bound — and the threshold gates *that*, not one branch of it:
    # a mere re-scan (floor 8d, posted 5d) is not a stale advert.
    age = max(a for a in (posted_age, floor_age) if a is not None)
    if age < STALE_FLAG_DAYS:
        return None

    status = getattr(record, "status", "?")
    first_seen = getattr(record, "first_seen", "?")
    verdict = ("an advert open this long means the firm cannot fill it, filled "
               "it and never closed it, or farms CVs")

    if floor_age is not None and posted_age is not None and floor_age > posted_age:
        return (
            f"stale advert: open at least {floor_age} days (since {first_seen}, "
            f"status {status}) — the source re-published it {posted} "
            f"({posted_age} days ago), which is a relist, not a new advert; "
            f"{verdict}",
            age,
        )

    if posted_age is not None:
        return (
            f"stale advert: posted {posted} ({age} days ago, status {status}) "
            f"— {verdict}",
            age,
        )

    return (
        f"stale advert: open at least {age} days (since {first_seen}, status "
        f"{status}) — {verdict}; our age is a lower bound on its real age",
        age,
    )


def _apply_stale_kill(result: "MatchResult", reason: str) -> "MatchResult":
    """Score 0 / DROP: never surface an advert this old."""
    result.fit_score = 0.0
    result.reach_score = 0.0
    result.flourish_score = 0.0
    result.combined_score = 0.0
    result.score = 0.0
    result.recommended_action = "DROP"
    result.decision = "DROP"
    result.reasoning = f"AUTO-DROP: {reason}. " + result.reasoning
    return result


def _apply_stale_cap(result: "MatchResult", reason: str, age: int) -> "MatchResult":
    """Cap a stale-but-not-dead advert at FLAG level, whatever the LLM said."""
    if result.combined_score is not None and result.combined_score > STALE_FLAG_CAP:
        result.combined_score = STALE_FLAG_CAP
        result.score = STALE_FLAG_CAP
        result.recommended_action = "WATCH"
        result.decision = "FLAG"
    result.reasoning = f"STALE ({age}d): {reason}. " + result.reasoning
    return result


def _apply_staleness(result: "MatchResult", registry, org_name: str,
                     job_title: str, job_url: str = "") -> "MatchResult":
    """Deterministic staleness rule, applied after the LLM scored the advert."""
    found = _check_stale_reason(registry, org_name, job_title, job_url)
    if found is None:
        return result
    reason, age = found
    if age >= STALE_DROP_DAYS:
        return _apply_stale_kill(result, reason)
    return _apply_stale_cap(result, reason, age)


def _ledger_prior_applications(
    org_name: str, dataset_dir: Optional[Path],
) -> list:
    """Synthesize prior-application records from the outcomes ledger.

    A LoveWork-cut case is deliberately NOT mirrored into the principal's
    unified applications view (`application_packs.MIRROR_PACKS`, off by
    default since 2026-10), so a submission the system itself prepared is
    invisible to the directory scan — the PostHog rejection of 2026-10-17
    exposed the gap. The ledger is the single fact authority (D22) and
    `snapshot.py --case-outcomes` (docs/41 P1b) records those cases'
    deliberate markers, so the cooldowns read it as a second source.

    One record per distinct role title: earliest applied date, plus the
    rejection flag and date from any later row (an interview/offer row also
    implies an application). Org matching reuses the directory scan's alias
    rules, with `canonical_company` applied to row org names first.
    """
    if not dataset_dir:
        return []
    path = Path(dataset_dir) / "outcomes.jsonl"
    if not path.is_file():
        return []
    try:
        from history import PriorApplication, _org_aliases
    except Exception:
        return []
    try:
        from company_events import canonical_company
    except Exception:
        canonical_company = None  # type: ignore[assignment]

    aliases = _org_aliases(org_name)
    per_role: dict = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(row.get("kind") or "")
            if kind not in (
                "applied", "application_sent", "rejection", "interview",
                "offer", "withdrawal", "accepted",
            ):
                continue
            org = str(row.get("org_name") or row.get("case_org") or "")
            if canonical_company is not None:
                try:
                    org = canonical_company(org) or org
                except Exception:
                    pass
            if not org or not any(a in org.lower() for a in aliases):
                continue
            when = str(row.get("date") or "").strip()
            role = str(row.get("title") or "role unknown").strip()
            entry = per_role.setdefault(
                role, {"applied": "", "rejection": ""})
            if kind == "rejection":
                if when and when > entry["rejection"]:
                    entry["rejection"] = when
            elif when and (not entry["applied"] or when < entry["applied"]):
                # applied/application_sent set the date; interview/offer
                # imply an application happened no later than this row.
                entry["applied"] = when
    return [
        PriorApplication(
            date=e["applied"] or e["rejection"],
            company=org_name,
            role=role,
            path=Path(path),
            txt_path=None,
            has_rejection=bool(e["rejection"]),
            rejection_date=e["rejection"] or None,
            advert_hash="", title=role, url="", decision="",
            initial_score=None, outcome_markers=[],
        )
        for role, e in per_role.items()
    ]


def _check_reapply_kill(
    org_name: str, job_title: str, applications_dir: Optional[Path] = None,
    gmail_label: Optional[str] = None,
    gmail_credential_home: Optional[Path] = None,
    dataset_dir: Optional[Path] = None,
    use_gmail: bool = True,
    job_url: str = "",
) -> Optional[str]:
    """Return a reason string if we should auto-DROP this re-apply, else None.

    Two layers of protection:
      1. **Same-role cooldown** (REAPPLY_COOLDOWN_MONTHS, default 6): if
         the principal applied for a similar role (title jaccard >= 0.6)
         and was rejected, DROP. Catches "applied to ACME-AI-Scientist
         and got rejected — don't apply to ACME-AI-Scientist again".
      2. **Org-level cooldown** (REAPPLY_ORG_COOLDOWN_MONTHS, default 18):
         if the principal was rejected at the same org within the last N
         months (any role), DROP. Catches "Poolside rejected Evaluations
         2 months ago — don't apply to Poolside Pre-training today,
         even though the role title is different".

    Both are configurable via env. The org-level cooldown is the
    user-controlled knob: 12 = 1 year, 18 = 1.5y, 24 = 2 years.

    Args:
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
    """
    # **The advert's own URL names the employer** (2026-10-18). Four HuggingFace GOs
    # were filed under "TTS Arena V2" and "Open ASR Leaderboard" — HF *Space* names,
    # taken from a tracker that lists Spaces beside companies — so the HuggingFace
    # cooldown never matched and a rejected org stayed on the GO list. The ATS slug
    # (`apply.workable.com/huggingface/…`) is the employer's own filing, so when it
    # disagrees with the page-derived org, both are asked.
    orgs = [org_name]
    try:
        from lead_identity import org_from_ats_url

        ats_org = org_from_ats_url(job_url)
        if ats_org and ats_org.lower() != (org_name or "").lower():
            orgs.append(ats_org)
    except Exception:
        pass

    try:
        from history import scan_history
        # use_gmail=True so the kill catches rejections that were only seen
        # in the Gmail inbox (not yet recorded in the applications/ .txt
        # file). Gmail is the canonical rejection signal — applications/
        # txt files are updated after the fact. The Gmail accessor
        # gracefully no-ops if google_api.py is not set up.
        history_kwargs = {"applications_dir": applications_dir}
        if gmail_label is not None:
            history_kwargs["gmail_label"] = gmail_label
        if gmail_credential_home is not None:
            history_kwargs["gmail_credential_home"] = gmail_credential_home
        # `use_gmail=False` is for the **bulk** caller: a wiki repair asks this for
        # every GO/MAYBE block, and one Gmail scan per block hangs the pass (measured
        # 2026-10-17 — the repair produced no output at all). The ledger and the
        # applications tree are the cheap authority; Gmail is for the *assessment*,
        # once per job, where a rejection that has not been ingested yet still counts.
        prior = scan_history(org_name, use_gmail=use_gmail, **history_kwargs)
        # …and every org the advert itself names. A rejection is recorded against the
        # *employer*, and a page-derived org can be a project or product name.
        for extra in orgs[1:]:
            more = scan_history(extra, use_gmail=use_gmail, **history_kwargs)
            prior.applications.extend(more.applications)
    except Exception:
        return None
    for candidate in orgs:
        prior.applications.extend(_ledger_prior_applications(candidate, dataset_dir))

    if not prior.applications:
        return None

    # Layer 0 (P4): an active application — applied, no rejection yet — is
    # principal intent evidence, not a fresh opportunity. Same-role within
    # the short cooldown, no rejection recorded: suppress so it is never
    # re-surfaced as a GO while the principal is already pursuing it. A
    # rejected app falls through to layers 1/2, whose reasons name the
    # rejection rather than claiming it is still in progress.
    role_cutoff = datetime.now() - timedelta(days=REAPPLY_COOLDOWN_MONTHS * 30)
    for app in prior.applications:
        app_date = _naive_date(app.date)
        if app_date is None:
            continue
        if (app_date >= role_cutoff and not app.has_rejection
                and _titles_similar(app.role, job_title)):
            return (
                f"Active application on {app.date} for similar role '{app.role}' "
                f"(no rejection recorded — in progress). Not a fresh opportunity."
            )

    # Layer 1: same-role short cooldown.
    for app in prior.applications:
        app_date = _naive_date(app.date)
        if app_date is None:
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
            app_date = _naive_date(app.date)
            if app_date is None:
                continue
            if app_date >= org_cutoff and app.has_rejection:
                return (
                    f"Org-level cooldown: {org_name} rejected LJ on {app.date} "
                    f"(role '{app.role}'). No re-apply to this org for at least "
                    f"{REAPPLY_ORG_COOLDOWN_MONTHS} months."
                )

    return None


def _check_applied_before_cap(
    org_name: str, job_title: str, applications_dir: Optional[Path] = None,
    gmail_label: Optional[str] = None,
    gmail_credential_home: Optional[Path] = None, use_gmail: bool = True,
    dataset_dir: Optional[Path] = None,
) -> Optional[str]:
    """Cap a GO when we applied to this org and **the outcome is unknown**.

    The three cooldown layers all require either a recent same-role application or
    a *recorded rejection*. LJ's Poetiq case (2026-10-14) is neither: applied
    2025-12-20, no rejection on file (the known gap — "no rejection found in .txt
    or Gmail"), nine months later — so every layer missed, the advert came back as
    a fresh **GO 7.4/10**, and the pipeline cut a pack for a role he had already
    tried and a second he had chosen to skip.

    A hard DROP would assert a rejection we never recorded, so this **caps**: the
    judgement stays, the pack does not get cut, and the reason names the date and
    says the outcome is unknown. The principal decides — which is what "we applied
    here before" should have asked all along.
    """
    if REAPPLY_ORG_COOLDOWN_MONTHS <= 0:
        return None
    try:
        from history import scan_history

        history_kwargs = {"applications_dir": applications_dir}
        if gmail_label is not None:
            history_kwargs["gmail_label"] = gmail_label
        if gmail_credential_home is not None:
            history_kwargs["gmail_credential_home"] = gmail_credential_home
        prior = scan_history(org_name, use_gmail=use_gmail, **history_kwargs)
    except Exception:
        return None
    prior.applications.extend(_ledger_prior_applications(org_name, dataset_dir))
    org_cutoff = datetime.now() - timedelta(days=REAPPLY_ORG_COOLDOWN_MONTHS * 30)
    for app in prior.applications:
        app_date = _naive_date(app.date)
        if app_date is None or app_date < org_cutoff:
            continue
        if getattr(app, "has_rejection", False):
            continue                      # a rejection is a kill, handled elsewhere
        return (
            f"applied at {org_name} on {app.date} for '{app.role}' and no outcome "
            f"is on file, so this is not a fresh opportunity — your call whether to "
            f"try again"
        )
    return None


def _check_closed_advert_kill(job_url: str, principal: str) -> Optional[str]:
    """A DROP when this advert has been **seen closed**.

    Two authorities, both dated: the link re-check's verdicts (`expired`,
    `unreachable`) and the closing piles' ledgers. Matched by the canonical advert
    URL, because the same advert arrives spelled several ways — LinkedIn's
    `/comm/` mail form against the plain one, a trailing slash, a tracking
    parameter (the 2026-10-14 John Lewis case: the closed verdict was recorded on
    2026-09-12 and a fresh GO 8.5/10 pack was cut on 2026-10-09, three weeks
    later).

    Pre-LLM and absolute: an advert that is closed is not a lead, and no score
    should be able to bring it back. The principal's own case directories are a
    different matter — the reapply layers own those.
    """
    from lead_identity import advert_url_key

    key = advert_url_key(job_url)
    if not key or not principal:
        return None
    import retire

    state_root = config.STATE_DIR / principal
    # No blanket `except`: the first version swallowed whatever went wrong and
    # returned None, which is indistinguishable from "the advert is fine" — the
    # silent-degradation class this project keeps paying for. A missing directory
    # is handled where it occurs, not by hiding the whole function.
    if key in retire.retired_urls(principal):
        return (f"this advert was already closed and retired to "
                f"{principal}/gone or {principal}/expired — it is not a new "
                f"opportunity")
    checks = state_root / "cache" / "link_checks"
    if not checks.is_dir():
        return None
    for path in sorted(checks.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue                      # an unreadable verdict file is skipped
        for verdict in payload.get("verdicts", []):
            if verdict.get("verdict") not in ("expired", "unreachable"):
                continue
            if advert_url_key(verdict.get("url") or "") != key:
                continue
            detail = str(verdict.get("detail") or "").strip()
            return (f"the 06:00 world pass found this advert "
                    f"{verdict['verdict']}"
                    + (f" ({detail})" if detail else "")
                    + f" on {str(payload.get('checked_at') or '')[:10]}")
    return None


def _apply_reapply_cap(result: MatchResult, cap: Optional[str]) -> MatchResult:
    """A cap keeps the judgement and drops the GO.

    `GO` becomes `FLAG`, with the reason first: a flagged advert still gets its
    analysis, its lead page and the brief's attention, and no application pack is
    cut for a company the principal has already applied to.
    """
    if not cap or result.decision != "GO":
        return result
    result.decision = "FLAG"
    result.recommended_action = "USE_AS_GAP_SIGNAL"
    result.reasoning = f"CAP (was GO): {cap}. " + result.reasoning
    return result


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


# A profile can declare the countries it cannot work in, with a marker in
# work_auth.md:
#
#     <!-- lovework:work-auth-exclude-countries: US -->
#
# The existing work-auth kill is *text*-based ("US citizen only", "no
# sponsorship"); it never fires on a role that merely sits in an excluded
# country. VJ's 2026-09-13 sweep surfaced 72 US/Canada-located listings — 6 as
# "United States", 6 as "REMOTE (US)", the rest as "San Francisco, CA",
# "NYC", "Austin, TX" — several of which scored MAYBE while the profile says a
# purely US role is an automatic DROP.
#
# The rule is country-scoped and permissive by construction: a location that
# also names an eligible region is left to the LLM, and so is any advert whose
# text shows a real path (sponsorship, a UK entity, employer-of-record,
# worldwide remote). Only a location that is exclusively in an excluded country
# is killed.
COUNTRY_EXCLUSION_MARKER = "lovework:work-auth-exclude-countries"
_COUNTRY_EXCLUSION_RE = re.compile(
    r"<!--\s*lovework:work-auth-exclude-countries\s*:?\s*(?P<codes>[^>]*?)-->", re.I
)

# Only *unambiguous* US signals: a location that could equally be read as
# another country is left to the LLM rather than killed. Bare "us" is included
# because this pattern is matched against the location field only ("join us"
# lives in descriptions, which this never sees).
_US_LOCATION_RE = re.compile(
    r"(?<![a-z])(united states|u\.?s\.?a\.?|usa|us|us-based|us based|"
    r"us-only|us only|remote\s*\(\s*us|remote\s*us\b|"
    r"us\s*(pacific|eastern|central|mountain|timezone|time zone)|"
    r"new york|nyc|san francisco|bay area|san jose|san diego|seattle|austin|"
    r"boston|chicago|los angeles|denver|atlanta|dallas|houston|phoenix|"
    r"philadelphia|miami|minneapolis|detroit|nashville|charlotte|portland|"
    r"washington,? d\.?c|palo alto|mountain view|sunnyvale|bellevue|"
    r"pittsburgh|cincinnati|cleveland|columbus|indianapolis|st\.? louis|baltimore|"
    r"milwaukee|raleigh|salt lake|sacramento|orlando|tampa|san antonio|fort worth|"
    r"jacksonville|memphis|louisville|richmond|buffalo|hartford|providence|tucson|"
    r"albuquerque|omaha|las vegas|oklahoma city|kansas city|virginia beach|"
    r"charleston|savannah|des moines|madison|boise)(?![a-z])",
    re.I,
)
# US state codes that are NOT also ISO-3166 country codes and not a market the
# principals may work in. Delaware (DE = Germany), Montana (MT = Malta),
# Georgia (GA = Gabon), Indiana (IN = India), California (CA = Canada) and
# their kin are deliberately absent: "Berlin, DE" must not read as Delaware,
# and Malta is one of VJ's preferred markets.
_US_STATE_RE = re.compile(
    r",\s*(AK|AZ|CT|FL|HI|IL|IA|KS|KY|MI|MN|MO|MS|NC|ND|NH|NJ|NM|NV|NY|OH|OK|"
    r"OR|RI|SD|TX|UT|VT|WA|WV|WI|WY)\b"
)

# A location naming any of these is *not* exclusively in an excluded country.
ELIGIBLE_REGION_SIGNALS = (
    "united kingdom", "uk", "england", "scotland", "wales", "london",
    "eu", "europe", "eea", "emea", "schengen", "worldwide", "world",
    "global", "anywhere", "remote (any",
    # Remote shapes that are workable from outside the country: the 2026-10-09
    # VJ row "Berkeley Office; Remote (International); Remote (US)" was killed
    # by the rule because "remote (us" matched and "remote (international" was
    # not an escape hatch. A kill that cannot see a workable shape is a false
    # positive, and a false DROP is worse than a missed one: it removes a real
    # opportunity silently (2026-10-09).
    "remote (international", "remote (global", "remote (worldwide",
    "international remote", "remote worldwide", "remote global",
    "worldwide remote", "global remote", "remote-first", "distributed team",
    "remote in ", "remote across",
    # The principal's own stated markets, so a mixed posting is never killed:
    "poland", "malta", "netherlands", "ireland", "germany", "france",
    "spain", "portugal", "greece", "north macedonia",
)

# …and an advert whose text shows one of these has a real path for the principal.
# Positive phrasing only: a bare "sponsor" matched "no sponsorship available" and
# switched the kill OFF — the exact opposite of what the text says (found
# 2026-09-22 while chasing a US-only advert that survived with 205 mentions of
# "United States" in its body).
ELIGIBLE_PATH_SIGNALS = (
    "visa sponsorship available", "sponsorship available", "we sponsor",
    "will sponsor", "sponsor a visa", "sponsorship provided", "can sponsor",
    "employer of record", "employer-of-record", "eor ", "deel",
    "remote worldwide", "worldwide remote", "globally remote", "any location",
    "uk entity", "uk-based entity", "hire in the uk", "contract via",
    "uk or eu", "eu or uk", "remote (eu", "remote (uk", "remote (global",
    "non-us", "non us", "outside the us",
)

#: A bare country mention in the advert body or slug. Weaker than a stated
#: requirement, so it only counts when the location field names no eligible
#: region and the text claims no hiring path — which is what makes it decisive
#: for the common job-board shape (location empty, country named in the body).
_US_MENTION_RE = re.compile(
    r"(?<![a-z])(united\s+states|u\.s\.a\.?|u\.s\.|usa|us-based|us based)(?![a-z])",
    re.IGNORECASE,
)

# A US *requirement* stated in the advert body or the advert's URL. Requirement
# phrasing only: "we also have offices in the United States" must not kill a
# role, but "authorized to work in the US" must. The location-only rule missed
# both a role whose location field was empty and the one whose advert text named
# Boston onboarding while the location said something else (2026-09-22).
_US_REQUIREMENT_RE = re.compile(
    r"(authoriz\w*\s+to\s+work\s+in\s+(the\s+)?(us|u\.s\.?|united\s+states)"
    r"|(us|u\.s\.?|united\s+states)\s+work\s+authoriz\w*"
    r"|work\s+authoriz\w*\s+in\s+(the\s+)?(us|u\.s\.?|united\s+states)"
    r"|eligible\s+to\s+work\s+in\s+the\s+(us|u\.s\.?|united\s+states)"
    r"|must\s+(be\s+)?(reside|live|be\s+based|be\s+located)\s+in\s+the\s+(us|u\.s\.?|united\s+states)"
    r"|us\s+citizen\w*|u\.s\.\s+citizen\w*|us\s+person|green\s+card|permanent\s+resident"
    r"|w-?2\s+only)",
    re.IGNORECASE,
)

#: The advert's *slug* encodes the role's location on job boards
#: ("…-sr-analyst-pricing-pittsburgh/", "…-united-states-actuary/").
_US_URL_RE = re.compile(r"(?:-|/)(?:united-states|usa)(?:-|/|$)", re.IGNORECASE)


def declared_country_exclusions(profile: str) -> List[str]:
    """Country codes the profile declares it cannot work in."""
    text = profile or ""
    if COUNTRY_EXCLUSION_MARKER not in text.lower():
        return []
    match = _COUNTRY_EXCLUSION_RE.search(text)
    if not match:
        return []
    return [c.strip().upper() for c in re.split(r"[|,;]", match.group("codes") or "") if c.strip()]


#: A path signal that appears *negated* is not a path: "no visa sponsorship
#: available" contains "visa sponsorship available" and would otherwise rescue
#: exactly the adverts the exclusion is meant to remove (2026-09-22).
_NEGATED_PATH_RE = re.compile(
    r"\b(no|not|without|cannot|can't|unable to|do not|don't|does not|doesn't)\b"
    r"[^.;!?]{0,40}?"
    r"(sponsor\w*|employer of record|employer-of-record|\beor\b|\bdeel\b)",
    re.IGNORECASE,
)


def _check_country_auth_kill(
    location: str, job_description: str, profile: str, job_url: str = ""
) -> Optional[str]:
    """Return a reason when the role sits only in a country the principal cannot work in.

    The evidence is the whole advert, not just its location field: a role can name
    no location and still require US work authorisation in its body, and job-board
    URLs encode the role's country in the slug ("…-pricing-pittsburgh/",
    "…-united-states-actuary/"). Both were reaching the report as ranked leads
    until 2026-09-22 — Pittsburgh is not in the city list and "PA" is deliberately
    absent from the state list (PA = Panama), so a "Pittsburgh, PA" advert matched
    nothing and was handed to the LLM, which put it in MAYBE at 6.8.
    """
    codes = declared_country_exclusions(profile)
    if not codes or "US" not in codes:
        return None
    location = (location or "").strip()
    description = job_description or ""
    if not location and not description and not job_url:
        return None

    if any(signal in location.lower() for signal in ELIGIBLE_REGION_SIGNALS):
        # Mixed eligibility: "REMOTE (US/Canada/Brazil/Poland/UK/India)" is a real
        # opportunity, so the LLM judges it with the profile in front of it.
        return None

    advert = f"{location} {description} {job_url}".lower()
    # A negated path signal is not a path (see _NEGATED_PATH_RE).
    advert_positive = _NEGATED_PATH_RE.sub(" ", advert)
    if any(signal in advert_positive for signal in ELIGIBLE_PATH_SIGNALS):
        return None

    matched = ""
    if _US_LOCATION_RE.search(location) or _US_STATE_RE.search(location):
        matched = location[:60]
    elif _US_URL_RE.search(job_url or ""):
        matched = (job_url or "")[:80]
    else:
        requirement = _US_REQUIREMENT_RE.search(description) or _US_MENTION_RE.search(description)
        if requirement:
            matched = requirement.group(0)[:60]

    if not matched:
        return None

    return (
        f"location deal-breaker (matched '{matched}'): the role is only in a "
        f"country the principal cannot work in ({', '.join(codes)})"
    )


def _apply_country_auth_kill(result: MatchResult, reason: Optional[str]) -> MatchResult:
    """If the country kill fires, override the result (hard DROP)."""
    if reason:
        for f in ("fit_score", "reach_score", "flourish_score"):
            setattr(result, f, 0.0)
        result.combined_score = 0.0
        result.recommended_action = "DROP"
        result.score = 0.0
        result.decision = "DROP"
        result.reasoning = f"AUTO-DROP: {reason}. " + result.reasoning
    return result


# Work-mode kill (LJ, 2026-10-06 — the Callosum lesson: a 9.7/GO advert whose
# board page said "Location Type: On-site"; the LLM read the label and scored
# "commutable from the home town" as a plus). A profile carries
# <!-- lovework:work-mode: remote-only --> in work_auth.md; for that principal
# an advert that states on-site and shows no remote/hybrid signal anywhere is
# a hard DROP. Permissive by construction, like the country rule: any
# remote/hybrid/flexible/distributed mention leaves the job to the LLM, and
# profiles without the marker (vj, kj today) are untouched.
WORK_MODE_MARKER = "lovework:work-mode: remote-only"

# The ALLOW-list dual of `COUNTRY_EXCLUSION_MARKER`: the places the principal's
# body may work in. The exclude-list kills "exclusively in an excluded
# country"; this kills "exclusively outside the allowed places" — the only
# shape that can express "UK and MK only", and the gap that let the 2026-10-03
# Gradium rows score GO 9.7 in Paris with six packs prepared while the file
# said EU-located is not a green signal.
#
#     <!-- lovework:work-location-allowed: UK, MK -->
#
# Permissive by construction, like its siblings: no remote signal anywhere in
# the location names a workplace outside the list, a location that also names an
# allowed place (a mixed posting: "Paris; London") is left to the model, and a
# profile without the marker is untouched. The place vocabulary lives in
# lead_identity so the matcher, the wiki audit and the re-check share it.
WORK_LOCATION_MARKER = "lovework:work-location-allowed"
WORK_LOCATION_RE = re.compile(
    r"<!--\s*lovework:work-location-allowed\s*:?\s*(?P<places>[^>]*?)-->", re.I)


def _allowed_work_places(profile: str) -> list[str]:
    """The principal's allowed physical workplaces, or [] when undeclared."""
    if not profile or WORK_LOCATION_MARKER not in profile.lower():
        return []
    m = WORK_LOCATION_RE.search(profile)
    if not m:
        return []
    return [p.strip() for p in re.split(r"[,;/]", m.group("places")) if p.strip()]


def _check_work_location_kill(
    location: str, job_description: str, profile: str
) -> Optional[str]:
    """Reason string when an advert sits outside the principal's places.

    `lead_identity.is_ineligible_work_location` decides the eligibility ("a
    physical place outside the allow-list, no remote signal, no allowed place
    named"); this reads the marker and words the reason. A principal who has not
    declared the rule is never killed by it.
    """
    from lead_identity import classify_work_location, is_ineligible_work_location

    allowed_places = _allowed_work_places(profile)
    if not allowed_places:
        return None
    # The LOCATION is the workplace; the description is only consulted when the
    # source gave no location at all (a HN post with "Paris" only in the body).
    where = (location or "").strip() or (job_description or "").strip()
    if not is_ineligible_work_location(where):
        return None
    _remote, _allowed, others = classify_work_location(where)
    place = ", ".join(dict.fromkeys(others[:3])) or where[:40]
    # Deliberately does not claim "no remote option": an advert can say
    # "SF Bay Area (HYBRID)" and still be unworkable, and the reason must only
    # assert what was read.
    return (f"work location '{place}' is outside this principal's allowed "
            f"places ({', '.join(allowed_places)}) — the role is not stated as "
            f"remote, so it needs a relocation and a right-to-work path")
_ONSITE_ONLY_RE = re.compile(r"(?<![a-z])(on-?site|in-?office)(?![a-z])", re.I)
_WORK_MODE_OK_RE = re.compile(
    r"(?<![a-z])(remote|hybrid|work[- ]from[- ]home|wfh|telecommut"
    r"|flexible|distributed|anywhere)(?![a-z])", re.I,
)


# "No remote." must not read as a remote signal: strip negated mentions
# before the permissive guard looks. "Remote: No", "not remote", "zero
# hybrid" all neutralise here (the 2026-10-06 test caught the first shape).
_NEG_WORK_MODE_RE = re.compile(
    r"(?<![a-z])(?:(?:no|not|zero|without|never)\s+(?:remote|hybrid)"
    r"|(?:remote|hybrid)\s*[:=]?\s*(?:no|none|unavailable))(?![a-z])", re.I)


def _sanitize_work_mode_text(text: str) -> str:
    """The work-mode rule's text: negated remote/hybrid mentions removed."""
    return _NEG_WORK_MODE_RE.sub(" ", text or "")


def _check_work_mode_kill(
    location: str, job_description: str, profile: str
) -> Optional[str]:
    """Reason string when a remote-only principal faces an on-site-only job."""
    if WORK_MODE_MARKER not in (profile or "").lower():
        return None
    text = _sanitize_work_mode_text(f"{location or ''} {job_description or ''}")
    if not text.strip() or _WORK_MODE_OK_RE.search(text):
        return None
    m = _ONSITE_ONLY_RE.search(text)
    if m:
        return (f"work-mode conflict: advert states '{m.group(0).strip()}' with no "
                f"remote/hybrid signal (profile is remote-only)")
    return None


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


# Not-a-job hard-kill: platform boilerplate that the parsers sometimes emit as
# a listing. The 2026-09-10 KJ email scored "You'll receive notifications when
# new jobs are posted that match your search preferences." 8.0/GO.
NOT_A_JOB_PATTERNS = [
    r"job alert has been created",
    r"receive notifications when new jobs are posted",
    r"matching your search preferences",
]


def _check_not_a_job_kill(job_title: str, job_description: str = "") -> Optional[str]:
    """Return a reason string when the 'listing' is platform boilerplate, not a job."""
    text = f"{job_title or ''} {job_description or ''}".lower()
    if not text.strip():
        return None
    for pat in NOT_A_JOB_PATTERNS:
        if re.search(pat, text):
            return f"platform boilerplate, not a job advert (matched '{pat}')"
    return None


def _apply_not_a_job_kill(result: MatchResult, reason: Optional[str]) -> MatchResult:
    """If the not-a-job kill fires, override the result (hard DROP)."""
    if reason:
        for f in ("fit_score", "reach_score", "flourish_score"):
            setattr(result, f, 0.0)
        result.combined_score = 0.0
        result.recommended_action = "DROP"
        result.score = 0.0
        result.decision = "DROP"
        result.reasoning = f"AUTO-DROP: {reason}. " + result.reasoning
    return result


def _check_profile_preference_exclusion(
    profile: str,
    job_title: str,
    job_description: str = "",
) -> Optional[str]:
    """Return a reason when a profile explicitly excludes this profession.

    This deliberately handles only the narrowly marked AI/ML/NLP-research
    exclusion. Ordinary adjacent roles remain LLM-scored; a principal can still
    seek a classical-statistics Data Scientist job that happens to mention AI.
    """
    profile_lower = (profile or "").lower()
    if AI_ML_NLP_RESEARCH_EXCLUSION_MARKER not in profile_lower:
        return None

    title = (job_title or "").lower()
    description = (job_description or "").lower()
    if any(re.search(pattern, title) for pattern in AI_ML_NLP_TITLE_PATTERNS):
        return "explicit principal preference: AI/ML/NLP work is not the desired profession"

    is_frontier_research = (
        any(re.search(pattern, title) for pattern in FRONTIER_RESEARCH_TITLE_PATTERNS)
        and any(re.search(pattern, description) for pattern in FRONTIER_AI_RESEARCH_PATTERNS)
    )
    if is_frontier_research:
        return (
            "explicit principal preference: frontier AI research is not the desired profession"
        )
    return None


def _apply_profile_preference_exclusion(
    result: MatchResult,
    preference_exclusion: Optional[str],
) -> MatchResult:
    """Make a durable profession exclusion an explainable low-score DROP."""
    if preference_exclusion:
        for field in ("fit_score", "reach_score", "flourish_score"):
            setattr(result, field, 1.0)
        result.combined_score = 1.0
        result.recommended_action = "DROP"
        result.score = 1.0
        result.decision = "DROP"
        result.reasoning = f"AUTO-DROP: {preference_exclusion}. " + result.reasoning
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
        history_kwargs: Optional[dict] = None,
        principal: str = "",
    ):
        self.llm = llm
        self.profile = profile
        #: Whose state the closed-advert check reads. A named default would be the
        #: principal-blind-default class this project has paid for repeatedly, so
        #: an empty value simply makes that one check inert until a caller says
        #: who it is scoring for.
        self.principal = principal
        self.registry = registry
        self.use_history = use_history
        self.history_kwargs = history_kwargs or {}

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ) -> MatchResult:
        # Work-auth hard-kill fires BEFORE the LLM call (no point paying to match a role
        # the principal isn't eligible to take, e.g. "US citizen only").
        work_auth_kill = _check_work_auth_kill(location, job_description)
        if work_auth_kill:
            return _apply_work_auth_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), work_auth_kill
            )
        country_kill = _check_country_auth_kill(location, job_description, self.profile, job_url)
        if country_kill:
            return _apply_country_auth_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), country_kill
            )
        work_location_kill = _check_work_location_kill(location, job_description,
                                                       self.profile)
        if work_location_kill:
            return _apply_work_auth_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), work_location_kill
            )
        work_mode_kill = _check_work_mode_kill(location, job_description, self.profile)
        if work_mode_kill:
            return _apply_country_auth_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), work_mode_kill
            )
        not_a_job = _check_not_a_job_kill(job_title, job_description)
        if not_a_job:
            return _apply_not_a_job_kill(
                MatchResult(score=0.0, decision="DROP", reasoning=""), not_a_job
            )

        preference_exclusion = _check_profile_preference_exclusion(
            self.profile, job_title, job_description
        )
        ideology_exclusion = preference_exclusion or _check_ideology_exclusion(
            self.profile, org_name
        )
        if ideology_exclusion:
            return _apply_profile_preference_exclusion(
                MatchResult(score=1.0, decision="DROP", reasoning=""),
                ideology_exclusion,
            )

        context, reapply_kill, applied_cap = _build_context(
            org_name, job_title, job_url, self.registry, self.use_history, self.history_kwargs
        )
        # A closed advert is not a lead, whatever it scores (2026-10-14). Decided
        # before the model call: there is nothing to judge, and the call is the
        # expensive part of a sweep.
        _closed = _check_closed_advert_kill(job_url, self.principal)
        if _closed:
            return _apply_reapply_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                _closed)

        prompt = prompts.match_prompt(org_name, job_title, job_description, context,
                                      self.profile, location)

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

        # Model calibration, then the calibrated tier. A model swap moves the
        # scale (mimo-v2.6-flash scores ~2.5 lower than the mimo-v2.5 baseline
        # on identical input, which emptied VJ's GO tier 22 → 1 on 2026-09-22),
        # so the scores are shifted onto the reference scale before anything
        # reads them, and GO is decided by the calibrated score against the
        # empirically-anchored floor rather than by the model's own action word.
        calibrate_result(raw, getattr(self.llm, "model", "") or "")
        apply_tier_bands(raw)

        # Staleness is deterministic and applied last: it must be able to
        # override a high LLM score (a 3-month-old advert is not a GO, ever).
        raw = _apply_staleness(raw, self.registry, org_name, job_title, job_url)
        return _apply_reapply_cap(
            _apply_reapply_kill(raw, reapply_kill or _closed), applied_cap)


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
        history_kwargs: Optional[dict] = None,
    ):
        self.profile = profile
        self.registry = registry
        self.use_history = use_history
        self.history_kwargs = history_kwargs or {}

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
        country_kill = _check_country_auth_kill(location, job_description, self.profile, job_url)
        if country_kill:
            return _apply_country_auth_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                country_kill,
            )
        work_location_kill = _check_work_location_kill(location, job_description,
                                                       self.profile)
        if work_location_kill:
            return _apply_work_auth_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                work_location_kill,
            )
        work_mode_kill = _check_work_mode_kill(location, job_description, self.profile)
        if work_mode_kill:
            return _apply_country_auth_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                work_mode_kill,
            )
        not_a_job = _check_not_a_job_kill(job_title, job_description)
        if not_a_job:
            return _apply_not_a_job_kill(
                MatchResult(fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                            combined_score=0.0, recommended_action="DROP",
                            score=0.0, decision="DROP", reasoning=""),
                not_a_job,
            )

        preference_exclusion = _check_profile_preference_exclusion(
            self.profile, job_title, job_description
        )
        ideology_exclusion = preference_exclusion or _check_ideology_exclusion(
            self.profile, org_name
        )
        if ideology_exclusion:
            return _apply_profile_preference_exclusion(
                MatchResult(fit_score=1.0, reach_score=1.0, flourish_score=1.0,
                            combined_score=1.0, recommended_action="DROP",
                            score=1.0, decision="DROP", reasoning=""),
                ideology_exclusion,
            )

        context, reapply_kill, applied_cap = _build_context(
            org_name, job_title, job_url, self.registry, self.use_history, self.history_kwargs
        )
        # A closed advert is not a lead, whatever it scores (2026-10-14).
        _closed = _check_closed_advert_kill(job_url, self.principal)

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

        # Model calibration, then the calibrated tier — after the DSPy call, so
        # a failure path returns its unscored result untouched. A model swap
        # moves the scale (mimo-v2.6-flash scores ~2.5 lower than the mimo-v2.5
        # baseline on identical input, which emptied VJ's GO tier 22 → 1 on
        # 2026-09-22), so scores are shifted onto the reference scale before
        # anything reads them, and GO is decided by the calibrated score against
        # the empirically-anchored floor rather than by the model's own action.
        calibrate_result(raw, getattr(self.llm, "model", "") or "")
        apply_tier_bands(raw)

        raw = _apply_staleness(raw, self.registry, org_name, job_title, job_url)
        return _apply_reapply_cap(
            _apply_reapply_kill(raw, reapply_kill or _closed), applied_cap)

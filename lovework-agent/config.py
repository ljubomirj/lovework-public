"""
Configuration and profile definitions for lovework-agent.

LoveWork (lovework.be) — a personal job discovery agent.
Mission: LoveWork. Work that you love, so you never work a day in your life.
"""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────
# lovework-agent/ — the package directory
AGENT_ROOT = Path(__file__).resolve().parent
# lovework/ — the parent repo (sibling of lovework-agent/)
# Override with LOVEWORK_ROOT env var for portability across machines.
LOVEWORK_ROOT = Path(os.getenv("LOVEWORK_ROOT", AGENT_ROOT.parent))
# ~/.lovework/ — user-level config (profiles, custom data sources)
USER_CONFIG_DIR = Path(os.getenv("LOVEWORK_HOME", Path.home() / ".lovework"))

# Wiki + cache live with the agent (per-repo state)
WIKI_ROOT = Path(os.getenv("LOVEWORK_WIKI", AGENT_ROOT / "wiki"))
WIKI_ORGS = WIKI_ROOT / "orgs"
WIKI_REPORTS = WIKI_ROOT / "reports"
CACHE_DIR = Path(os.getenv("LOVEWORK_CACHE", AGENT_ROOT / "cache"))
DATASET_DIR = Path(os.getenv("LOVEWORK_DATASET", AGENT_ROOT / "dataset"))

for d in (WIKI_ROOT, WIKI_ORGS, WIKI_REPORTS, CACHE_DIR, DATASET_DIR, USER_CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── LLM ──────────────────────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# ── Firecrawl ────────────────────────────────────────────────────────────
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

# ── Crawl limits ─────────────────────────────────────────────────────────
MAX_PAGES_PER_ORG = int(os.getenv("MAX_PAGES_PER_ORG", "4"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "2"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))

# ── Job filters ──────────────────────────────────────────────────────────
# Only pursue jobs posted within the last N weeks (older = likely filled)
MAX_JOB_AGE_WEEKS = int(os.getenv("MAX_JOB_AGE_WEEKS", "4"))

# ── Profile loading ──────────────────────────────────────────────────────
# Profiles are looked up in this order (first match wins):
#   1. ~/.lovework/profiles/<name>/   (user-private profiles)
#   2. lovework/profiles/<name>/      (repo-level shared profiles)
#   3. lovework-agent/profiles/<name>/ (bundled defaults — empty for now)
#
# Profile structure (3-layer model):
#   soul.md         — what the candidate is, wants, avoids (always loaded)
#   cv-short.md     — Layer 2 (tip): current highest-SNR short CV
#   bio-long.md     — Layer 1 (long path): full past→present CV (loaded by load_bio())
#   possibilities.md — Layer 3 (branching): future directions with matcher signals
#   roles/<role>.md — role-specific criteria (one or more, one selected per run)

PROFILES_DIR_CANDIDATES = [
    USER_CONFIG_DIR / "profiles",
    LOVEWORK_ROOT / "profiles",
    AGENT_ROOT / "profiles",
]


def get_profiles_dir() -> Path:
    """Return the first existing profiles dir, or default to user config."""
    for p in PROFILES_DIR_CANDIDATES:
        if p.is_dir():
            return p
    return PROFILES_DIR_CANDIDATES[0]  # default to user config

PROFILES_DIR = get_profiles_dir()


# ── External data sources (configurable paths) ──────────────────────────
# The user can point these at any directory via env var.
# Defaults: look in lovework/ (the repo) for the data files.
APPLICATIONS_DIR = Path(
    os.getenv("LOVEWORK_APPLICATIONS_DIR", LOVEWORK_ROOT / "applications")
)
HF_TRACKER_DIR = Path(
    os.getenv("LOVEWORK_HF_TRACKER_DIR", LOVEWORK_ROOT / "AI-for-HF-startup-tracker")
)
NEOLAB_TRACKER = Path(
    os.getenv("LOVEWORK_NEOLAB_TRACKER", LOVEWORK_ROOT / "neolab-and-emerging-ai-lab-tracker.txt")
)


def load_profile_text(profile_name: str, role: Optional[str] = None) -> str:
    """Load a candidate profile as a single combined string for the matcher.

    Combines: soul + work-auth + market-position + cv-short + possibilities + (optional role).
    Long bio (Layer 1) is available via load_bio() if needed for a specific call
    (kept out of the default load to control token cost).
    """
    pdir = PROFILES_DIR / profile_name.lower()
    if not pdir.is_dir():
        raise ValueError(f"Profile not found: {profile_name} (looked in {pdir})")

    parts = []

    soul = pdir / "soul.md"
    if soul.exists():
        parts.append("# CANDIDATE SOUL\n\n" + soul.read_text(encoding="utf-8"))

    # Work authorization — where the candidate may live/work + visa deal-breakers.
    # Optional; if absent, no work-auth hard-kill context is added.
    work_auth = pdir / "work_auth.md"
    if work_auth.exists():
        parts.append("# WORK AUTHORIZATION\n\n" + work_auth.read_text(encoding="utf-8"))

    # Market position — feasibility reality + flourishing signals.
    # Optional; if absent, the matcher lacks the negative-instruction layer.
    market_pos = pdir / "market-position.md"
    if market_pos.exists():
        parts.append("# MARKET POSITION (feasibility + flourishing)\n\n" + market_pos.read_text(encoding="utf-8"))

    cv = pdir / "cv-short.md"
    if cv.exists():
        parts.append("# CANDIDATE CV (short)\n\n" + cv.read_text(encoding="utf-8"))

    # Layer 3 — branching future directions with explicit matcher signals.
    # Optional; if absent, the matcher simply has no branching context.
    poss = pdir / "possibilities.md"
    if poss.exists():
        parts.append("# CANDIDATE POSSIBILITIES (branching directions)\n\n" + poss.read_text(encoding="utf-8"))

    if role:
        role_path = pdir / "roles" / f"{role}.md"
        if not role_path.exists():
            available = list((pdir / "roles").glob("*.md")) if (pdir / "roles").is_dir() else []
            names = [p.stem for p in available]
            raise ValueError(
                f"Role '{role}' not found for profile '{profile_name}'. "
                f"Available: {names}"
            )
        parts.append(f"# ROLE: {role}\n\n" + role_path.read_text(encoding="utf-8"))

    return "\n\n---\n\n".join(parts)


def load_bio(profile_name: str) -> str:
    """Load the long bio for a profile (use sparingly — token cost)."""
    pdir = PROFILES_DIR / profile_name.lower()
    bio = pdir / "bio-long.md"
    if not bio.exists():
        return ""
    return bio.read_text(encoding="utf-8")


def list_roles(profile_name: str) -> List[str]:
    """List available role files for a profile."""
    pdir = PROFILES_DIR / profile_name.lower()
    roles_dir = pdir / "roles"
    if not roles_dir.is_dir():
        return []
    return sorted(p.stem for p in roles_dir.glob("*.md"))


# ── Backwards-compat shims (deprecated) ───────────────────────────────────
# These are kept for legacy callers. Prefer load_profile_text(name, role).
# The public engine intentionally ships only profiles/example/, so imports must
# remain usable when LJ/VJ's private profiles are not installed.
def _build_legacy_profile(profile_name: str, role: str) -> str:
    try:
        return load_profile_text(profile_name, role)
    except ValueError:
        if profile_name != "example" and (PROFILES_DIR / "example").is_dir():
            return load_profile_text("example", "general")
        raise


LJ_PROFILE = _build_legacy_profile("lj", "general")
VJ_PROFILE = _build_legacy_profile("vj", "platform-sre")

# ── Research Orgs Source (from juleslogs tweet, 2026-05-27) ──────────────
RESEARCH_ORGS = [
    {"name": "MATS", "url": "https://www.matsprogram.org/", "careers_url": None},
    {"name": "OpenAI Residency", "url": "https://openai.com/residency", "careers_url": "https://openai.com/careers"},
    {"name": "Anthropic Fellows", "url": "https://www.anthropic.com/fellows", "careers_url": "https://www.anthropic.com/careers"},
    {"name": "DeepMind Student Researcher", "url": "https://deepmind.google/", "careers_url": "https://deepmind.google/careers"},
    {"name": "ML Collective", "url": "https://mlcollective.org/", "careers_url": None},
    {"name": "FAR.AI", "url": "https://far.ai/", "careers_url": "https://far.ai/careers/"},
    {"name": "Mila", "url": "https://mila.quebec/", "careers_url": "https://mila.quebec/en/work-at-mila/"},
    {"name": "INSAIT", "url": "https://insait.ai/", "careers_url": "https://insait.ai/careers"},
    {"name": "EleutherAI", "url": "https://www.eleuther.ai/", "careers_url": None},
    {"name": "Redwood Research", "url": "https://www.redwoodresearch.org/", "careers_url": None},
    {"name": "Apart Research", "url": "https://apartresearch.com/", "careers_url": None},
    {"name": "Encode", "url": "https://encodejustice.org/", "careers_url": None},
    {"name": "AI2", "url": "https://allenai.org/", "careers_url": "https://allenai.org/careers"},
    {"name": "LAION", "url": "https://laion.ai/", "careers_url": None},
    {"name": "Berkeley BAIR", "url": "https://bair.berkeley.edu/", "careers_url": None},
    {"name": "Stanford SAIL", "url": "https://ai.stanford.edu/", "careers_url": None},
    {"name": "MIT CSAIL", "url": "https://www.csail.mit.edu/", "careers_url": None},
    {"name": "Vector Institute", "url": "https://vectorinstitute.ai/", "careers_url": "https://vectorinstitute.ai/careers/"},
    {"name": "Hugging Face", "url": "https://huggingface.co/", "careers_url": "https://huggingface.co/jobs"},
]

# ── Scoring thresholds ───────────────────────────────────────────────────
MATCH_THRESHOLD_GO = 7.0
MATCH_THRESHOLD_MAYBE = 4.0

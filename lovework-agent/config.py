"""
Configuration and profile definitions for lovework-agent.

LoveWork (lovework.be) — a personal job discovery agent.
Mission: LoveWork. Work what you love and love your work.
"""

import logging
import os
import socket
from pathlib import Path
from typing import Callable, Dict, List, Optional
from dotenv import load_dotenv

# Load the single canonical .env next to this package (lovework-agent/.env).
# Pinned explicitly so CWD never decides which .env wins — there is exactly
# one, and it must be found regardless of where the process was started.
# **This file is the configuration; the ambient environment is not.**
#
# `load_dotenv` does not override variables that are already set, so a shell that
# exported `LLM_API_KEY` before an edit — a `~/.bashrc` change that no running process
# has re-read — silently wins over the file. LJ, 2026-10-18: *"the change in ~/.bashrc
# may not be reflected in the actual environment live for some time (until bash
# restart). The new valid keys are all `oc_sk_…` while the old invalid keys are
# `sk-…`"* — and OpenCode invalidated the old ones after a security breach, so a stale
# export is a *broken* key, not a preference.
#
# `override=True` makes the file authoritative for this project's own settings. Tests
# are unaffected: they set their environment after import, which still wins.
_env_file = os.getenv("LOVEWORK_ENV_FILE") or (Path(__file__).resolve().parent / ".env")
load_dotenv(Path(_env_file), override=True)

# ── Paths ────────────────────────────────────────────────────────────────
# lovework-agent/ — the package directory
AGENT_ROOT = Path(__file__).resolve().parent
# lovework/ — the parent repo (sibling of lovework-agent/)
# Override with LOVEWORK_ROOT env var for portability across machines.
LOVEWORK_ROOT = Path(os.getenv("LOVEWORK_ROOT", AGENT_ROOT.parent))
# lovework/ — the workspace. It holds the engine checkout (`lovework-engine/`)
# plus, beside it, one repo per principal (`lovework-principal-<p>/`, each with
# its own `profiles/<p>` and `state/<p>`) and the state tree (`lovework-state/`,
# whose `state/<p>` entries are symlinks into those repos). A single-repo host
# has none of them; every fallback below triggers on finding real material,
# never on a name alone.
WORKSPACE_ROOT = LOVEWORK_ROOT.parent
# ~/.lovework/ — user-level config (profiles, custom data sources)
USER_CONFIG_DIR = Path(os.getenv("LOVEWORK_HOME", Path.home() / ".lovework"))

# ── Where state lives ───────────────────────────────────────────────────
#: What marks a directory under `state/` as a **principal**.
#:
#: **A name shape is not a principal** (2026-10-15). The dashboard listed principals
#: by scanning `state/` for names matching `[a-z][a-z0-9_-]*`, so `state/idx/` — the
#: AS1 situation cache that `situation.py` owns (its cursors and its projection) —
#: appeared as a fifth principal called **IDX**, with four links that 404
#: (`/principals/idx/wiki/`). LJ: *"Remove the IDX … IDK where IDX originated from."*
#:
#: `state/` is not a registry of principals; it holds whatever the tools keep
#: there. A principal's directory carries a principal's **material**, and that is
#: the test — not the spelling of its name.
#: Markers are the areas that are a principal's *own* material. `cache`, `logs`
#: and `strata` are deliberately absent: they are generic names a tool could keep
#: under any directory, which is exactly how `idx` looked like a principal.
PRINCIPAL_MARKERS = ("STATE.md", "wiki", "dataset", "applications", "leads")


def is_principal_dir(path: Path) -> bool:
    """True if `path` carries a principal's own material (see PRINCIPAL_MARKERS)."""
    try:
        return (path.is_dir()
                and not path.name.startswith(".")
                and any((path / marker).exists() for marker in PRINCIPAL_MARKERS))
    except OSError:                     # absent, or a directory we may not list
        return False


def has_principal_material(root: Path) -> bool:
    """True if `root` holds at least one principal's material."""
    try:
        return any(is_principal_dir(entry) for entry in root.iterdir())
    except OSError:                     # absent, or a directory we may not list
        return False


def _state_root() -> Path:
    """Resolve the state root, honouring the workspace layout.

    `LOVEWORK_STATE_DIR` is the authority and is what production sets. Without
    it the workspace's `lovework-state/` repo wins when it exists: it is the
    runtime tree, and it owns the areas that are not a principal's — `logs/`,
    `idx/`, `monitor/`, `MANUAL.md` — not only the principal symlinks. The
    engine's own `state/` is the fallback (a single-repo checkout, where it *is*
    the tree), and it is used only when it holds a principal's material: an
    empty `state/` is residue from a command that ran without the env var, and
    letting it shadow the real tree is how a whole dev session comes to read
    "no memory" and "no seals" while the data sits one directory up.
    """
    explicit = os.getenv("LOVEWORK_STATE_DIR")
    if explicit:
        return Path(explicit)
    sibling = WORKSPACE_ROOT / "lovework-state"
    if has_principal_material(sibling):
        return sibling
    # A single-repo checkout, or a fresh clone: `state/` is the tree to use (and
    # the mkdir below makes it). An empty one is not evidence of anything yet.
    return LOVEWORK_ROOT / "state"


# Principal-owned operational data is visible and versioned independently of
# small host-local configuration. LJ is the first migrated principal; new
# principals use the same STATE_DIR/<principal>/ layout from their first run.
STATE_DIR = _state_root()
# Created on first import: state lives outside the code repo, so a fresh
# clone (or a rebuilt state repo) must not fail on a missing directory.
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # read-only checkout, e.g. a docs build or a test fixture
LJ_STATE_DIR = STATE_DIR / "lj"
# These defaults intentionally keep legacy single-principal modules on LJ's
# migrated state. Compatibility symlinks remain at lovework-agent/{cache,wiki,
# dataset} for old scripts and dashboard URLs.
WIKI_ROOT = Path(os.getenv("LOVEWORK_WIKI", LJ_STATE_DIR / "wiki"))
WIKI_ORGS = WIKI_ROOT / "orgs"
WIKI_REPORTS = WIKI_ROOT / "reports"
CACHE_DIR = Path(os.getenv("LOVEWORK_CACHE", LJ_STATE_DIR / "cache"))
DATASET_DIR = Path(os.getenv("LOVEWORK_DATASET", LJ_STATE_DIR / "dataset"))
# OAuth material is deliberately host-local: a refresh on one computer must
# never overwrite a different computer's token through the shared worktree.
# The stable principal source policy supplies only a credential key; the
# runtime adds this host component below.
GMAIL_CREDENTIALS_DIR = Path(
    os.getenv("LOVEWORK_GMAIL_CREDENTIALS_DIR", USER_CONFIG_DIR / "credentials" / "gmail")
)
GMAIL_CREDENTIAL_HOST = os.getenv(
    "LOVEWORK_GMAIL_CREDENTIAL_HOST",
    socket.gethostname().split(".", 1)[0].lower(),
)

for d in (WIKI_ROOT, WIKI_ORGS, WIKI_REPORTS, CACHE_DIR, DATASET_DIR, USER_CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── LLM ──────────────────────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
# Measured faster in tok/s than mimo-v2.6-flash at comparable cost/quality, and its
# temporary price cut became permanent (2026-10-05). Score calibration handles the
# scale difference: calibration.MODEL_OFFSETS["deepseek-v4.1-flash"] = +1.3.
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4.1-flash")


def secret_from_file(path: str | Path) -> str | None:
    """First non-empty line of a credentials file, or None.

    Secrets are files outside the repository (mode 600), never a line in a
    tracked `.env` — the rule the credentials directory exists for.
    """
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        return None
    return None


#: The credential **every** LLM call uses — the crawl, the tick, the monitor and
#: anything added later (LJ, 2026-10-21: *"for all work from now on … use
#: OpenCode Go subscription (label `opencode-go`), use key …"*).
#:
#: Order: an explicit env override, then the credential file, then the historical
#: env/.env variables. The file wins over `.env` on purpose: `.env` is tracked in
#: git, so a key rotation must never need a commit, and it must not be possible
#: for a stale shell export to shadow the current key (the 2026-09-10 trap).
LLM_KEY_FILE = Path(
    os.getenv("LOVEWORK_LLM_KEY_FILE",
              USER_CONFIG_DIR / "credentials" / "opencode-go.key")
)
_OPENCODE_KEY = (
    os.getenv("LOVEWORK_LLM_API_KEY")
    or secret_from_file(LLM_KEY_FILE)
    or os.getenv("OPENCODE_GO_API_KEY")
    # legacy names, kept so an older env still resolves (2026-11-03)
    or os.getenv("OPENCODE_GO_EQUAL_API_KEY")
    or os.getenv("OPENCODE_GO_EQUAL_LJ_API_KEY")
    or os.getenv("OPENCODE_GO_LJ_API_KEY")
)
LLM_API_KEY = (
    _OPENCODE_KEY
    or os.getenv("LLM_API_KEY")
    or os.getenv("OPENROUTER_API_KEY")
    or os.getenv("DEEPSEEK_API_KEY")
    or os.getenv("OPENAI_API_KEY")
)
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL",
    "https://opencode.ai/zen/go/v1" if _OPENCODE_KEY
    else "https://openrouter.ai/api/v1" if os.getenv("OPENROUTER_API_KEY")
    else "https://api.deepseek.com/v1",
)
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# Generous on purpose: this is a ceiling, not a target — the model stops when it is
# done (measured 2026-10-05: at max_tokens=131072 a one-line summary still stopped at
# ~1.8K tokens). These call sites are a handful of turns per advert, not an agentic
# loop, so starving the cap buys nothing and risks a truncated answer.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "131072"))
# Thinking at max (LJ, 2026-10-21): every call asks the relay for
# `reasoning_effort: "max"` — the relay's own vocabulary, passed through as-is
# (probe 2026-10-21: 200 OK, `completion_tokens_details.reasoning_tokens` > 0).
# Dial back per run with LLM_REASONING_EFFORT=high|medium|low if latency bites.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "max")


# ── Per-principal LLM settings (2026-10-21) ──────────────────────────────
# LJ: *"We need to have per-principal setting for this. We need to think about a
# case where a principal is running lovework on their own computer. So this needs
# to be a per-principal setting, not a global setting."*
#
# `profiles/<p>/llm.yaml` carries the **choices** (tracked, no secrets); the
# credential stays a file whose path the profile names (`key_file`) or an env
# var **name** (`api_key_env`). Precedence, highest first:
#
#   1. host env overrides — `LOVEWORK_LLM_PROVIDER` / `_BASE_URL` / `_MODEL` /
#      `_REASONING_EFFORT` / `_API_KEY` / `_TEMPERATURE` / `_MAX_TOKENS`
#      (an operator forcing one model on a host, or a one-off test)
#   2. `profiles/<p>/llm.yaml`
#   3. the global defaults above (`lovework-agent/.env` + the credential file)
#
# A caller with no principal (`--profile all`, the monitor's loop-wide triage)
# uses (1)/(3). `set_active_profile()` binds a process to one principal so every
# `LLMClient()` it constructs follows the same profile without threading a
# parameter through twenty call sites.
_ACTIVE_PROFILE: Optional[str] = None


def active_profile() -> Optional[str]:
    """The principal this process is working for, if it has said so."""
    return _ACTIVE_PROFILE


def set_active_profile(name: Optional[str]) -> None:
    """Bind the process to one principal (None clears it)."""
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = str(name) if name and name != "all" else None


class LLMSettings:
    """One resolved LLM configuration: what to call, with which credential, and why.

    `source` says which layer won — `"env"`, `"profile"` or `"global"` — so a log
    line can tell a reader where the setting came from (docs/49).
    """

    __slots__ = ("principal", "provider", "base_url", "model", "reasoning_effort",
                 "api_key", "temperature", "max_tokens", "source", "detail")

    def __init__(self, *, principal, provider, base_url, model, reasoning_effort,
                 api_key, temperature, max_tokens, source, detail):
        self.principal = principal
        self.provider = provider
        self.base_url = base_url
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.source = source
        self.detail = detail

    def client_kwargs(self) -> dict:
        """The keyword arguments `llm_client.LLMClient` takes."""
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "provider": self.provider,
        }

    def describe(self) -> str:
        """One line for a log: provider/model, effort, and where it came from."""
        key_from = ("env" if self.detail.get("api_key") == "env"
                    else self.detail.get("api_key") or "none")
        return (f"{self.provider}/{self.model} · effort {self.reasoning_effort or 'default'}"
                f" · {self.source}"
                + (f" ({self.principal})" if self.principal else "")
                + f" · key from {key_from}")


def profile_llm_file(principal: Optional[str]) -> Optional[Path]:
    """`profiles/<p>/llm.yaml`, if the principal has one."""
    if not principal:
        return None
    path = profile_dir(principal) / "llm.yaml"
    return path if path.is_file() else None


def _read_profile_llm(principal: Optional[str]) -> dict:
    path = profile_llm_file(principal)
    if path is None:
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # a malformed profile must not stop a crawl
        logging.getLogger(__name__).warning("Ignoring unreadable %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def llm_settings(principal: Optional[str] = None) -> LLMSettings:
    """Resolve the LLM configuration for `principal` (default: this process's).

    Reads once per call — cheap (a small YAML file), and it means a principal can
    edit their `llm.yaml` and have the next tick honour it without a restart.
    """
    principal = principal or _ACTIVE_PROFILE
    profile = _read_profile_llm(principal)
    detail: dict = {}

    def pick(field: str, env_name: str, default, cast=None):
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            detail[field] = "env"
            return (cast(raw) if cast else raw)
        if profile.get(field) not in (None, ""):
            detail[field] = "profile"
            return profile[field]
        detail[field] = "global"
        return default

    provider = str(pick("provider", "LOVEWORK_LLM_PROVIDER", LLM_PROVIDER))
    base_url = str(pick("base_url", "LOVEWORK_LLM_BASE_URL", LLM_BASE_URL))
    model = str(pick("model", "LOVEWORK_LLM_MODEL", LLM_MODEL))
    effort = pick("reasoning_effort", "LOVEWORK_LLM_REASONING_EFFORT",
                  LLM_REASONING_EFFORT)
    effort = str(effort) if effort else None
    temperature = pick("temperature", "LOVEWORK_LLM_TEMPERATURE", LLM_TEMPERATURE, float)
    max_tokens = pick("max_tokens", "LOVEWORK_LLM_MAX_TOKENS", LLM_MAX_TOKENS, int)

    # The credential: an explicit override, then the profile's named env var, then
    # the profile's key file, then the global resolution (which already prefers the
    # shared credential file over `.env`). A profile never *contains* a secret.
    key, key_from = None, None
    if os.getenv("LOVEWORK_LLM_API_KEY"):
        key, key_from = os.getenv("LOVEWORK_LLM_API_KEY"), "env"
    elif profile.get("api_key_env"):
        named = str(profile["api_key_env"])
        if os.getenv(named):
            key, key_from = os.getenv(named), f"env {named}"
    if key is None and profile.get("key_file"):
        key = secret_from_file(Path(str(profile["key_file"])).expanduser())
        key_from = f"file {profile['key_file']}" if key else None
    if key is None:
        key, key_from = LLM_API_KEY, "global"
    detail["api_key"] = key_from or "none"

    # The *highest* layer any field came from — an operator's env override is the
    # most specific statement in play, so it names the source even when the rest of
    # the configuration still comes from the profile.
    sources = {detail.get(field) for field in
               ("provider", "base_url", "model", "reasoning_effort")}
    source = ("env" if "env" in sources
              else "profile" if "profile" in sources else "global")

    return LLMSettings(
        principal=principal, provider=provider, base_url=base_url, model=model,
        reasoning_effort=effort, api_key=key, temperature=temperature,
        max_tokens=max_tokens, source=source, detail=detail)

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
#   soul.md         — what the principal is, wants, avoids (always loaded)
#   cv-short.md     — Layer 2 (tip): current highest-SNR short CV
#   bio-long.md     — Layer 1 (long path): full past→present CV (loaded by load_bio())
#   possibilities.md — Layer 3 (branching): future directions with matcher signals
#   roles/<role>.md — role-specific criteria (one or more, one selected per run)

PROFILES_DIR_PRINCIPALS = [
    USER_CONFIG_DIR / "profiles",
    LOVEWORK_ROOT / "profiles",
    AGENT_ROOT / "profiles",
]


def get_profiles_dir() -> Path:
    """Return the first existing profiles dir, or default to user config.

    An **unreadable** candidate is not a candidate (2026-11-04): `is_dir()` raises
    `PermissionError`, not `False`, when a parent is closed to us — `LOVEWORK_HOME`
    points at the deployment's credentials directory, which belongs to the runtime
    user, so a developer (or a test run) importing `config` as anyone else died at
    import time rather than falling through to the repo's own profiles.
    """
    for p in PROFILES_DIR_PRINCIPALS:
        try:
            if p.is_dir():
                return p
        except OSError as exc:          # permission denied is "not here", not fatal
            logging.getLogger(__name__).debug("profiles candidate %s unreadable: %s", p, exc)
    return PROFILES_DIR_PRINCIPALS[0]  # default to user config

PROFILES_DIR = get_profiles_dir()
#: The root `config` computed for itself. A caller that assigns `PROFILES_DIR`
#: takes over the question: `profile_dir()` then never falls back (see below).
_DEFAULT_PROFILES_DIR = PROFILES_DIR


def principal_repo(principal: str) -> Path:
    """The workspace checkout of one principal's repo (`lovework-principal-<p>`)."""
    return WORKSPACE_ROOT / f"lovework-principal-{str(principal or '').lower()}"


def profile_dir(principal: str) -> Path:
    """Resolve one principal's profile directory.

    `PROFILES_DIR/<principal>` answers it while a single profiles root exists
    (production: `$LOVEWORK_HOME/profiles`, which holds every principal). In the
    workspace layout each principal's profile lives in that principal's own repo,
    `lovework-principal-<p>/profiles/<p>`, so that is the fallback.

    The fallback applies only to the root `config` computed for itself: a
    `PROFILES_DIR` that a caller assigned explicitly is taken literally, so a
    test (or an operator) pointing the root at a fixture never silently reads a
    real principal's profile.
    """
    name = str(principal or "").lower()
    candidate = PROFILES_DIR / name
    try:
        if candidate.is_dir():
            return candidate
        if PROFILES_DIR.resolve() != _DEFAULT_PROFILES_DIR.resolve():
            return candidate
    except OSError:                     # unreadable candidate is "not here"
        return candidate
    in_repo = principal_repo(name) / "profiles" / name
    try:
        return in_repo if in_repo.is_dir() else candidate
    except OSError:
        return candidate


def profile_dirs() -> Dict[str, Path]:
    """name → profile directory, for every principal resolvable on this host.

    The union of the subdirectories of `PROFILES_DIR` (single-root layout) and the
    principal repos beside the engine (workspace layout). A name present in both
    keeps the `PROFILES_DIR` entry: the configured root is the authority.
    """
    found: Dict[str, Path] = {}
    try:
        if PROFILES_DIR.is_dir():
            for entry in sorted(PROFILES_DIR.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    found[entry.name] = entry
    except OSError:
        pass
    try:
        repos = sorted(WORKSPACE_ROOT.glob("lovework-principal-*"))
    except OSError:
        repos = []
    prefix = "lovework-principal-"
    for repo in repos:
        name = repo.name[len(prefix):]
        if not name or name.startswith("."):
            continue
        try:
            in_repo = repo / "profiles" / name
            if in_repo.is_dir():
                found.setdefault(name, in_repo)
        except OSError:
            continue
    return found


# ── Who is a principal ──────────────────────────────────────────────────
# `PRINCIPAL_MARKERS` — the material that marks a principal — is defined beside
# `STATE_DIR` above, because the state-root fallback asks the same question.

#: The order principals are shown in — a **decision**, not the alphabet.
#:
#: LJ, 2026-10-15: *"What order are the principals listed in - how is that decided?
#: I would prefer the ordering to be LJ VJ KJ. LJ = patient#0, VJ = customer#1,
#: KJ = …"* It was `sorted()`, so the page read **kj, lj, vj** — an accident of
#: spelling that put the third customer first.
#:
#: The order is the principals' own history: LJ is patient#0 (the developer and
#: first subject), VJ the first customer, then KJ, then PK. It is written here once
#: so every surface that lists principals agrees — and a principal the list has
#: never heard of sorts **after** the known ones, alphabetically, rather than
#: disappearing or landing in front.
#: The order principals are worked in — **LJ's priority ruling, 2026-10-19**:
#: *"So far the ordering was 1-lj 2-vj 3-kj. New priority order 1-vj 2-kj 3-lj."*
#: It is read by the loop's round-robin, the pack queues and the dashboard, so the
#: ruling lives in one place rather than in each caller's own tuple.
PRINCIPAL_ORDER = ("vj", "kj", "lj", "pk")


def principal_names(state_dir: Optional[Path] = None) -> List[str]:
    """The principals with state on this host — one authority for every scanner.

    Used by the dashboard's index, `pack_queue` and `history_inventory`; the
    scanners that need a *specific* area (`dataset/memory`) ask for that area
    directly, which is a different question and a narrower one.
    """
    root = state_dir or STATE_DIR
    if not root.is_dir():
        return []
    found = [entry.name for entry in root.iterdir() if is_principal_dir(entry)]
    return sorted(found, key=principal_sort_key)


def principal_sort_key(name: str) -> tuple:
    """Sort key for the principals: the declared order first, then the alphabet."""
    known = {principal: index for index, principal in enumerate(PRINCIPAL_ORDER)}
    return (known.get(name, len(known)), name)


# ── External data sources (configurable paths) ──────────────────────────
# The user can point these at any directory via env var.
#
# APPLICATIONS_DIR is **LJ's** external manual corpus (the pre-LoveWork cases he
# wrote himself): the parent repo's applications/ directory, mirrored from
# ~/LJ-work-2026/applications. It is NOT a global default for the runtime —
# the corpus is per-principal profile data (2026-10-27), resolved by
# `manual_root(principal)` from `LOVEWORK_MANUAL_CASES_<PRINCIPAL>`, with this
# constant as LJ's legacy registry entry. Do not read it directly from new
# code; ask `manual_root(principal)`.
#
# (~/LJ-work-2026/applications), not LOVEWORK_ROOT/applications
# (repo moved out of ~/Documents 2026-09-20: iCloud+iDrive 5 GB quotas).
# This is the unified view: 181 legacy real dirs (pre-LoveWork, RW there)
# plus one read-only symlink per lovework/state/<principal>/applications/
# *-LoveWork* pack (RW in lovework's repo, surfaced read-only via the
# symlink here — ownership follows the workflow that created the case).
# history.py and the matcher's cooldown logic scan this dir to find prior
# applications across both sets. Fixed 2026-07-30 after the lovework/
# applications symlink was removed during the state restructure — without
# this, scan_applications() silently returned [] and the cooldown logic
# stopped firing.
APPLICATIONS_DIR = Path(
    os.getenv("LOVEWORK_APPLICATIONS_DIR", LOVEWORK_ROOT.parent / "applications")
)

# ── A principal's *manual* application corpus ────────────────────────────
# LJ, 2026-10-20: *"The case histories of our manual applications should be
# something Lovework should absolutely learn from! So for every user we need to
# have an extra directory for 'non-Lovework applications' that lovework should
# index, search, use, gather knowledge etc. For principal LJ that should be
# ~/LJ-work-2026. For principals vj and kj that is currently empty, does not
# exist. But it will in due time."*
#
# The corpus is the root of the `YYYYMMDD-Company-Role/` cases the principal
# wrote himself — the parent repo's `applications/` for LJ, and (later) a
# directory of their own for anyone else. It is **not** LoveWork's pack tree
# (`state/<p>/applications/*-LoveWork`): that is LoveWork's own output, and the
# two are told apart in `manual_projects.read_case`.
#
# Each value is a **callable**, so the path is resolved when it is asked for.
# LJ's entry has to follow `APPLICATIONS_DIR` — an env override, a relocated
# repo, a test that rebinds the constant — never a copy taken at import time.
MANUAL_CASES_ENV_PREFIX = "LOVEWORK_MANUAL_CASES_"


def _manual_cases_from_env(principal: str) -> Optional[Path]:
    """`LOVEWORK_MANUAL_CASES_<PRINCIPAL>`, or None when unset.

    The external manual corpus is **per-principal profile data**, so its
    override is per principal: `LOVEWORK_MANUAL_CASES_LJ`,
    `LOVEWORK_MANUAL_CASES_P3`, … A new principal therefore needs no code
    change to get a corpus — just the env var and a directory it owns.
    """
    raw = os.getenv(f"{MANUAL_CASES_ENV_PREFIX}{(principal or '').upper()}", "").strip()
    return Path(raw) if raw else None


MANUAL_CORPUS_ROOTS: dict[str, Callable[[], Path]] = {
    # LJ's corpus is the legacy `APPLICATIONS_DIR`; every other principal is
    # either declared here or (preferred) supplied by its own env var above.
    "lj": lambda: APPLICATIONS_DIR,
    # vj, kj: no manual corpus exists yet, so `manual_root` returns None and
    # every reader answers "none" instead of failing or inventing a directory.
    # Adding one is one line here (or one env var) and nothing else anywhere.
}


def manual_root(principal: str) -> Optional[Path]:
    """The principal's manual-application corpus root, or None when it has none.

    One question, one answer, **per principal** (2026-10-27: the corpus is
    profile data, not a property of the LoveWork runtime — one shared root
    cannot serve p3/p2 later, and it made every principal's "have I
    applied to this org before?" scan LJ's history).

    Resolution order: the principal's own env var, then the registry, then
    None. A principal without a corpus (vj and kj today) is not an error and
    not an empty directory invented on their behalf — it is None, and the CLI
    says so instead of printing an empty list that looks like a corpus with
    nothing in it.
    """
    name = (principal or "").strip().lower()
    from_env = _manual_cases_from_env(name)
    if from_env is not None:
        return from_env
    resolver = MANUAL_CORPUS_ROOTS.get(name)
    return Path(resolver()) if resolver is not None else None


HF_TRACKER_DIR = Path(
    os.getenv("LOVEWORK_HF_TRACKER_DIR", LOVEWORK_ROOT / "AI-for-HF-startup-tracker")
)
NEOLAB_TRACKER = Path(
    os.getenv("LOVEWORK_NEOLAB_TRACKER", LOVEWORK_ROOT / "neolab-and-emerging-ai-lab-tracker.txt")
)
NEOCLOUD_TRACKER = Path(
    os.getenv("LOVEWORK_NEOCLOUD_TRACKER", LOVEWORK_ROOT / "neocloud-and-compute-tracker.txt")
)
# Speech / voice-AI / speech-agent companies (repo-owned). Same line format as
# the neolab and neocloud trackers; crawled by sources/speech_companies.py.
CHEMINFO_TRACKER = Path(
    os.getenv("LOVEWORK_CHEMINFO_TRACKER", LOVEWORK_ROOT / "cheminfo-companies-tracker.txt")
)
SPEECH_TRACKER = Path(
    os.getenv("LOVEWORK_SPEECH_TRACKER", LOVEWORK_ROOT / "speech-and-voice-ai-companies-tracker.txt")
)


# ── Pinned inputs (docs/28 WP1b box ③) ───────────────────────────────────
# Inside a writer operation the input bytes that scored a run are pinned at run
# start (crawl_operation pins them; run_context refuses a live fallback for
# them). The operation exports the pinned tree here so *loaders* read those
# bytes rather than the live files — otherwise an edit made mid-run would mean
# the manifest's "these were the effective inputs" claim is false.
# Unset (direct runs, tests, MCP) = live files, exactly as before.
PINNED_INPUTS_ENV = "LOVEWORK_PINNED_INPUTS"

_pinned_logger = logging.getLogger("lovework.config")


def principal_from_argv(argv: list[str], default: str = "lj") -> str:
    """The principal named on a command line (`--profile X` / `--profile=X`).

    Used by entry points that must take a writer lease *before* they parse
    arguments (docs/28 §WP1b box ①), so they cannot rely on argparse.
    """
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
    return default


def pinned_inputs_dir() -> Optional[Path]:
    """The operation's pinned-input tree, or None when not inside an operation."""
    raw = os.getenv(PINNED_INPUTS_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def pinned_input_text(kind: str, name: str, *, warn: bool = True) -> Optional[str]:
    """Text of a pinned input, or None when it is not in the pinned tree.

    ``warn`` is for the caller that knows whether the live counterpart exists:
    a file the operation never had cannot be a gap, but a file that exists live
    and is missing from the pins means the bundle overstates what this run read.
    """
    root = pinned_inputs_dir()
    if root is None:
        return None
    candidate = root / kind / name
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    if warn:
        _pinned_logger.warning(
            "pinned input missing: %s/%s (using the live file; the sealed "
            "bundle will not contain the bytes this run read)", kind, name)
    return None


def _profile_file(pdir: Path, name: str) -> Optional[str]:
    """Pinned bytes first (inside an operation), else the live profile file.

    Warns only when a file that *does* exist live was not pinned — that is a
    real gap, while an absent optional file is simply absent.
    """
    pinned = pinned_input_text("file", name, warn=False)
    if pinned is not None:
        return pinned
    path = pdir / name
    if not path.exists():
        return None
    if pinned_inputs_dir() is not None:
        _pinned_logger.warning(
            "pinned input missing: file/%s (using the live file; the sealed "
            "bundle will not contain the bytes this run read)", name)
    return path.read_text(encoding="utf-8")


def load_profile_text(profile_name: str, role: Optional[str] = None) -> str:
    """Load a principal profile as a single combined string for the matcher.

    Combines: soul + work-auth + market-position + cv-short + possibilities + (optional role).
    Long bio (Layer 1) is available via load_bio() if needed for a specific call
    (kept out of the default load to control token cost).
    """
    pdir = profile_dir(profile_name)
    if not pdir.is_dir():
        raise ValueError(f"Profile not found: {profile_name} (looked in {pdir})")

    parts = []

    soul = _profile_file(pdir, "soul.md")
    if soul is not None:
        parts.append("# PRINCIPAL SOUL\n\n" + soul)

    # Work authorization — where the principal may live/work + visa deal-breakers.
    # Optional; if absent, no work-auth hard-kill context is added.
    work_auth = _profile_file(pdir, "work_auth.md")
    if work_auth is not None:
        parts.append("# WORK AUTHORIZATION\n\n" + work_auth)

    # Market position — feasibility reality + flourishing signals.
    # Optional; if absent, the matcher lacks the negative-instruction layer.
    market_pos = _profile_file(pdir, "market-position.md")
    if market_pos is not None:
        parts.append("# MARKET POSITION (feasibility + flourishing)\n\n" + market_pos)

    cv = _profile_file(pdir, "cv-short.md")
    if cv is not None:
        parts.append("# PRINCIPAL CV (short)\n\n" + cv)

    # Layer 3 — branching future directions with explicit matcher signals.
    # Optional; if absent, the matcher simply has no branching context.
    poss = _profile_file(pdir, "possibilities.md")
    if poss is not None:
        parts.append("# PRINCIPAL POSSIBILITIES (branching directions)\n\n" + poss)

    if role:
        pinned_role = pinned_input_text("role", role, warn=False)
        if pinned_role is not None:
            parts.append(f"# ROLE: {role}\n\n" + pinned_role)
        else:
            role_path = pdir / "roles" / f"{role}.md"
            if not role_path.exists():
                available = list((pdir / "roles").glob("*.md")) if (pdir / "roles").is_dir() else []
                names = [p.stem for p in available]
                raise ValueError(
                    f"Role '{role}' not found for profile '{profile_name}'. "
                    f"Available: {names}"
                )
            if pinned_inputs_dir() is not None:
                _pinned_logger.warning(
                    "pinned input missing: role/%s (using the live file; the "
                    "sealed bundle will not contain the bytes this run read)", role)
            parts.append(f"# ROLE: {role}\n\n" + role_path.read_text(encoding="utf-8"))

    return "\n\n---\n\n".join(parts)


def load_bio(profile_name: str) -> str:
    """Load the long bio for a profile (use sparingly — token cost)."""
    pdir = profile_dir(profile_name)
    pinned = _profile_file(pdir, "bio-long.md")
    return pinned or ""


def list_roles(profile_name: str) -> List[str]:
    """List available role files for a profile."""
    pdir = profile_dir(profile_name)
    roles_dir = pdir / "roles"
    if not roles_dir.is_dir():
        return []
    return sorted(p.stem for p in roles_dir.glob("*.md"))


# The old `LJ_PROFILE` / `VJ_PROFILE` module constants were removed 2026-09-20:
# nothing in the repo (or the homelab scripts) consumed them, and they read two
# profiles at import time — which, inside a writer operation, made them report
# pin gaps for a principal that was not even the run's (docs/28 WP1b box ③).
# Callers use `load_profile_text(name, role)`.

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
    # Charity / philanthropy tech-contract category (added 2026-09-25; LJ track 2 - fixed 1-2yr technical contracts)
    {"name": "Renaissance Philanthropy", "url": "https://www.renaissancephilanthropy.org/", "careers_url": "https://www.renaissancephilanthropy.org/careers"},
    {"name": "ARIA", "url": "https://www.aria.org.uk/", "careers_url": "https://aria.pinpointhq.com/postings"},
    {"name": "Cato Institute", "url": "https://www.cato.org/", "careers_url": "https://www.cato.org/careers"},
    {"name": "RAND", "url": "https://www.rand.org/", "careers_url": "https://rand.wd5.myworkdayjobs.com/en-US/External_Career_Site"},
    {"name": "Alan Turing Institute", "url": "https://www.turing.ac.uk/", "careers_url": "https://www.turing.ac.uk/work-turing"},
    {"name": "Wellcome", "url": "https://wellcome.org/", "careers_url": "https://wellcome.org/who-we-are/jobs"},
    {"name": "Tony Blair Institute", "url": "https://institute.global/", "careers_url": "https://institute.global/careers"},
    {"name": "Nesta", "url": "https://www.nesta.org.uk/", "careers_url": "https://www.nesta.org.uk/about-us/jobs/"},
    {"name": "Chan Zuckerberg Initiative", "url": "https://chanzuckerberg.com/", "careers_url": "https://chanzuckerberg.com/careers/"},
    {"name": "Schmidt Sciences", "url": "https://www.schmidtsciences.org/", "careers_url": None},
    {"name": "Mozilla", "url": "https://foundation.mozilla.org/", "careers_url": "https://foundation.mozilla.org/en/careers/"},
    {"name": "OpenAI Foundation", "url": "https://openaifoundation.org/", "careers_url": "https://openaifoundation.org/careers"},
    {"name": "Templeton World Charity Foundation", "url": "https://www.templetonworldcharity.org/", "careers_url": None},
    {"name": "Ada Lovelace Institute", "url": "https://www.adalovelaceinstitute.org/", "careers_url": "https://www.adalovelaceinstitute.org/work-with-us/"},
    {"name": "SPRIND", "url": "https://www.sprind.org/en/", "careers_url": "https://www.sprind.org/en/career"},
    {"name": "Francis Crick Institute", "url": "https://www.crick.ac.uk/", "careers_url": "https://www.crick.ac.uk/careers-student-life/vacancies"},
    {"name": "EMBL-EBI", "url": "https://www.ebi.ac.uk/", "careers_url": "https://www.ebi.ac.uk/about/jobs"},
    {"name": "Hugging Face", "url": "https://huggingface.co/", "careers_url": "https://huggingface.co/jobs"},
]

# ── Scoring thresholds ───────────────────────────────────────────────────
MATCH_THRESHOLD_GO = 7.0
MATCH_THRESHOLD_MAYBE = 4.0

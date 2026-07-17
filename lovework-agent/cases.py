"""
Lead → case terminology + slug convention.

A "lead" is a job found during the initial sweep — the matcher has scored
it, the registry has it, the wiki reports it. A "case" is a lead LJ has
decided to pursue: a tailored CV has been written, a cover letter drafted,
emails exchanged, status updates made.

This module owns the transition:

  - `slug_for(when, org, role)` returns a YYYYMMDD-Company-Role slug
    matching LJ's `applications/` convention (already used for the
    application directories). The slug is the case's identity.
  - `make_case_dir(...)` creates the case directory under
    `applications/` (or a configurable cases root) and seeds it with a
    README. Idempotent — re-running updates the README in place.
  - `is_case_open(slug, cases_root)` and `close_case(slug, ...)` are
    the lifecycle helpers; LJ will wire these into the agent REPL.

The slug format is intentionally short, humanly readable, and
filename-safe. Example:

  slug = slug_for(date(2026, 6, 23), "Anthropic", "AI Research Engineer")
  # → "20260623-Anthropic-AI_Research_Engineer"

A case dir holds:
  - README.md            — case status, links, timeline
  - cvlj-tailored.md     — the tailored CV (LJ's existing convention)
  - cover-letter.md      — optional
  - notes.md             — interaction log

Nothing destructive: if the case dir already exists, README is rewritten
in place; other files are never touched. The agent REPL is expected to
write the rest.
"""

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Same parent as `applications/`. Defaults to LOVEWORK_ROOT/applications
# which is the same dir LJ uses for the existing application directories.
DEFAULT_CASES_ROOT = config.LOVEWORK_ROOT / "applications"

# Slug regex (used to recognise existing case dirs and for input validation).
SLUG_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<org>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<role>[A-Za-z0-9._-]+)$"
)

# Filename-safe character set: alphanumerics, dash, underscore, dot. Spaces
# become underscores; everything else (slashes, ampersands, parentheses) is
# stripped. This matches what people would type at a shell prompt.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify_part(s: str, *, max_len: int = 50) -> str:
    s = (s or "").strip()
    # Drop common noise.
    s = re.sub(r"\s+", "_", s)
    s = _UNSAFE_RE.sub("", s)
    # Collapse runs of underscores introduced by adjacent unsafe chars.
    s = re.sub(r"_+", "_", s)
    s = s.strip("._-")
    if not s:
        s = "x"
    return s[:max_len]


def slug_for(when: date, org: str, role: str) -> str:
    """Build a YYYYMMDD-Company-Role slug for a new case.

    Args:
        when: the date the case was created (use date.today() for new ones).
        org:  the company name as it should appear (e.g. "Anthropic").
        role: the role title (e.g. "AI Research Engineer").

    Returns:
        A slug of the form YYYYMMDD-Company-Role. Safe for shell/filename use.
    """
    if not isinstance(when, date):
        raise TypeError(f"when must be a datetime.date, got {type(when).__name__}")
    org_part = _slugify_part(org, max_len=40)
    role_part = _slugify_part(role, max_len=60)
    return f"{when.strftime('%Y%m%d')}-{org_part}-{role_part}"


def parse_slug(slug: str) -> Optional[dict]:
    """Reverse of slug_for: extract {date, org, role} from a slug string.

    Returns None if the slug doesn't match the canonical format.
    """
    if not slug:
        return None
    m = SLUG_RE.match(slug)
    if not m:
        return None
    return {
        "date": m.group("date"),
        "org": m.group("org"),
        "role": m.group("role"),
    }


def is_case_open(slug: str, cases_root: Optional[Path] = None) -> bool:
    """True if a case dir exists for this slug (treated as 'open')."""
    root = cases_root or DEFAULT_CASES_ROOT
    return (root / slug).is_dir()


def case_dir(slug: str, cases_root: Optional[Path] = None) -> Path:
    """Return the case directory path (does not create it)."""
    root = cases_root or DEFAULT_CASES_ROOT
    return root / slug


def make_case_dir(
    slug: str,
    *,
    title: str = "",
    url: str = "",
    source: str = "",
    score: Optional[float] = None,
    decision: str = "",
    reasoning: str = "",
    cases_root: Optional[Path] = None,
) -> Path:
    """Create a case dir for `slug` and seed it with a README.

    Idempotent: if the dir already exists, the README is rewritten in
    place (preserving any user-added sections is out of scope — the
    README is a generated block that we control). All other files
    (CV, cover letter, notes) are left untouched.

    Returns the case dir path.
    """
    parsed = parse_slug(slug)
    if parsed is None:
        raise ValueError(f"Invalid case slug: {slug!r}")

    root = cases_root or DEFAULT_CASES_ROOT
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)

    readme = d / "README.md"
    body = [
        f"# Case — {slug}",
        "",
        f"- **Created:** {parsed['date']}",
        f"- **Org:** {parsed['org']}",
        f"- **Role:** {parsed['role'].replace('_', ' ')}",
    ]
    if title:
        body.append(f"- **Listing title:** {title}")
    if url:
        body.append(f"- **Listing URL:** <{url}>")
    if source:
        body.append(f"- **Source:** {source}")
    if score is not None:
        body.append(f"- **Initial score:** {score}/10")
    if decision:
        body.append(f"- **Initial decision:** {decision}")
    if reasoning:
        body.append(f"- **Initial reasoning:** {reasoning}")
    body += [
        "",
        "## Status",
        "",
        "- [ ] Tailored CV written",
        "- [ ] Cover letter written",
        "- [ ] Submitted",
        "- [ ] Phone / screening",
        "- [ ] Technical interview",
        "- [ ] Final / offer",
        "- [ ] Outcome (rejected / withdrawn / accepted)",
        "",
        "## Timeline",
        "",
        f"- {parsed['date']}: case opened from LoveWork lead.",
        "",
        "## Notes",
        "",
        "_(free-form — interactions, follow-ups, decisions)_",
        "",
    ]
    readme.write_text("\n".join(body), encoding="utf-8")
    logger.info(f"Created case dir: {d}")
    return d


def case_status(slug: str, cases_root: Optional[Path] = None) -> str:
    """Return the case's status by inspecting its README.

    Possible return values:
      - "none"     — no case dir for this slug
      - "open"     — case dir exists, no [x] in the Status section
      - "submitted"— at least the Submitted checkbox is checked
      - "closed"   — the Outcome line has been filled in
    """
    d = case_dir(slug, cases_root=cases_root)
    if not d.is_dir():
        return "none"
    readme = d / "README.md"
    if not readme.exists():
        return "open"
    text = readme.read_text(encoding="utf-8")
    # Outcome is filled in if the line is no longer a blank checkbox.
    if re.search(r"^- \[[xX]\] Outcome[^\n]*\S", text, re.MULTILINE):
        return "closed"
    if re.search(r"^- \[[xX]\] Submitted", text, re.MULTILINE):
        return "submitted"
    return "open"

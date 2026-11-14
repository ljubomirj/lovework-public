"""
Local wiki storage for crawl results.

Format: Markdown files under wiki/.
- wiki/reports/YYYY-MM-DD-report.md — per-run reports
- wiki/orgs/<org-name>.md — per-organization history
- wiki/index.md — master index of all findings
"""

import json
import logging
import re
import os
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional

from filename_limits import clip_stem

import config
from notify import human_slug
from report_header import build_header, build_trailer, section_heading

logger = logging.getLogger(__name__)


#: A block on an org page starts at its "### " heading.
_BLOCK_HEAD_RE = re.compile(r"(?m)^### ")
#: The advert's link line inside a block (the identity carrier).
_BLOCK_URL_RE = re.compile(r"(?m)^- \*\*URL\*\*: (\S+)")
#: The heading's leading ISO date, when the block was written in the
#: canonical org-page shape ("### 2026-09-06 — Title").
_BLOCK_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")


#: The one spelling of "this entry has no usable advert link", used by every
#: writer. Two spellings grew (`_not available_` and `_not_available_`) and three
#: readers each tolerated a different set — `link_check` and `notify` even
#: accept the *broken* value `_not`, which is what a parser produced when it
#: split the spaced spelling on whitespace and kept the first token. The email
#: printed `_not` and offered no link (2026-09-15), `wiki_backfill` re-created
#: it on 21 pages (2026-10-08). One constant, one predicate, and readers stay
#: tolerant of the historical spellings because old pages carry them.
NO_URL_PLACEHOLDER = "_not available_"
_NO_URL_VALUES = frozenset({"", "_not", "_not available_", "_not_available_"})


def is_no_url_placeholder(value: str) -> bool:
    """True when a URL field carries no usable link (any historical spelling)."""
    return (value or "").strip().lower() in _NO_URL_VALUES


def _unescape(text: str) -> str:
    """Decode HTML entities once, at the boundary where text enters the wiki.

    Fetched pages and digests arrive HTML-escaped; storing them escaped leaks
    the escaping into the wiki, the index, the report and (worst) into a
    filename — "St._Jude_Children_x27_s_Research_Hospital" is a page named
    after `&#x27;`, and an advert URL containing `&apos;` is a dead link
    (2026-10-07 audit). One decode here is cheaper than a rule per writer.
    """
    if not text or "&" not in text:
        return text
    import html
    try:
        return html.unescape(text)
    except Exception:      # pragma: no cover - html.unescape is total
        return text


#: A canonical block states either a verdict or that it is a note; anything
#: else is a block written by a foreign writer (the audit's
#: `foreign_block_schema` class).
BLOCK_KIND_RE = re.compile(r"(?m)^- \*\*(?:Decision|Note)\*\*: (\S.*)$")


def is_canonical_block(block: str) -> bool:
    """True when a block carries a `**Decision**` or `**Note**` line."""
    return bool(BLOCK_KIND_RE.search(block))


def _norm_key(name: str) -> str:
    """The page-MINTING identity fold (alnum-only), from its one home."""
    try:
        from company_events import norm_key

        return norm_key(name)
    except Exception:                     # pragma: no cover - defensive
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def advert_identity(org_name: str, title: str, url: str = "") -> str:
    """The advert's identity, using the registry's own hash.

    One identity authority for the registry, the dataset ledger, the review
    packs and now the wiki page: `job_registry._job_hash`. A second, local
    definition is how the wiki grew 3,431 duplicate blocks for adverts the
    registry already considered one row (2026-10-07).
    """
    from job_registry import _job_hash

    return _job_hash(org_name, title, url or "")


def block_identity(org_name: str, block_text: str) -> str:
    """Identity of an existing wiki block: its heading title + its URL line."""
    head = block_text.split("\n", 1)[0]
    title = head.split(" — ", 1)[1] if " — " in head else head
    title = title.strip().removesuffix("🆕").strip()
    url = _BLOCK_URL_RE.search(block_text)
    link = url.group(1) if url else ""
    # The no-link placeholder must fold to the registry's "" or the two
    # identities of the same linkless advert stop matching.
    if is_no_url_placeholder(link):
        link = ""
    return advert_identity(org_name, title, link)


def block_day(block_text: str) -> str:
    """The observation day a block claims, or "" when its heading carries none.

    Tolerates the heading with or without its "### " marker: callers hold a
    block in both shapes (the page split leaves the marker behind, the entry
    render keeps it), and a marker-sensitive match silently returned "" for
    every canonical block — which would have made the identity upsert treat
    every observation day as the same day (2026-10-07).
    """
    head = block_text.split("\n", 1)[0]
    if head.startswith("### "):
        head = head[4:]
    m = _BLOCK_DATE_RE.match(head)
    return m.group(1) if m else ""


def match_fields(match) -> dict:
    """Return optional multi-axis fields from a MatchResult-like object."""
    return {
        "fit_score": getattr(match, "fit_score", None),
        "reach_score": getattr(match, "reach_score", None),
        "flourish_score": getattr(match, "flourish_score", None),
        "combined_score": getattr(match, "combined_score", None),
        "recommended_action": getattr(match, "recommended_action", None),
        "primary_content_hash": getattr(match, "primary_content_hash", None),
        "primary_fetched_at": getattr(match, "primary_fetched_at", None),
        "primary_fetch_method": getattr(match, "primary_fetch_method", None),
        "alignment_matrix": getattr(match, "alignment_matrix", None),
        "gaps": getattr(match, "gaps", None),
        "application_angle": getattr(match, "application_angle", None),
        "screening_story": getattr(match, "screening_story", None),
        "likely_day_to_day": getattr(match, "likely_day_to_day", None),
        "prestige_trap_risk": getattr(match, "prestige_trap_risk", None),
        "assessment_status": getattr(match, "assessment_status", "SCORED"),
    }


_TRACKING_PARAM_RE = re.compile(
    r"^(utm_[a-z_]+|trk|trk[a-z_]*|trackingid|refid|lipi|midtoken|midsig|"
    r"trkemail|eid|otptoken|mc_cid|mc_eid|gh_jid|spm|scm|from|referrer)$",
    re.I)


#: The junk bucket: findings whose organisation could not be determined are
#: filed here rather than under a name that is not an organisation. One
#: authority for the page name — `_canonical_org_for_new`, the repair and the
#: audit all use this constant, because a second spelling of it splits the
#: bucket in two.
UNATTRIBUTED_ORG = "_Unattributed"


def clean_url(url: str) -> str:
    """Strip tracking params from a URL (LinkedIn digest links carried
    1,100+ chars of trackingId/refId/lipi/midToken — ugly, leaky, and the
    first thing to rot; the canonical /jobs/view/<id> is stable). The
    2026-10-07 audit found 1,700+ such URLs across the wikis."""
    if not url or not url.startswith("http"):
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _TRACKING_PARAM_RE.match(k)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(kept), parts.fragment))
    except ValueError:
        return url


def lead_serve_path(principal: str, directory) -> str:
    """The dashboard path for a lead page (the `leads` area is published)."""
    from leads import LEAD_PAGE_NAME

    return f"/principals/{principal}/leads/{Path(directory).name}/{LEAD_PAGE_NAME}"


def cached_advert_path(url: str):
    """Path of the cached advert page for `url`, or None when it is not cached.

    The report used to link `cache/page_<primary_content_hash>.md` — the
    sha256 of the fetched *text* — while the fetch cache is named by
    `sha256(url)[:16]` (`crawler._cache_path`). The two never agreed, so every
    "Cached advert" link in every report was dead (lj 6,771, vj 4,706, kj
    3,882; 2026-10-07 audit). Asking the cache for the path, and checking that
    the file exists, makes the link either correct or honestly absent.
    """
    if not url or not url.startswith("http"):
        return None
    try:
        from crawler import _cache_path

        path = _cache_path(url)
    except Exception:
        return None
    return path if path.exists() else None


def url_lines(e: "WikiEntry") -> List[str]:
    """The URL block for an entry.

    When the lead was resolved past an intermediary (a LinkedIn posting whose
    original advert was found), the original leads and the intermediary follows
    in parentheses — the same shape the notification email uses.
    """
    original = clean_url(e.original_url) if e.original_url else ""
    direct = clean_url(e.url) if e.url else ""
    if original and original != direct:
        return [f"- **URL**: {original} (via {human_slug(e.url)})"]
    if direct:
        return [f"- **URL**: {direct}"]
    if not e.discovery_url:
        return [f"- **URL**: {NO_URL_PLACEHOLDER}"]
    return []


def _days_since(value: str) -> int | None:
    """Whole days from an ISO date or timestamp to now, or None when unusable.

    Compares like with like: a zoned value against a naive `now()` raises
    `TypeError`, and a caller that swallows it shows *no age at all* — a silent
    data-quality loss rather than a visible failure (2026-09-21).
    """
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    return (now - when).days


def _is_ineligible(entry) -> bool:
    """True for entries the engine removed for eligibility, not for fit.

    Eligibility kills (work authorisation, an excluded country, a page that is
    not a job advert) mean the principal *cannot take the role* — the probability
    of landing it is zero however good the fit is — so the report records them
    without spending screen space on them. Staleness is deliberately not here:
    an advert open for months is a market signal the reader wants to see.
    """
    text = (getattr(entry, "reasoning", "") or "").lower()
    if not text.startswith("auto-drop:"):
        return False
    return any(marker in text for marker in _INELIGIBLE_MARKERS)


def _ineligible_reasons(entries) -> str:
    """A short "why" for the hidden count: the kill classes present, counted."""
    counts: dict = {}
    for entry in entries:
        text = (getattr(entry, "reasoning", "") or "").lower()
        for marker, label in _INELIGIBLE_LABELS.items():
            if marker in text:
                counts[label] = counts.get(label, 0) + 1
                break
        else:
            counts["other"] = counts.get("other", 0) + 1
    return ", ".join(f"{label} {n}" for label, n in sorted(counts.items()))


#: Markers in the engine's AUTO-DROP reasoning, and the label the report uses.
_INELIGIBLE_LABELS = {
    "work authorization": "work authorisation",
    "location deal-breaker": "excluded country",
    "not a job": "not an advert",
}
_INELIGIBLE_MARKERS = tuple(_INELIGIBLE_LABELS)


def dedupe_entries(entries: List["WikiEntry"]) -> tuple[List["WikiEntry"], int]:
    """One entry per advert identity, keeping the most useful verdict.

    A source can hand back the same advert many times in one run (the
    digitaljobs listing feed returned one advert ten times in the 2026-09-06
    lj sweep) and a re-run re-ingests the same email archive. The ledger keeps
    every assessment — that is the record of what happened — but the wiki, the
    report, the index and the review packs are *presentation*, and there one
    advert must appear once, or the page reads as ten conflicting verdicts.

    Returns (unique_entries, collapsed_count).
    """
    best: dict[str, "WikiEntry"] = {}
    order: list[str] = []
    for entry in entries:
        key = advert_identity(entry.org_name, entry.title, entry.url or "")
        current = best.get(key)
        if current is None:
            best[key] = entry
            order.append(key)
            continue
        if _entry_rank(entry) > _entry_rank(current):
            best[key] = entry
    return [best[k] for k in order], len(entries) - len(order)


def _entry_rank(entry: "WikiEntry") -> tuple:
    """How much a duplicate is worth keeping: scored, linked, then scored high."""
    scored = 0 if (entry.assessment_status or "").upper() == "UNSCORED" else 1
    linked = 1 if (entry.original_url or entry.url) else 0
    score = entry.combined_score if entry.combined_score is not None else entry.score
    return (scored, linked, float(score or 0.0))


def render_org_note(title: str, day: str, note: str, source: str,
                    fields=None) -> List[str]:
    """A non-finding block: something we observed about an organization.

    The page carries two block kinds and the writer renders both: a *finding*
    (a listing, with a `**Decision**`) and a *note* (a prior-contact
    cross-check, a diligence remark). `crosscheck` and `incremental_crawl`
    used to build the note text by hand and pass it as `reasoning`, which put
    a second `### ` heading *inside* a block and left `- **Decision**: ` empty
    — a block that splits every reader's parser in two (2026-10-07 audit).
    """
    block = [f"### {day} — {title}", "", f"- **Note**: {note}"]
    for name, value in (fields or {}).items():
        if value:
            block.append(f"- **{name}**: {value}")
    block.append(f"- **Source**: {source}")
    block.append("")
    return block


def render_org_block(e: "WikiEntry") -> List[str]:
    """One finding as an org-page block — THE org-page block writer.

    Both the crawl (`WikiStore.update_org_pages`) and the report-backfill
    (`wiki_backfill`) render through here, because two writers of the same
    document grew two schemas: the backfill copied the *report's* block shape
    (`### Org — Title`, `**Score**`, no date), and the index parser — which
    needs `### DATE — Title` and `**Decision**` — then could not see 1,778
    backfilled blocks in three wikis (2026-10-07 audit).
    """
    block = [
        f"### {e.date} — {e.title}",
        "",
        (
            # An unscored row has no verdict: the matcher did not run (an LLM
            # or profile failure). Printing the FLAG it defaults to states a
            # fit opinion nobody formed — 63 lj / 167 vj / 197 kj blocks did
            # (2026-10-07 audit).
            "- **Decision**: UNSCORED (matcher did not run)"
            if e.assessment_status == "UNSCORED"
            else f"- **Decision**: {e.decision} ({e.score}/10)"
        ),
    ]
    if e.fit_score is not None:
        block.append(f"- **Fit**: {e.fit_score}/10")
    if e.reach_score is not None:
        block.append(f"- **Reach**: {e.reach_score}/10")
    if e.flourish_score is not None:
        block.append(f"- **Flourish**: {e.flourish_score}/10")
    if e.combined_score is not None:
        block.append(f"- **Combined**: {e.combined_score}/10")
    if e.recommended_action:
        block.append(f"- **Action**: {e.recommended_action}")
    block.extend(url_lines(e))
    if e.discovery_url:
        date_suffix = f" ({e.discovery_date})" if e.discovery_date else ""
        block.append(f"- **Found via**: [{e.source}]({e.discovery_url}){date_suffix}")
    if e.primary_content_hash:
        block.append(
            f"- **Primary evidence**: `{e.primary_content_hash[:12]}` "
            f"via {e.primary_fetch_method} ({e.primary_fetched_at})"
        )
    if e.alignment_matrix:
        block.append("- **Evidence alignment**:")
        block.extend(f"  - {item}" for item in e.alignment_matrix)
    if e.gaps:
        block.append("- **Gaps**:")
        block.extend(f"  - {item}" for item in e.gaps)
    if e.application_angle:
        block.append(f"- **Application angle**: {e.application_angle}")
    if e.screening_story:
        block.append(f"- **Screening story**: {e.screening_story}")
    if e.likely_day_to_day:
        block.append(f"- **Likely day-to-day**: {e.likely_day_to_day}")
    if e.prestige_trap_risk:
        block.append(f"- **Prestige-trap risk**: {e.prestige_trap_risk}")
    if e.location:
        block.append(f"- **Location**: {e.location}")
    block.append(f"- **Reasoning**: {e.reasoning}")
    block.append(f"- **Source**: {e.source}")
    block.append("")
    return block


def render_report_block(e: "WikiEntry", principal: str, *,
                        show_age: bool = True, index: int | None = None,
                        org_stem: str | None = None) -> List[str]:
    """One finding as a report block — THE report block writer.

    `save_report` and `incremental_crawl` used to render reports with two
    different block writers, so the same advert read differently in a full
    report and in an incremental one (the incremental shape has no org-wiki
    link and puts the score before the decision). One writer, one shape.
    """
    lines: List[str] = []
    _format_entry(lines, e, principal, show_age=show_age, index=index,
                  org_stem=org_stem)
    return lines


def _format_entry(
    lines: List[str],
    e: "WikiEntry",
    principal: str,
    show_age: bool = False,
    index: int | None = None,
    org_stem: str | None = None,
) -> None:
    """Format a single WikiEntry into the report lines.

    Args:
        lines: Output list to append to.
        e: The entry to format.
        show_age: Whether to show how long an entry has been open.
        index: Ordinal position within its section (1-based). When set,
               the heading is prefixed with "N. ".
        principal: Principal name for building local-content links. Required,
               and taken from WikiStore.principal — a default of "lj" is how
               another principal's report linked into lj's tree.
        org_stem: Filename stem of this org's page as it exists on disk
                  (WikiStore._org_stem). Omitted by callers with no store to
                  consult, which then assume the plain sanitised name.
    """
    heading = f"### {e.org_name} — {e.title}"
    if index is not None:
        heading = f"### {index}. {e.org_name} — {e.title}"
    if e.lifecycle_status == "new":
        heading += " 🆕"
    lines.append(heading)
    lines.append("")
    lines.extend(url_lines(e))
    if e.discovery_url:
        date_suffix = f" ({e.discovery_date})" if e.discovery_date else ""
        lines.append(f"- **Found via**: [{e.source}]({e.discovery_url}){date_suffix}")
    if e.location:
        lines.append(f"- **Location**: {e.location}")
    # Local content links
    # Link must use the SAME filename the org page is written under
    # (WikiStore.org_page_path) — a re-guessed slug 404s on the dashboard
    # (2026-09-07: "renaissance-philanthropy.md" vs the actual
    # "Renaissance_Philanthropy.md"), and the page for an org already written
    # under a different capitalisation keeps that spelling, because the name is
    # folded case-insensitively on the way in (2026-09-20: "Aon" page is where
    # "AON" findings live).
    safe_org = org_stem or WikiStore._safe_filename(e.org_name)
    # …and the link follows the page to wherever its kind puts it (`orgs/` or
    # `agencies/`), because a guessed directory 404s exactly as a guessed slug did.
    wiki_root = config.STATE_DIR / principal / "wiki"
    page = org_page_for(wiki_root, safe_org)
    org_rel = (org_link(wiki_root, page) if page
               else f"{ORGS_DIRNAME}/{safe_org}.md")
    lines.append(f"- **Org wiki**: [/principals/{principal}/wiki/{org_rel}](</principals/{principal}/wiki/{org_rel}>)")
    if e.salary:
        lines.append(f"- **Salary**: {e.salary}")
    if e.seniority:
        lines.append(f"- **Seniority**: {e.seniority}")
    if e.primary_content_hash:
        # Link the cache file by the path the cache itself uses, and only when
        # it is there: a link to a filename nobody wrote is worse than no link
        # (see cached_advert_path).
        cache_file = cached_advert_path(e.original_url or e.url)
        # A name, not a link: `/principals/<p>/cache/**` is deliberately NOT a
        # published area (dashboard_server.PUBLISHED_PRINCIPAL_AREAS — the
        # cache holds jobs.csv, the ledgers and downloaded source material),
        # so a link there 404s however correct the filename is. The line still
        # carries the provenance a reader needs (which cache file, which
        # fetcher, when) — publishing the cache behind a
        # `page_[0-9a-f]{16}.md` allow-list is the alternative, recorded in
        # LEARNINGS.md as considered and deferred.
        if cache_file is not None:
            lines.append(
                f"- **Cached advert**: `{cache_file.name}` "
                f"(via {e.primary_fetch_method}, {e.primary_fetched_at}; "
                f"local cache, not a published path)"
            )
        else:
            lines.append(
                f"- **Cached advert**: _not retained_ "
                f"(via {e.primary_fetch_method}, {e.primary_fetched_at})"
            )
    if e.assessment_status == "UNSCORED":
        lines.append("- **Score**: UNSCORED")
    else:
        lines.append(f"- **Score**: {e.score}/10")
    if e.fit_score is not None:
        lines.append(f"- **Fit**: {e.fit_score}/10")
    if e.reach_score is not None:
        lines.append(f"- **Reach**: {e.reach_score}/10")
    if e.flourish_score is not None:
        lines.append(f"- **Flourish**: {e.flourish_score}/10")
    if e.combined_score is not None:
        lines.append(f"- **Combined**: {e.combined_score}/10")
    if e.recommended_action:
        lines.append(f"- **Action**: {e.recommended_action}")
    if e.alignment_matrix:
        lines.append("- **Evidence alignment**:")
        lines.extend(f"  - {item}" for item in e.alignment_matrix)
    if e.gaps:
        lines.append("- **Gaps**:")
        lines.extend(f"  - {item}" for item in e.gaps)
    if e.application_angle:
        lines.append(f"- **Application angle**: {e.application_angle}")
    if e.screening_story:
        lines.append(f"- **Screening story**: {e.screening_story}")
    if e.likely_day_to_day:
        lines.append(f"- **Likely day-to-day**: {e.likely_day_to_day}")
    if e.prestige_trap_risk:
        lines.append(f"- **Prestige-trap risk**: {e.prestige_trap_risk}")
    if e.lifecycle_status:
        age_str = ""
        if show_age:
            # The advert's own posting date is the true age; the floor is only
            # how long *we* have watched it. Never present the floor as the age
            # when a real date exists — "open 0 days" beside a four-month-old
            # posting is the exact impression PK was misled by (§WP3a).
            posted_days = _days_since(e.posted_at)
            floor_days = _days_since(e.first_seen)
            if posted_days is not None:
                age_str = f" (posted {posted_days} days ago, on {e.posted_at}"
                age_str += (f"; observed since {e.first_seen})" if e.first_seen
                            else ")")
            elif floor_days is not None:
                # "open at least 0 days" is true but reads as noise; the floor
                # only earns the "at least" phrasing once it is a real count.
                age_str = (f" (open at least {floor_days} days, since "
                           f"{e.first_seen})" if floor_days > 0
                           else f" (observed since {e.first_seen})")
        lines.append(f"- **Lifecycle**: {e.lifecycle_status}{age_str}")
    lines.append(f"- **Source**: {e.source}")
    lines.append(f"- **Reasoning**: {e.reasoning}")
    lines.append("")


# ── where an organisation's page lives ───────────────────────────────────
#
# **One authority, because a directory glob is an assumption.** An organisation's
# page is about a *company*, an *agency* or a *board*, and LJ asked for the
# separation (2026-10-15): *"Let us do the separation next - separate them move them
# into wiki/agencies."* The kind is recorded on the page itself
# (`lead_identity.ENTITY_MARKER_RE`), so the directory is just where the file sits —
# and every reader that hard-coded `orgs/` would silently stop seeing the agencies
# the moment they moved. That is the "the audit got quieter, not cleaner" failure
# this project already paid for once.
ORGS_DIRNAME = "orgs"
AGENCIES_DIRNAME = "agencies"


#: The index's freshness window, in days: a GO from a June sweep shown beside
#: October's is noise, because the principal cannot act on a months-old advert as
#: if it were fresh (LJ, 2026-10-06). History stays on the org pages, so the
#: window is a knob rather than a deletion. One reader — `current_view` and the
#: Counts block must not disagree about the window they are both describing.
def go_window_days() -> int:
    return int(os.getenv("LOVEWORK_WIKI_GO_WINDOW_DAYS", "30"))


def entry_identity(e: "WikiEntry") -> tuple[str, str, str]:
    """One advert's identity — the key every count and every listing must use.

    A `WikiEntry` for the same advert arrives twice: once from the run that saw it
    and once read back from the organisation page it was written to. They can carry
    different URL spellings — a LinkedIn `/comm/` link against the employer's Workday
    URL, an en-dash against two hyphens — so keying on the raw string lists one advert
    twice, and (measured 2026-10-18) makes a count of "listings on file" read exactly
    double the blocks the page holds.

    `advert_url_key` is the same authority the closed-advert check and the piles use.
    """
    from lead_identity import advert_url_key   # lazy: lead_identity imports us

    return (
        (e.org_name or "").lower().strip(),
        (e.title or "").removesuffix("🆕").lower().strip(),
        advert_url_key(e.url or ""),
    )


def org_page_dirs(wiki_root: Path) -> List[Path]:
    """Every directory an organisation page may sit in, `orgs/` first."""
    return [Path(wiki_root) / ORGS_DIRNAME, Path(wiki_root) / AGENCIES_DIRNAME]


def org_page_paths(wiki_root: Path) -> List[Path]:
    """Every organisation page on file, whichever kind put it where."""
    out: List[Path] = []
    for directory in org_page_dirs(wiki_root):
        if directory.is_dir():
            out.extend(sorted(directory.glob("*.md")))
    return out


def org_page_for(wiki_root: Path, stem_or_org: str) -> Optional[Path]:
    """The page for one organisation, in whichever directory holds it.

    Accepts a filename stem (`Enigma`) or an organisation name (`Enigma`), because
    callers hold one or the other and the two are the same page.
    """
    stem = stem_or_org[:-3] if stem_or_org.endswith(".md") else stem_or_org
    for directory in org_page_dirs(wiki_root):
        candidate = directory / f"{stem}.md"
        if candidate.is_file():
            return candidate
    return None


def org_link(wiki_root: Path, page: Path) -> str:
    """The relative link for a page, from wherever it actually is."""
    for directory in org_page_dirs(wiki_root):
        try:
            return f"{directory.name}/{page.relative_to(directory)}"
        except ValueError:
            continue
    return f"{ORGS_DIRNAME}/{page.name}"


class WikiEntry:
    """Single job finding to store in wiki."""

    def __init__(
        self,
        org_name: str,
        title: str,
        url: Optional[str],
        location: Optional[str],
        score: float,
        decision: str,
        reasoning: str,
        source: str,
        date: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        first_seen: Optional[str] = None,
        fit_score: Optional[float] = None,
        reach_score: Optional[float] = None,
        flourish_score: Optional[float] = None,
        combined_score: Optional[float] = None,
        recommended_action: Optional[str] = None,
        discovery_url: Optional[str] = None,
        discovery_date: Optional[str] = None,
        primary_content_hash: Optional[str] = None,
        primary_fetched_at: Optional[str] = None,
        primary_fetch_method: Optional[str] = None,
        alignment_matrix: Optional[List[str]] = None,
        gaps: Optional[List[str]] = None,
        application_angle: Optional[str] = None,
        screening_story: Optional[str] = None,
        likely_day_to_day: Optional[str] = None,
        prestige_trap_risk: Optional[str] = None,
        assessment_status: Optional[str] = None,
        advert_excerpt: Optional[str] = None,
        salary: Optional[str] = None,
        seniority: Optional[str] = None,
        posted_at: Optional[str] = None,
        original_url: Optional[str] = None,
        entity_kind: Optional[str] = None,
    ):
        self.org_name = org_name
        #: `company` | `agency` | `board`, **as the organisation's page records it**
        #: (`lead_identity.recorded_entity_kind`). Carried on the entry so a listing
        #: row needs no file read, and so the row says what the page decided rather
        #: than what a rule guessed (2026-10-15).
        self.entity_kind = entity_kind or ""
        self.title = title
        self.url = url or ""
        self.location = location or ""
        self.score = score
        self.decision = decision
        self.reasoning = reasoning
        self.source = source
        self.date = date or datetime.now().strftime("%Y-%m-%d")
        # new, still_open, long_lasting, disappeared, not_listed_in_scope
        self.lifecycle_status = lifecycle_status
        self.first_seen = first_seen  # ISO date when first seen by the crawler
        self.fit_score = fit_score
        self.reach_score = reach_score
        self.flourish_score = flourish_score
        self.combined_score = combined_score
        self.recommended_action = recommended_action
        self.discovery_url = discovery_url or ""
        self.discovery_date = discovery_date or ""
        # The advert behind an intermediary (a LinkedIn posting resolved to the
        # company's own advert). Rendered in preference to `url`, with the
        # intermediary shown in parentheses; empty when unknown.
        self.original_url = original_url or ""
        self.primary_content_hash = primary_content_hash or ""
        self.primary_fetched_at = primary_fetched_at or ""
        self.primary_fetch_method = primary_fetch_method or ""
        self.alignment_matrix = alignment_matrix or []
        self.gaps = gaps or []
        self.application_angle = application_angle or ""
        self.screening_story = screening_story or ""
        self.likely_day_to_day = likely_day_to_day or ""
        self.prestige_trap_risk = prestige_trap_risk or ""
        self.assessment_status = assessment_status or "SCORED"
        # The source text used by the matcher. Keep it out of public reports,
        # but preserve it in a private review pack for a newly actionable lead.
        self.advert_excerpt = advert_excerpt or ""
        # Structured listing fields (item 14, 2026-09-10 feedback). Populated
        # by structured sources (e.g. digitaljobs MCP); empty for alerts that
        # don't carry them.
        self.salary = salary or ""
        self.seniority = seniority or ""
        #: The advert's own posting date when the source gives one
        #: (docs/28 §WP3a) — the true age, unlike the observed-open floor.
        self.posted_at = posted_at or ""

        # Decode HTML entities once, on the way in: escaped text reaches the
        # wiki, the index, the report and the filenames (see _unescape).
        for field in ("org_name", "title", "location", "reasoning", "url",
                      "original_url", "discovery_url", "application_angle",
                      "screening_story", "likely_day_to_day",
                      "prestige_trap_risk", "salary", "seniority"):
            value = getattr(self, field, "")
            if isinstance(value, str):
                setattr(self, field, _unescape(value))
        self.alignment_matrix = [_unescape(x) for x in self.alignment_matrix]
        self.gaps = [_unescape(x) for x in self.gaps]


#: Explicit transliterations, applied before the NFKD fallback below. The
#: letters that actually occur in this data are German/Nordic/Slavic/Romance,
#: and a few of them (`ß` `æ` `œ` `ø` `ł` `đ` `ð` `þ` `ı`) have no Unicode
#: decomposition at all — without an entry they would fold to `_` and mangle
#: the name a second way. The values are the ones `mem.slug` (the memory-side
#: authority) produces, so a wiki page and a memory directory for one company
#: agree: `Deutsche Börse` -> `Deutsche-Borse`, `Straße` -> `Strasse`.
_TRANSLIT_MAP = {
    # German / Nordic
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
    "Ä": "A", "Ö": "O", "Ü": "U", "ẞ": "SS",
    "å": "a", "Å": "A", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
    # Romance / Iberian
    "ñ": "n", "Ñ": "N", "ç": "c", "Ç": "C",
    "è": "e", "é": "e", "ê": "e", "ë": "e",
    "È": "E", "É": "E", "Ê": "E", "Ë": "E",
    "à": "a", "â": "a", "á": "a", "ã": "a",
    "À": "A", "Â": "A", "Á": "A", "Ã": "A",
    "î": "i", "ï": "i", "í": "i", "ì": "i",
    "Î": "I", "Ï": "I", "Í": "I", "Ì": "I",
    "ô": "o", "ó": "o", "õ": "o", "ò": "o",
    "Ô": "O", "Ó": "O", "Õ": "O", "Ò": "O",
    "û": "u", "ú": "u", "ù": "u",
    "Û": "U", "Ú": "U", "Ù": "U",
    "ý": "y", "ÿ": "y", "Ý": "Y", "Ÿ": "Y",
    # Latin letters with no NFKD decomposition (they would otherwise be `_`)
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "œ": "oe", "Œ": "OE", "ı": "i",
}


def _transliterate(text: str) -> str:
    """Fold a name to plain ASCII *letters*, keeping the letters it had.

    Accents are folded, never dropped: `Deutsche Börse` must stay readable as
    `Deutsche_Borse`, not become `Deutsche_B_rse`. The house naming policy
    (AGENTS.md §8, and `mem.slug` on the memory side) is exactly this — the
    proper spelling lives in the text, the slug is the convenient ASCII form.

    A trademark or copyright mark carries no name and is dropped first
    (`mem.slug` does the same): NFKD spells `™` as "TM", which would put
    `Owen_Thomas_B_CorpTM` on disk for "Owen Thomas | B Corp™".

    Three passes, in order:
    1. the explicit map above (`ß` -> `ss`, `æ` -> `ae`, `ü` -> `u`, …);
    2. NFKD decomposition with the combining marks dropped, so any accented
       letter the map does not name still folds (`š` -> `s`, `ő` -> `o`);
    3. `_` for a character that survives both — a symbol, a CJK ideograph, an
       emoji. That is the same placeholder the caller already uses for
       punctuation, so it cannot introduce a new filename shape.

    Non-ASCII input therefore never yields an empty result, and two spellings
    that differ only by accents ("Zūm" / "Zum") land on the same stem instead
    of minting two pages for one company.
    """
    text = re.sub(r"[®™©]", "", text or "")
    out: List[str] = []
    for ch in text:
        mapped = _TRANSLIT_MAP.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        folded = "".join(
            c for c in unicodedata.normalize("NFKD", ch)
            if not unicodedata.combining(c)
        )
        out.append(folded if folded.isascii() else "_")
    return "".join(out)


class WikiStore:
    """Manages local markdown wiki."""

    def __init__(self, root: Path = config.WIKI_ROOT, *, create: bool = True):
        """`create=False` opens an **existing** wiki without writing to it.

        The store's directories are made on construction, which is right for a
        writer and wrong for a reader: the loop's decider asks "which GO is
        current?" once an hour and must not create `orgs/` and `reports/` in a
        principal's tree by asking. A read that writes is the class
        `tests/test_decide.py::test_deciding_is_a_pure_read` exists to catch, and
        it caught exactly this (2026-10-18).
        """
        #: Path of the most recent report written by this store. Callers
        #: (main.py / crawl_operation.py) read it to register the EXACT
        #: artifact — never "newest report by mtime".
        self.last_report_path: Path | None = None
        self.root = root
        self.orgs_dir = root / "orgs"
        self.reports_dir = root / "reports"
        if create:
            self.orgs_dir.mkdir(parents=True, exist_ok=True)
            self.reports_dir.mkdir(parents=True, exist_ok=True)
        #: case-folded stem → on-disk stem, built on first use; see _org_stem.
        self._org_stems: tuple[dict, dict] | None = None

    def save_report(
        self,
        entries: List[WikiEntry],
        profile_name: str = "LJ",
        run_type: str = "full",
        pack_results=None,
    ) -> Path:
        """Save a daily run report.

        The filename includes an hourly timestamp suffix so multiple
        sweeps on the same calendar date produce distinct files.
        The run type label ("full" or "incremental") replaces the role in
        the filename so reports are identifiable by crawl type.
        """
        date = entries[0].date if entries else datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H%M%S")
        run_label = run_type.lower().replace(" ", "-")
        profile_part = profile_name.split("-")[0].lower()
        path = self.reports_dir / f"{date}-{ts}-{profile_part}-{run_label}-report.md"

        go = [e for e in entries if e.decision == "GO"]
        maybe = [e for e in entries if e.decision == "MAYBE"]
        flag = [e for e in entries if e.decision == "FLAG"]
        kill = [e for e in entries if e.decision == "DROP"]

        new_jobs = [e for e in entries if e.lifecycle_status == "new"]
        long_lasting = [e for e in entries if e.lifecycle_status == "long_lasting"]
        still_open = [e for e in entries if e.lifecycle_status == "still_open"]

        header_type = run_type.upper().replace("-", " ")
        lines = build_header(
            run_type=header_type,
            profile_label=profile_name,
            sources=None,  # full sweep covers all 8; header explainer says so
        )
        # The principal comes from the store's own root (state/<p>/wiki), not
        # from the profile label and never from a default: a vj reassessment
        # wrote "/principals/lj/…" links into 7,570 places because the label
        # path was bypassed (2026-10-07 audit).
        principal_name = self.principal

        # Findings first: a reader wants the leads, not our plumbing. Order is
        # GO → MAYBE → New Listings → the rest (LJ, 2026-09-13): the actionable
        # verdicts open the report, and "new" follows as the wider net.
        for section, items in (("GO", go), ("MAYBE", maybe)):
            if not items:
                continue
            lines.append(section_heading(section, len(items)))
            lines.append("")
            for i, e in enumerate(
                sorted(items, key=lambda x: x.score, reverse=True), start=1
            ):
                _format_entry(lines, e, show_age=True, index=i, principal=principal_name, org_stem=self._org_stem(e.org_name))
            lines.append("")
            if section == "GO" and pack_results:
                from application_packs import render_pack_report_section

                lines.extend(render_pack_report_section(pack_results))

        # New listings section
        if new_jobs:
            # Header must state what is SHOWN: only GO/MAYBE new ads are
            # formatted; the FLAG/DROP remainder lives in its own sections.
            # Claiming (280) while showing 52 made readers count ghosts
            # (2026-09-13 VJ complaint).
            shown = sorted(
                (e for e in new_jobs if e.decision in ("GO", "MAYBE")),
                key=lambda x: x.score, reverse=True,
            )
            lines.append(section_heading("New Listings", len(shown)))
            lines.append("")
            if len(shown) < len(new_jobs):
                lines.append(
                    f"{len(shown)} of {len(new_jobs)} first-time ads shown "
                    "(GO and MAYBE only; the rest are scored under FLAG/DROP below)."
                )
                lines.append("")
            else:
                lines.append("First time seen by the crawler. Fresh opportunities.")
                lines.append("")
            for i, e in enumerate(shown, start=1):
                _format_entry(lines, e, show_age=True, index=i, principal=principal_name, org_stem=self._org_stem(e.org_name))
            lines.append("")
        if long_lasting:
            lines.append(section_heading("Long-Lasting / Suspicious", len(long_lasting)))
            lines.append("")
            lines.append(
                "Open >30 days. Company may be picky, unserious, or has infinite need. "
                "Score is lowered accordingly."
            )
            lines.append("")
            for i, e in enumerate(
                sorted(long_lasting, key=lambda x: x.score, reverse=True), start=1
            ):
                _format_entry(lines, e, show_age=True, index=i, principal=principal_name, org_stem=self._org_stem(e.org_name))
            lines.append("")

        for section, items in (("FLAG", flag), ("DROP", kill)):
            if not items:
                continue
            # Ineligible adverts are recorded, not listed (LJ, 2026-09-22): a role
            # requiring US work authorisation is a zero-probability lead — it
            # cannot be taken however good the fit — so it must not take screen
            # space. They stay in the registry, the dataset ledger and the org
            # pages; the header says how many and why. Staleness drops stay
            # listed: an old advert is a market signal worth reading, an
            # ineligible one is not a lead at all.
            listed = [e for e in items if not _is_ineligible(e)]
            hidden = [e for e in items if _is_ineligible(e)]
            lines.append(section_heading(section, len(listed), len(hidden),
                                          _ineligible_reasons(hidden)))
            lines.append("")
            for i, e in enumerate(
                sorted(listed, key=lambda x: x.score, reverse=True), start=1
            ):
                _format_entry(lines, e, show_age=True, index=i, principal=principal_name, org_stem=self._org_stem(e.org_name))
            lines.append("")

        # Reference material closes the report.
        lines.extend(build_trailer())

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Report saved to {path}")
        self.last_report_path = path
        return path

    @property
    def principal(self) -> str:
        """The principal this store writes for — derived from its own root.

        Never a caller's argument. `_format_entry` defaulted `principal` to
        "lj", so a vj *reassessment* report linked `/principals/lj/…` 5,846
        times (and kj's 6,490): every local-content link in those documents
        pointed into another principal's tree — wrong destination, and across
        the principal boundary the separate state trees exist to keep
        (2026-10-07 audit). `principal_runtime` builds the store at
        `state/<p>/wiki`, so the path already knows the answer.
        """
        try:
            if self.root.parent.parent == config.STATE_DIR:
                return self.root.parent.name
        except Exception:                 # pragma: no cover - defensive
            pass
        return "lj"                       # legacy store outside the state tree

    def org_page_path(self, org_name: str) -> Path:
        """This organisation's page, reusing whatever spelling is already there.

        Org names arrive from sources that disagree about capitalisation — one
        crawl reports "OpenAI" from one source and "Openai" from another — and
        the name is the page's identity, so a case-sensitive filename writes the
        same organisation into two files. macOS cannot hold two files whose
        names differ only by case, and Linux silently can, so the same crawl
        produced different wikis per platform (state/vj/wiki/orgs/AON.md next to
        Aon.md, 2026-09-20) and git could not carry both.

        Identity is therefore the case-folded stem: the first spelling to land
        owns the filename, later spellings append to that page. This is exactly
        what macOS does natively, so both platforms now agree.
        """
        stem = self._org_stem(org_name)
        # A page that already exists keeps its home, whichever kind put it there;
        # a *new* page is a company until someone records otherwise, so it is
        # minted in `orgs/` (LJ, 2026-10-15: the separation is by recorded kind).
        return org_page_for(self.root, stem) or (self.orgs_dir / f"{stem}.md")

    def _register_page(self, stem: str) -> None:
        """Record a page this store just minted, in both fold indexes."""
        if self._org_stems is None:
            return
        by_case, by_norm = self._org_stems
        by_case.setdefault(stem.casefold(), stem)
        by_norm.setdefault(_norm_key(stem), stem)

    def _org_index(self) -> tuple[dict, dict]:
        """Two fold indexes over the pages on disk, built once per store.

        * `casefold` — the filename identity macOS enforces.
        * `norm_key` — the alnum-fold identity that MINTS pages
          (`company_events.norm_key`).

        They disagree ("Shield AI" → casefold "shield_ai" but norm_key
        "shieldai", which is the page `Shieldai.md`), and the writer used only
        the first: a report linked `orgs/Shield_AI.md` while the page it meant
        was `Shieldai.md`. That was the last class still breaking org links on
        the 2026-10-03/04 reports (16 lj / 9 vj / 7 kj) — 2026-10-07 audit.
        """
        if self._org_stems is None:
            by_case: dict[str, str] = {}
            by_norm: dict[str, str] = {}
            for p in org_page_paths(self.root):
                by_case.setdefault(p.stem.casefold(), p.stem)
                by_norm.setdefault(_norm_key(p.stem), p.stem)
            self._org_stems = (by_case, by_norm)
        return self._org_stems

    def _canonical_org_for_new(self, org_name: str) -> str:
        """The org an entry should be filed under, before a new page is minted.

        Five gates, in this order:
        1. a URL in the name, or a name long enough to be a mis-split header
           (`is_implausible_header`) — the two checks this method has always
           made first;
        2. a name that already owns a page is returned as it stands: nothing is
           being minted, so no minting gate may divert it (see below);
        3. a name that is not an organisation at all — a salary string, a
           mail-digest line, a sentence, an address, an opaque token, a bare
           role phrase (`lead_identity.is_non_org_name`) — or a placeholder, a
           UI string or a salary band (`lead_identity.is_noise_company`) goes to
           `_Unattributed`, and the raw name stays in the entry line;
        4. a name that cleans to a real company (`LAGO - https://getlago.com/
           hiring/jobs` -> `LAGO`, `Jefferies New York NY` -> `Jefferies`) is
           filed under the cleaned name, so a page is minted under the company,
           not under the concatenation;
        5. otherwise the raw name.

        Gate 2 runs before gates 3 and 4 on purpose, and the order is load
        bearing — the junk predicates over-match two classes of real company: a
        *bare* domain ("Checkout.com", "IPinfo.io"), which
        `is_non_org_name`'s own `_URL_TAIL_RE` comment calls the audit's false
        class (2026-10-07), and any name they cannot parse.
        `QCG Computer (Jülich) GmbH` is the second: with gate 3 first, a finding
        for that company is filed to `_Unattributed` even though its page
        exists, because the transliterated stem (`QCG_Computer_Julich_GmbH`) and
        `norm_key` ("…Jlich…", which drops the accent) no longer agree.
        Consulting the existing page first keeps every page that already has an
        identity, and the gates then do what they are for: stop a *new* page
        being minted under a junk name.
        """
        from lead_identity import (clean_org_name, is_implausible_header,
                                   is_noise_company, is_non_org_name)
        import re as _re

        def _existing(name: str) -> Optional[str]:
            """The page that already owns `name`, if any.

            Two keys, because the transliteration inserts letters `norm_key`
            cannot see: `norm_key("Deutsche Börse")` is "deutschebrse" while
            its page is now `Deutsche_Borse` ("deutscheborse"), so a page
            renamed to its transliterated stem would stop matching the name
            that produced it. The second key is the writer's own stem rule, so
            "these two names are one page" means exactly what `_safe_filename`
            means.
            """
            try:
                from company_events import norm_key
            except Exception:
                return None
            key = norm_key(name)
            stem = self._safe_filename(name).casefold()
            for existing in org_page_paths(self.root):
                if (norm_key(existing.stem) == key
                        or existing.stem.casefold() == stem):
                    return existing.stem.replace("_", " ")
            return None

        raw = (org_name or "").strip()
        if not raw:
            return UNATTRIBUTED_ORG
        if _re.search(r"https?://|www\.", raw, re.I) or \
                is_implausible_header(raw, "Role"):
            # Gate 1 has always been "a URL in the name is not a name" — but a
            # name *plus* a URL tail has a company inside it
            # (`LAGO - https://getlago.com/hiring/jobs`), and the repair files
            # such a page under the company. The gate must agree with the
            # repair, or a new finding re-creates the page the repair removed.
            cleaned = clean_org_name(raw)
            if cleaned and cleaned != raw and not is_non_org_name(cleaned):
                return _existing(cleaned) or cleaned
            return UNATTRIBUTED_ORG
        existing = _existing(raw)
        if existing:
            return existing
        if is_non_org_name(raw) or is_noise_company(raw):
            return UNATTRIBUTED_ORG
        cleaned = clean_org_name(raw)
        if cleaned and cleaned != raw:
            return _existing(cleaned) or cleaned
        return raw


    def _org_stem(self, org_name: str) -> str:
        """The filename stem for `org_name`, folded against what exists."""
        stem = self._safe_filename(org_name)
        by_case, by_norm = self._org_index()
        return (by_case.get(stem.casefold())
                or by_norm.get(_norm_key(stem))
                or stem)

    def update_org_pages(self, entries: List[WikiEntry]) -> dict:
        """Write a run's findings to their organization pages — idempotently.

        One advert is one block per observation day. The writer used to append
        unconditionally, so the same advert accumulated a block per assessment:
        3,431 redundant blocks in lj (26% of the page body), 898 in vj, 1,377
        in kj — including 102 identical-dated blocks for one advert on
        `Competitive.md` and 25 on `Owen_Thomas_B_Corp.md` (2026-10-07 audit).
        Identity is `job_registry._job_hash` — the registry's own advert hash —
        so the registry, the ledger and the wiki cannot disagree about what
        "the same advert" means; the block for an identity *and* day is
        replaced in place, so the newest verdict wins and the page stays a
        history of observation days rather than a log of assessments.

        Returns {"pages": n, "blocks_written": n, "blocks_replaced": n}.
        """
        stats = {"pages": 0, "blocks_written": 0, "blocks_replaced": 0}
        by_page: dict[Path, list] = {}
        for entry in entries:
            org = self._canonical_org_for_new(entry.org_name)
            if org != entry.org_name:
                entry.org_name = org
            by_page.setdefault(self.org_page_path(entry.org_name), []).append(entry)

        for path, page_entries in by_page.items():
            if path.exists():
                content = path.read_text(encoding="utf-8")
            else:
                org_name = page_entries[0].org_name
                content = f"# {org_name}\n\nFindings from lovework-agent.\n\n---\n\n"
                self._register_page(path.stem)
            org_name = self._page_org_name(content, path)
            content, written, replaced = self._upsert_blocks(
                content, org_name, [render_org_block(e) for e in page_entries])
            path.write_text(content, encoding="utf-8")
            stats["pages"] += 1
            stats["blocks_written"] += written
            stats["blocks_replaced"] += replaced
        return stats

    def update_org_note(self, org_name: str, note: str, *, source: str,
                        title: str = "(prior-contact cross-check)",
                        day: str | None = None, fields=None,
                        url: str = "") -> None:
        """Record a note on an organization's page (no verdict, no score)."""
        day = day or datetime.now().strftime("%Y-%m-%d")
        entry_org = self._canonical_org_for_new(org_name)
        path = self.org_page_path(entry_org)
        if path.exists():
            content = path.read_text(encoding="utf-8")
            org_label = self._page_org_name(content, path)
        else:
            content = f"# {entry_org}\n\nFindings from lovework-agent.\n\n---\n\n"
            org_label = entry_org
            self._register_page(path.stem)
        # A note carries its source advert's URL when it has one, so the
        # identity upsert can tell two notes apart (and collapse a re-run).
        rendered_fields = dict(fields or {})
        if url:
            rendered_fields = {"URL": url, **rendered_fields}
        content, _, _ = self._upsert_blocks(
            content, org_label,
            [render_org_note(title, day, note, source, rendered_fields)])
        path.write_text(content, encoding="utf-8")

    def update_org_page(self, entry: WikiEntry) -> None:
        """Append (or refresh) one finding on its organization's page.

        Kept for single-entry callers (`crosscheck`, `incremental_crawl`,
        `consume`, the tool service); the batch form is what a crawl uses.
        """
        self.update_org_pages([entry])

    @staticmethod
    def _page_org_name(content: str, path: Path) -> str:
        """The org a page belongs to: its H1, else its filename stem."""
        for line in content.splitlines()[:5]:
            if line.startswith("# "):
                return line[2:].strip()
        return path.stem.replace("_", " ")

    @staticmethod
    def _upsert_blocks(content: str, org_name: str, blocks: List[List[str]]):
        """Fold `blocks` into `content`, one block per (advert identity, day).

        Returns (content, blocks_added, blocks_replaced).
        """
        parts = _BLOCK_HEAD_RE.split(content)
        prefix = parts[0]
        existing = parts[1:]
        index: dict[tuple, int] = {}
        for i, block in enumerate(existing):
            index.setdefault(
                (block_identity(org_name, block), block_day(block)), i)

        added = replaced = 0
        for lines in blocks:
            block = "\n".join(lines)
            # stored without the heading marker, matching what the split left
            if block.startswith("### "):
                block = block[4:]
            key = (block_identity(org_name, block), block_day(block))
            if key in index:
                existing[index[key]] = block
                replaced += 1
            else:
                index[key] = len(existing)
                existing.append(block)
                added += 1
        if not existing:
            return content, 0, 0
        return prefix + "### " + "### ".join(existing), added, replaced


    def _lead_index(self) -> dict:
        """advert identity → lead directory for this principal (one read)."""
        try:
            from leads import lead_index

            return lead_index(self.principal)
        except Exception:
            return {}

    def _pack_index(self) -> dict:
        """advert identity → pack directory for this principal (one read)."""
        try:
            from application_packs import pack_index

            return pack_index(self.principal)
        except Exception:
            return {}

    @staticmethod
    def _compact(value: str) -> str:
        """Alphanumerics only, folded: how a pack slug and a report title agree."""
        return "".join(ch for ch in (value or "").lower() if ch.isalnum())

    def _retired_keys(self) -> set:
        """The two closing piles, from the authority that owns them (`retire.py`).

        A retired advert is out of the decision space, so it leaves the GO/MAYBE/
        FLAG/DROP counts and listings and lives in its pile's index instead — LJ's
        aim for the piles: "we want high SNR, high quality ads" (2026-10-14).
        """
        import retire

        return retire.retired_keys(self.principal)

    def _pile_counts(self) -> dict:
        """How many adverts each closing pile holds (its ledger, not a scan)."""
        counts = {}
        for pile in ("gone", "expired", "leads"):
            ledger = config.STATE_DIR / self.principal / pile / "retired.jsonl"
            if not ledger.is_file():
                continue
            try:
                with ledger.open(encoding="utf-8", errors="replace") as handle:
                    counts[pile] = sum(1 for line in handle if line.strip())
            except OSError:
                continue
        return counts

    def _email_verdicts(self) -> dict:
        """The detector's last scan, for the count line (one read, no fetch)."""
        try:
            from apply_by_email import load_verdicts

            return load_verdicts(self.principal)
        except Exception:
            return {}

    def _email_index(self) -> set:
        """Packs whose advert can be applied to by email (one read).

        From the detector's own verdict file, never a fetch: an index build must
        not reach the network (LJ, 2026-10-13: "maybe we should 1-count and
        2-highlight those GO-s that one can apply by email").
        """
        try:
            from apply_by_email import applicable_packs

            return set(applicable_packs(self.principal))
        except Exception:
            return set()

    @staticmethod
    def _row_markers(entry: "WikiEntry") -> str:
        """What a listing row must say about itself, in one place.

        Two facts a reader needs before clicking, and neither was visible:

        * **NO LINK** — a GO with no advert link cannot be applied to. LJ's first GO
          row was exactly that (`Enigma — Research Engineer | 9.3/10 (no advert
          link)`, 2026-10-15): the roles came off a JavaScript-only agency site
          whose fetched markdown carries no hrefs, so we kept the titles and lost
          the links.
        * **AGENCY** — the organisation relays adverts rather than hiring. LJ:
          *"Enigma is a recruiter - not a company hiring itself."* A label, not a
          filter (`lead_identity.org_kind`): nothing is dropped or demoted on it.
        """
        from lead_identity import org_kind

        url = clean_url(entry.original_url or entry.url or "")
        markers = []
        if not url:
            markers.append("**NO LINK**")
        # The page's recorded kind wins; the rules only answer for a page that has
        # never been classified.
        kind = getattr(entry, "entity_kind", "") or org_kind(entry.org_name, url)
        if kind == "agency":
            markers.append("**AGENCY**")
        elif kind == "board":
            markers.append("**BOARD**")
        return (" " + " ".join(markers)) if markers else ""

    @staticmethod
    def _advert_identity(entry: "WikiEntry") -> str:
        """The registry's advert hash — the key a pack is filed under."""
        try:
            from job_registry import _job_hash

            return _job_hash(entry.org_name, entry.title, entry.url or "")
        except Exception:
            return ""

    def current_view(self, entries: Optional[List[WikiEntry]] = None, *,
                     window_days: Optional[int] = None,
                     today: Optional[date] = None,
                     ) -> tuple[List[WikiEntry], List[WikiEntry]]:
        """**The index's view of the world**: `(current, fresh)`, one row per advert.

        `current` is one row per advert — the *newest* observation wins — with
        retired adverts removed (docs/45: a retired advert leaves the decision
        space entirely, its material is in the pile); `fresh` is the subset inside
        the freshness window.

        `entries` are the entries to view. A crawl passes its own findings
        merged with what is already on disk, so a partial or cron run still
        leaves a populated view; absent, the view is built from the org pages
        alone — which is what a reader that has not just crawled wants, and what
        the loop's decider is.

        Two callers need exactly this pair, and that is why it is one reader:
        `rebuild_index` lists `fresh` and reports `len(current)` as the total on
        file, and the loop's `review_go` reviews the GOs the principal can still
        see. Two readers here would be two answers to "which GO is current" — the
        disagreement this project already paid for: 266 adverts carried *some* GO
        block while only 121 had GO as their current verdict, so the hourly pass
        kept preparing material for 145 adverts the index had dropped (2026-10-14).
        """
        from lead_identity import advert_url_key

        window_days = go_window_days() if window_days is None else window_days
        cutoff = (today or date.today()) - timedelta(days=window_days)
        # All four decisions: the index has a DROP section, and a view that read
        # only GO/MAYBE/FLAG would call an advert's DROP its current verdict and
        # count it as a finding (the reader's own default is the old three).
        all_entries = list(entries) if entries is not None else \
            self._entries_from_org_pages(decisions=("GO", "MAYBE", "FLAG", "DROP"))

        # NEWEST observation wins, not the first one read. The same advert is
        # re-assessed as it ages — Latent Labs' Computational Protein Designer was
        # DROP 1.8 on 2026-10-01 and MAYBE 6.1 on 2026-10-04 — and keeping the
        # first block showed the stale verdict: the index said MAYBE while the lead
        # page built from the same page said DROP, and a reader cannot tell which
        # is current (LJ, 2026-10-09: "this DROP … is in MAYBE together with all").
        # The current run's entries win ties: they are the freshest statement we have.
        #
        # The key is the advert's *identity*, not its spelling. The same advert
        # arrives from the crawl's own entries and from the org pages, and the two
        # can carry different URL forms — a LinkedIn `/comm/` link against the
        # employer's Workday URL, an en-dash against two hyphens — so keying on the
        # raw string listed one advert twice, identically, and LJ clicked the copy
        # that 404s (measured 2026-10-14: "European Bioinformatics Institute … —
        # Scientific Developer (Chemical Biology Resources)" appeared on two
        # consecutive index rows). `advert_url_key` is the same authority the
        # closed-advert check and the piles use.
        def _identity_key(e: WikiEntry) -> tuple[str, str, str]:
            return entry_identity(e)

        best: dict[tuple[str, str, str], WikiEntry] = {}
        order: list[tuple[str, str, str]] = []
        for e in all_entries:
            key = _identity_key(e)
            prior = best.get(key)
            if prior is None:
                best[key] = e
                order.append(key)
                continue
            if str(e.date or "") > str(prior.date or ""):
                best[key] = e
        deduped: List[WikiEntry] = [best[key] for key in order]

        # Retired adverts leave the decision space entirely (docs/45): counting
        # them as live would misstate the corpus we can act on. The two closing
        # piles are read from the authority that owns them (`retire.py`).
        retired = self._retired_keys()
        # …and by URL, not only by name: a pack directory whose name truncates
        # a long role (`…Modelin` against the block's `…Modeling`) folds to a
        # different (org, role) key, so the first review-ruled enactment
        # (2026-10-18, Cantina Labs) stayed live in the view while its pack sat
        # in leads/. `retired_urls` is the identity the audit already uses for
        # exactly this mismatch; the view now honours it too.
        import retire as _retire
        gone_urls = _retire.retired_urls(self.principal)

        def _live(e: "WikiEntry") -> bool:
            if (self._compact(e.org_name), self._compact(e.title)) in retired:
                return False
            url = advert_url_key(e.url or "")
            return not (url and url in gone_urls)

        def _fresh(e: "WikiEntry") -> bool:
            try:
                return date.fromisoformat(str(e.date or "")[:10]) >= cutoff
            except ValueError:
                return True   # an undated entry is shown, never silently lost

        current = [e for e in deduped if _live(e)]
        return current, [e for e in current if _fresh(e)]

    def rebuild_index(self, entries: List[WikiEntry]) -> None:
        """Rebuild the master index.md.

        `entries` is the current run's findings. The index combines them
        with anything already on disk in `wiki/orgs/` so a partial / cron
        run still leaves a fully populated index.

        The org-page format is parsed (### DATE — Title blocks under each
        org's wiki file) and merged with the in-memory entries; duplicates
        (same org+title+url) are dedup'd by first-seen.
        """
        path = self.root / "index.md"
        # Merge: in-memory entries + parsed from disk.
        # All four decisions: the index now has a DROP section, so it needs the
        # DROP blocks too (the reader's default is GO/MAYBE/FLAG, which is what
        # the earlier index wanted).
        all_entries = list(entries) + self._entries_from_org_pages(
            decisions=("GO", "MAYBE", "FLAG", "DROP"))
        # …then ask the one reader that says which of them are current: newest
        # verdict per advert, retired adverts removed, freshness window applied.
        # `deduped` is the whole on-file corpus (the "Total findings" line);
        # `fresh` is what the listings show. `window_days` is the same number
        # `current_view` applied — the Counts block names it, so it must not be
        # re-read here.
        deduped, fresh = self.current_view(all_entries)
        window_days = go_window_days()

        go_all = [e for e in deduped if e.decision == "GO"]
        maybe_all = [e for e in deduped if e.decision == "MAYBE"]
        flag_all = [e for e in deduped if e.decision == "FLAG"]
        kill_all = [e for e in deduped if e.decision == "DROP"]

        # FLAG and DROP get their own sections, on the same freshness window: the
        # index is the state of the world, and a rejected June advert is history.
        go = [e for e in fresh if e.decision == "GO"]
        maybe = [e for e in fresh if e.decision == "MAYBE"]
        flag = [e for e in fresh if e.decision == "FLAG"]
        kill = [e for e in fresh if e.decision == "DROP"]
        go_old, maybe_old = len(go_all) - len(go), len(maybe_all) - len(maybe)
        flag_old, kill_old = len(flag_all) - len(flag), len(kill_all) - len(kill)

        # The email-application count and the packs behind it, read once here
        # because both the Counts block and the GO listing need them (LJ,
        # 2026-10-13: "1-count and 2-highlight"). A pure read of the detector's
        # verdict file: an index build never fetches.
        piles = self._pile_counts()
        email_verdicts = self._email_verdicts()
        emails = self._email_index()
        email_count = email_verdicts.get("applicable", 0)
        email_packs = email_verdicts.get("packs", 0)
        email_inspected = email_verdicts.get("inspected", 0)
        email_coverage = email_verdicts.get("coverage", 0.0)

        # Latest report = the most recent by report date.
        latest_report_rel = self._latest_report_rel()

        lines = [
            "# LoveWork Index",
            "",
            f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Quick Links",
            "",
            f"- [Latest report]({latest_report_rel})" if latest_report_rel else "- [Reports](reports/)",
            f"- [All reports](reports/)",
            f"- [Organizations](orgs/) ({len(list(self.orgs_dir.glob('*.md')))} pages)",
            "",
            "## Counts",
            "",
            f"- **GO**: {len(go)} listings (last {window_days} days; "
            f"{go_old} older on file — see org pages)",
            f"- **MAYBE**: {len(maybe)} listings (last {window_days} days; "
            f"{maybe_old} older on file)",
            f"- **FLAG**: {len(flag)} listings"
            + (f" (last {window_days} days; {flag_old} older on file)" if flag_old else ""),
            f"- **DROP**: {len(kill)} listings"
            + (f" (last {window_days} days; {kill_old} older on file)" if kill_old else ""),
            f"- **Total findings on file**: {len(deduped)}",
            # The count LJ asked for, beside the counts a reader already sees
            # (2026-10-13: "maybe we should 1-count and 2-highlight those GO-s
            # that one can apply by email"). It carries its coverage, because an
            # advert whose page we never kept cannot be judged.
            *([f"- **GONE**: {piles['gone']} closed GO pack(s) — "
               "[the index](gone/index.md)"] if piles.get("gone") else []),
            *([f"- **EXPIRED**: {piles['expired']} closed advert(s) — "
               "[the index](expired/index.md)"] if piles.get("expired") else []),
            *([f"- **Applicable by email**: {email_count} GO pack(s) "
               f"(advert text retained for {email_inspected} of {email_packs} packs "
               f"= {email_coverage}%; the count is a floor) — "
               "[the listing](apply-by-email.md)"] if email_count else []),
            "",
            "## GO Listings",
            "",
        ]
        if go:
            # The pack index: advert identity → case directory, read once (LJ,
            # 2026-10-09 — a GO listing must lead to the pack, not only to the
            # advert, so the principal starts from prepared material).
            packs = self._pack_index()
            for e in sorted(go, key=lambda x: (x.date, x.score, x.org_name, x.title), reverse=True):
                preferred = clean_url(e.original_url or e.url)
                # The PACK leads the line (LJ, 2026-10-09: "make PACK more
                # prominent by being capitalised and 1st. As I almost missed the
                # 'pack' reference at the tail end of the url"). The org—title is
                # the pack's link, so the first thing a reader clicks is the
                # prepared material, and a missing pack is loud rather than
                # implied.
                # One lookup, two keys (identity hash, then the org+title
                # tolerance). `pack_for` is the authority — this row used to ask
                # only for the hash, so a GO read "NO PACK" while its pack existed
                # (2026-10-14: kj Tacit Labs, lj Cantina Labs).
                from application_packs import pack_for

                directory = pack_for(packs, e.org_name, e.title,
                                     e.original_url or e.url or "")
                if directory is not None:
                    from application_packs import PACK_INDEX_NAME, pack_serve_path

                    head = (f"**PACK** [{e.org_name} — {e.title}]"
                            f"({pack_serve_path(self.principal, directory, PACK_INDEX_NAME)})")
                    # The one route LoveWork can carry end to end: an advert that
                    # takes applications by email (LJ, 2026-10-13). A marker, not
                    # a promise — the address and its sentence are on the pack and
                    # in `wiki/apply-by-email.md`.
                    # `emails` holds pack *names*; `directory` is a path.
                    if Path(directory).name in emails:
                        head += " **EMAIL**"
                else:
                    page = org_page_for(
                        self.root, self._safe_filename(self._org_stem(e.org_name)))
                    org_rel = (org_link(self.root, page) if page
                               else f"orgs/{self._safe_filename(self._org_stem(e.org_name))}.md")
                    head = f"**NO PACK** [{e.org_name} — {e.title}]({org_rel})"
                head += self._row_markers(e)
                tail = (f"[{preferred}]({preferred})" if preferred
                        else "_(no advert link)_")
                lines.append(f"- **{e.date}** | {head} | {e.score:.1f}/10 {tail}")
        else:
            lines.append("_No GO listings yet._")
        lines.append("")

        leads = self._lead_index()

        def listing(items, title: str, empty: str, cap: int = 50) -> None:
            """One decision's section — GO and MAYBE first, then the rest.

            LJ, 2026-10-09: *"keep the GO-s at the top, then the MAYBE-s, but
            then have separate sections for DROP and other classes"* — a DROP
            must never read as a MAYBE because the reader scrolled into a
            section they did not expect.
            """
            lines.append(f"## {title}")
            lines.append("")
            if not items:
                lines.append(empty)
                lines.append("")
                return
            for e in sorted(items,
                            key=lambda x: (x.date, x.score, x.org_name, x.title),
                            reverse=True)[:cap]:
                preferred = clean_url(e.original_url or e.url)
                url_text = (f"[{preferred}]({preferred})" if preferred
                            else "_(no advert link)_")
                # A judged lead leads with its LEAD page, exactly as a GO leads
                # with its PACK: the judgement and the evidence behind it, one
                # click.
                directory = leads.get(self._advert_identity(e))
                if directory is not None:
                    head = (f"**LEAD** [{e.org_name} — {e.title}]"
                            f"({lead_serve_path(self.principal, directory)})")
                else:
                    head = f"**NO LEAD** {e.org_name} — {e.title}"
                head += self._row_markers(e)
                lines.append(f"- **{e.date}** | {head} | {e.score:.1f}/10 {url_text}")
            if len(items) > cap:
                lines.append(f"_…and {len(items) - cap} more (see reports)._")
            lines.append("")

        listing(maybe, "MAYBE Listings", "_No MAYBE listings yet._")
        listing(flag, "FLAG Listings — worth a look",
                "_No FLAG listings in the window._")
        listing(kill, "DROP Listings — examined and rejected",
                "_No DROP listings in the window._")

        # **Agencies, apart from the companies** (LJ, 2026-10-15: *"We should have
        # separate wiki pages for the agencies, separate from the companies? So we
        # keep history, but we can also distinguish company from agency."*). The
        # kind comes from each page's own marker (`entity_kind.py` records it), and
        # the entries already carry it — so this section costs no extra read. An
        # agency advertises roles it does not own, and a reader should be able to
        # see that the two are different before clicking.
        # Count each **advert** once, and take the kind from whichever copy records
        # it (2026-10-18). `all_entries` is the run's entries *plus* the pages they
        # were written to, so a raw tally counted every advert twice: Harnham read
        # "130 listing(s) on file" over a page holding 65 blocks, and 14 of the 15
        # agency rows were exactly 2.00x. The kind lives on the page's copy (that is
        # where the marker is), so a plain dedupe to the first copy would drop the
        # agencies instead of the duplicate.
        agency_kinds: dict[tuple[str, str, str], tuple[str, str]] = {}
        for e in all_entries:
            key = entry_identity(e)
            prior = agency_kinds.get(key)
            kind = getattr(e, "entity_kind", "")
            if prior is None:
                agency_kinds[key] = (kind, e.org_name)
            elif kind and not prior[0]:
                agency_kinds[key] = (kind, prior[1])
        agency_orgs: dict[str, int] = {}
        for kind, org_name in agency_kinds.values():
            if kind in ("agency", "board"):
                agency_orgs[org_name] = agency_orgs.get(org_name, 0) + 1
        if agency_orgs:
            lines.append("## Agencies (intermediaries — they advertise roles they "
                         "do not own)")
            lines.append("")
            for name, count in sorted(agency_orgs.items(),
                                      key=lambda item: (-item[1], item[0]))[:40]:
                page = org_page_for(self.root,
                                    self._safe_filename(self._org_stem(name)))
                rel = (org_link(self.root, page) if page
                       else f"orgs/{self._safe_filename(self._org_stem(name))}.md")
                lines.append(f"- [{name}]({rel}) — {count} listing(s) on file")
            if len(agency_orgs) > 40:
                lines.append(f"_…and {len(agency_orgs) - 40} more._")
            lines.append("")

        # Organisational browsing — first 30 org pages by mtime, with link.
        orgs = sorted(org_page_paths(self.root), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        if orgs:
            lines.append("## Organizations (recent)")
            lines.append("")
            for p in orgs[:30]:
                stem = p.stem.replace("_", " ")
                lines.append(f"- [{stem}]({org_link(self.root, p)})")
            if len(orgs) > 30:
                lines.append(f"_…and {len(orgs) - 30} more in [orgs/](orgs/)._")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Index rebuilt at {path}")

    def _entries_from_org_pages(self, decisions=("GO", "MAYBE", "FLAG")
                                ) -> List[WikiEntry]:
        """Parse existing `wiki/orgs/<Org>.md` files back into WikiEntry
        rows. Used to merge into the index on rebuild so the index is
        always populated even after a partial / cron run.

        `decisions` is parameterised because the *lead pages* need the DROP rows
        too (LJ, 2026-10-09: the analysis behind a DROP must be saved as well),
        while the index lists only GO/MAYBE/FLAG. One reader, two callers — a
        second parser is how two answers to "what does this page say" appear.
        """
        import re
        out: List[WikiEntry] = []
        if not any(d.is_dir() for d in org_page_dirs(self.root)):
            return out
        section_re = re.compile(r"^###\s+(?P<date>\d{4}-\d{2}-\d{2})\s+—\s+(?P<title>.+?)\s*$")
        decision_re = re.compile(
            r"^\s*-\s*\*\*Decision\*\*:\s*\*?(GO|MAYBE|FLAG|DROP)\*?"
            r"(?:\s*\((?P<score>\d+(?:\.\d+)?)/10\))?"
        )
        action_re = re.compile(r"^\s*-\s*\*\*Action\*\*:\s*(?P<action>[A-Z_]+)")
        fit_re = re.compile(r"^\s*-\s*\*\*Fit\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        reach_re = re.compile(r"^\s*-\s*\*\*Reach\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        flourish_re = re.compile(r"^\s*-\s*\*\*Flourish\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        combined_re = re.compile(r"^\s*-\s*\*\*Combined\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        url_re = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
        loc_re = re.compile(r"^\s*-\s*\*\*Location\*\*:\s*(?P<loc>.+)")
        source_re = re.compile(r"^\s*-\s*\*\*Source\*\*:\s*(?P<src>.+)")
        # The ANALYSIS block: a lead page must show what the decision was made
        # from, not a stub. The index ignores these fields; the lead pages are
        # built on them (2026-10-09).
        reasoning_re = re.compile(r"^\s*-\s*\*\*Reasoning\*\*:\s*(?P<value>.+)")
        text_field_res = {
            "application_angle": re.compile(r"^\s*-\s*\*\*Application angle\*\*:\s*(?P<v>.+)"),
            "screening_story": re.compile(r"^\s*-\s*\*\*Screening story\*\*:\s*(?P<v>.+)"),
            "likely_day_to_day": re.compile(r"^\s*-\s*\*\*Likely day-to-day\*\*:\s*(?P<v>.+)"),
            "prestige_trap_risk": re.compile(r"^\s*-\s*\*\*Prestige-trap risk\*\*:\s*(?P<v>.+)"),
        }
        list_field_res = {
            "alignment_matrix": re.compile(r"^\s*-\s*\*\*Evidence alignment\*\*:\s*$"),
            "gaps": re.compile(r"^\s*-\s*\*\*Gaps\*\*:\s*$"),
        }
        item_re = re.compile(r"^\s+-\s+(?P<item>.+)$")
        found_via_re = re.compile(
            r"^\s*-\s*\*\*Found via\*\*:\s*\[[^]]+\]\((?P<url>[^)]+)\)"
            r"(?:\s+\((?P<date>\d{4}-\d{2}-\d{2})\))?"
        )
        for path in org_page_paths(self.root):
            # Prefer the H1 of the file (write_org_page writes "# Org"),
            # fall back to the filename stem (with collapse for double/triple
            # underscores introduced by the safe_filename sanitizer).
            org_name = ""
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines()[:5]:
                if line.startswith("# "):
                    org_name = line[2:].strip()
                    break
            if not org_name:
                import re as _re
                stem = _re.sub(r"_+", " ", path.stem).strip()
                org_name = stem
            # The organisation's **recorded kind**, read once per page: the marker
            # is the decision (`lead_identity.recorded_entity_kind`), and carrying
            # it on the entry lets a listing row say `AGENCY` without a file read
            # and without falling back to a rule the page has already overruled.
            from lead_identity import recorded_entity_kind

            page_kind = recorded_entity_kind(text)

            def _entry(fields: dict) -> "WikiEntry":
                """**One construction**, because there are two flushes.

                The parser emits a block when the *next* heading arrives and again
                for the trailing one, and the two `WikiEntry(...)` calls had already
                drifted: the kind was added to the first and not the second, so the
                **last block of every page** carried no kind — caught by a fixture
                with a single block, which is exactly the shape that hides it
                (2026-10-15). One builder, so a new field cannot be added to one
                path only.
                """
                return WikiEntry(
                    org_name=org_name,
                    entity_kind=page_kind,
                    title=fields.get("title", ""),
                    url=fields.get("url") or None,
                    original_url=fields.get("original_url"),
                    location=fields.get("location") or None,
                    score=float(fields.get("score") or 0.0),
                    decision=fields.get("decision", ""),
                    reasoning=fields.get("reasoning") or "(from wiki/orgs)",
                    source=fields.get("source", ""),
                    alignment_matrix=fields.get("alignment_matrix"),
                    gaps=fields.get("gaps"),
                    application_angle=fields.get("application_angle", ""),
                    screening_story=fields.get("screening_story", ""),
                    likely_day_to_day=fields.get("likely_day_to_day", ""),
                    prestige_trap_risk=fields.get("prestige_trap_risk", ""),
                    date=fields.get("date", ""),
                    fit_score=fields.get("fit_score"),
                    reach_score=fields.get("reach_score"),
                    flourish_score=fields.get("flourish_score"),
                    combined_score=fields.get("combined_score"),
                    recommended_action=fields.get("recommended_action"),
                    discovery_url=fields.get("discovery_url"),
                    discovery_date=fields.get("discovery_date"),
                )

            current: dict | None = None
            for line in text.splitlines():
                sec = section_re.match(line.rstrip())
                if sec:
                    if current and current.get("decision") in decisions:
                        out.append(_entry(current))
                    current = {"date": sec.group("date"), "title": sec.group("title").strip()}
                    continue
                if current is None:
                    continue
                m = decision_re.match(line)
                if m:
                    current["decision"] = m.group(1)
                    if m.group("score"):
                        current["score"] = float(m.group("score"))
                    continue
                m = action_re.match(line)
                if m:
                    current["recommended_action"] = m.group("action").strip()
                    continue
                m = fit_re.match(line)
                if m:
                    current["fit_score"] = float(m.group("score"))
                    continue
                m = reach_re.match(line)
                if m:
                    current["reach_score"] = float(m.group("score"))
                    continue
                m = flourish_re.match(line)
                if m:
                    current["flourish_score"] = float(m.group("score"))
                    continue
                m = combined_re.match(line)
                if m:
                    current["combined_score"] = float(m.group("score"))
                    continue
                m = url_re.match(line)
                if m:
                    candidate = m.group("url").strip()
                    # `- **URL**: _not available_` is the documented shape for an
                    # entry with no usable link (a provider can return a
                    # non-public host). It must not be stored as a URL called
                    # "_not" — that is how a blanked link would resurrect itself
                    # in the rebuilt index.
                    if candidate.startswith(("http://", "https://")):
                        current["url"] = candidate
                    # A resolved lead renders as "<preferred> (via <slug>)"; the
                    # parsed URL is the preferred one, so record it as such. The
                    # two being equal renders a plain URL line, never "(via)"
                    # pointing at itself.
                    if candidate.startswith(("http://", "https://")) and "(via " in line:
                        current["original_url"] = current["url"]
                    continue
                m = loc_re.match(line)
                if m:
                    current["location"] = m.group("loc").strip()
                    continue
                m = source_re.match(line)
                if m:
                    current["source"] = m.group("src").strip()
                    continue
                m = reasoning_re.match(line)
                if m:
                    current["reasoning"] = m.group("value").strip()
                    current["_list"] = None
                    continue
                m = item_re.match(line)
                if m and current.get("_list"):
                    current.setdefault(current["_list"], []).append(m.group("item").strip())
                    continue
                matched_list = next((name for name, rx in list_field_res.items()
                                     if rx.match(line)), None)
                if matched_list:
                    current["_list"] = matched_list
                    current.setdefault(matched_list, [])
                    continue
                matched_text = next((name for name, rx in text_field_res.items()
                                     if rx.match(line)), None)
                if matched_text:
                    current[matched_text] = text_field_res[matched_text].match(
                        line).group("v").strip()
                    current["_list"] = None
                    continue
                if line.strip() and not line.startswith("  "):
                    current["_list"] = None
                m = found_via_re.match(line)
                if m:
                    current["discovery_url"] = m.group("url").strip()
                    current["discovery_date"] = (m.group("date") or "").strip()
                    continue
            # Flush trailing section — through the same builder as the others.
            if current and current.get("decision") in decisions:
                out.append(_entry(current))
        return out

    def _latest_report_rel(self) -> Optional[str]:
        """Return the relative path of the most recent report (by H1 date)."""
        import re
        if not self.reports_dir.is_dir():
            return None
        best: Optional[tuple[str, Path]] = None
        date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
        for p in self.reports_dir.glob("*.md"):
            try:
                first = p.read_text(encoding="utf-8").splitlines()[:3]
            except Exception:
                continue
            for line in first:
                m = date_re.search(line)
                if m:
                    date = m.group(1)
                    if best is None or date > best[0]:
                        best = (date, p)
                    break
        if best is None:
            # Fall back to the file with the latest mtime.
            files = sorted(self.reports_dir.glob("*.md"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            return f"reports/{files[0].name}" if files else None
        return f"reports/{best[1].name}"

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Sanitise a string into a safe markdown filename stem.

        Rules:
        - The name is transliterated first (`_transliterate`), so an accented
          letter survives as a letter: "Deutsche Börse" -> "Deutsche_Borse",
          "Dassault Systèmes" -> "Dassault_Systemes", "Straße" -> "Strasse".
          An accent is never dropped — the stem is the org's identity in the
          wiki, and dropping the letter named the page after the gap
          ("Deutsche_B_rse", "Cond_Nast").
        - Letters / digits / dot / dash / underscore stay.
        - Everything else — a space, slashes, parens, punctuation, and a
          character with no ASCII form (CJK, an emoji) — is replaced with `_`.
        - Runs of `_` are collapsed to one.
        - Leading/trailing `_`, `.` and `-` are stripped.
        - Empty result is replaced with `x` (a defensive default).
        - Capped at 120 bytes (`filename_limits.MAX_FILENAME_BYTES`):
          eCryptfs stores at most 143 bytes per component (measured: 143
          accepted, 144 ENAMETOOLONG), and the `.md` suffix sits outside
          this stem.

        Why ASCII-only: filesystem behaviour for unicode paths varies
        across macOS / Linux / WSL, and unicode confusable chars (e.g.
        "Zūm" / "Zum" look identical but hash differently) silently
        produce duplicate org pages. ASCII fixes both, and because
        transliteration folds the accented spelling and the plain one onto
        the same stem, it fixes them without losing the letters.
        """
        if (name or "").strip() == UNATTRIBUTED_ORG:
            # The junk bucket's page name is a sentinel, not a company: the
            # leading `_` keeps it out of the alphabet and must survive, or a
            # freshly minted bucket lands beside the existing one and the audit
            # sees two pages for one name.
            return UNATTRIBUTED_ORG
        n = _transliterate(name or "x")
        # Keep alnum + a small set of safe punctuation; replace the rest.
        n = re.sub(r"[^A-Za-z0-9._-]+", "_", n)
        # Collapse runs of underscores introduced by adjacent unsafe chars.
        n = re.sub(r"_+", "_", n)
        n = n.strip("._-") or "x"
        return clip_stem(n)

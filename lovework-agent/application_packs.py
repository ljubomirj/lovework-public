"""Turn actionable LoveWork leads into reviewable, pre-application dossiers.

An application pack is deliberately *not* an application.  It is a local
evidence bundle created for a GO lead so LJ can review the advert, provenance,
and assessment before deciding whether to apply.  The explicit PREPARED status
keeps the history/outcome scanner from mistaking research for a submission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import config
from enrichment import ENRICHMENT_VERSION
from job_registry import _job_hash
from wiki_store import WikiEntry

logger = logging.getLogger(__name__)

SEPARATOR = "=" * 72
PREPARED_MARKER = "LoveWork status: PREPARED — not submitted"
#: What a pack says when only the scaffold exists — LJ, 2026-10-18: *"We
#: should not put `LoveWork status: PREPARED — not submitted` where the
#: application is NOT PREPARED"* (the Together AI case: the advert was gone,
#: no CV, no letter, no advert text — and the front page claimed PREPARED).
#: `PREPARED` is **earned** by `pack_completion` when steps 1–3 land; until
#: then the honest status is CUT. Readers that ask "is this a LoveWork pack?"
#: match the `LoveWork status:` prefix, not one vocabulary word.
CUT_MARKER = "LoveWork status: CUT — scaffold only, not yet prepared"
RECENT_APPLICATION_DAYS = 180
NON_ROLE_TITLES = {
    "london, uk",
    "full-time",
}


@dataclass(frozen=True)
class PackResult:
    entry: WikiEntry
    status: str  # created, existing, skipped_recent_application
    path: Path | None
    reason: str


def _slug_part(value: str, max_length: int) -> str:
    value = re.sub(r"\s+", "_", (value or "").strip())
    value = re.sub(r"[^A-Za-z0-9._-]+", "", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return (value or "x")[:max_length]


def pack_slug(when: date, org: str, title: str) -> str:
    """Return LJ's explicit YYYYMMDD-Company-Role-LoveWork pack name."""
    return (
        f"{when.strftime('%Y%m%d')}-{_slug_part(org, 40)}-"
        f"{_slug_part(title, 60)}-LoveWork"
    )


def _principal_cases_root(principal: str = "lj") -> Path:
    """LoveWork's principal-owned case root.

    Ownership follows the workflow that created the case (docs/21, 07-30
    restructure): ``-LoveWork`` packs are RW in lovework's own repo under
    ``state/<principal>/applications/``, and the parent repo's unified
    ``applications/`` view mirrors each pack as a read-only symlink down.
    Fall back to the unified view only when the principal has no state dir at
    all (pre-migration / non-LJ setups): a state dir that merely lacks
    ``applications/`` belongs to a principal whose packs must not be
    materialised as the *first* principal's data — the root is returned
    uncreated and ``prepare_go_cases`` makes it on demand.
    """
    principal_root = config.STATE_DIR / principal.lower()
    if principal_root.is_dir():
        return principal_root / "applications"
    return config.APPLICATIONS_DIR


#: Mirror packs into the parent repo's `applications/` view? **Off since
#: 2026-10-14.** LJ: *"Let us not create links in the upper directory
#: ~/LJ-work-2026/applications/ pointing down towards lovework/ for the GO-es: the
#: principal will not be applying to most of the GO-s. Let us leave the upper
#: directory … completely to the principal: if they are applying for, then they
#: will create the link themselves. So no automatic linking any more."*
#:
#: 771 such symlinks had accumulated beside his 197 real case directories, which is
#: exactly the mixing he is asking to stop: the parent `applications/` is his
#: workspace, and a GO pack he never applies to does not belong in it.
MIRROR_PACKS = os.getenv("LOVEWORK_MIRROR_PACKS", "0").strip().lower() \
    in ("1", "true", "yes", "on")


def _mirror_pack_in_unified_view(pack_dir: Path) -> None:
    """Expose a state-owned pack in the parent repo's applications/ view.

    **No-op unless `LOVEWORK_MIRROR_PACKS=1`** (see the constant above): the
    parent directory belongs to the principal. Kept as a function so the
    relationship is one line to restore rather than a design to re-derive, and so
    every call site keeps a single name for it.
    """
    if not MIRROR_PACKS:
        return
    try:
        link_path = config.APPLICATIONS_DIR / pack_dir.name
        if link_path.exists() or link_path.is_symlink():
            return  # already mirrored
        config.APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(os.path.relpath(pack_dir, config.APPLICATIONS_DIR))
        logger.info("[packs] Mirrored %s -> %s", link_path, pack_dir)
    except OSError as exc:
        logger.warning("[packs] Could not mirror pack %s in unified view: %s", pack_dir, exc)


def relink_packs(principal: str = "lj") -> dict[str, int]:
    """Ensure every state-owned pack of `principal` is mirrored in the unified view.

    Additive and idempotent: it creates only the relative symlinks the convention
    requires (`applications/<pack>` -> `../lovework/state/<p>/applications/<pack>`)
    and never removes or rewrites anything. Needed after merging a crawl from
    another host — the packs travel inside lovework's repo, but the parent repo's
    symlinks do not.

    Only the unified view's owner (LJ) is mirrored: `state/vj|kj/applications/`
    holds those principals' packs and must never appear in LJ's human view.
    """
    counts = {"packs": 0, "created": 0, "present": 0, "other_entries": 0}
    if principal.lower() != "lj":
        logger.info("[packs] %s packs are not mirrored (the unified view is LJ's)", principal)
        return counts
    root = _principal_cases_root(principal)
    if not root.is_dir():
        return counts
    for pack in sorted(root.iterdir()):
        if pack.is_symlink() or not pack.is_dir():
            continue                      # the up-links to manual cases, or loose files
        if "-LoveWork" not in pack.name:
            counts["other_entries"] += 1
            continue
        counts["packs"] += 1
        link = config.APPLICATIONS_DIR / pack.name
        if link.is_symlink() or link.exists():
            counts["present"] += 1
            continue
        _mirror_pack_in_unified_view(pack)
        if link.is_symlink() or link.exists():
            counts["created"] += 1
    return counts


def _normalise_words(value: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (value or "").lower()))
    return words - {
        "a", "an", "and", "at", "for", "in", "of", "or", "the", "to",
        "senior", "junior", "staff", "lead", "principal", "member",
    }


def _titles_similar(left: str, right: str) -> bool:
    a, b = _normalise_words(left), _normalise_words(right)
    if not a or not b:
        return False
    return a <= b or b <= a or len(a & b) / len(a | b) >= 0.6


def _title_requires_repair(title: str) -> bool:
    """Avoid creating a permanent case identity from a location or bare URL."""
    value = (title or "").strip().lower()
    if not value or value.startswith(("http://", "https://", "www.")):
        return True
    if value in NON_ROLE_TITLES:
        return True
    if value.startswith("remote") and any(word in value for word in ("engineer", "scientist", "researcher")):
        return False
    return value.startswith(("remote", "remote-first", "seattle/"))


def org_key(org: str) -> str:
    """The organisation as `pack_slug` writes it: truncated, then compacted.

    **The truncation is the point.** `pack_slug` cuts the organisation to 40
    characters, so the directory name can only ever hold that much of it. A
    matcher that requires the *whole* organisation therefore never matches its
    own packs for any long name — measured 2026-10-13: `Advanced Machine
    Intelligence (AMI Labs, Yann LeCun)` compacted to 48 characters against a
    40-character slug, so every day's run cut a fresh pack for the same advert
    (5 AMI packs, one per day, all carrying `Advert identity:
    4468d68edc015eef`), and the loop spent its actions re-investigating them.
    """
    return re.sub(r"[^a-z0-9]", "", _slug_part(org, 40).lower())


def _matches_org(directory_name: str, org: str) -> bool:
    """Is this pack directory about `org`?

    Compared on the truncated key the slugger actually writes, with the whole
    organisation accepted as well so an older, untruncated directory still
    matches.
    """
    compact_dir = re.sub(r"[^a-z0-9]", "", directory_name.lower())
    if not compact_dir:
        return False
    truncated = org_key(org)
    whole = re.sub(r"[^a-z0-9]", "", org.lower())
    return bool((truncated and truncated in compact_dir)
                or (whole and whole in compact_dir))


#: The dossier's extension, and the one it used to have.
#:
#: LJ, 2026-10-15: *"Think .txt is overly restrictive these days - it should be
#: markdown .md. So we can put links and other more rich content. Just plain .txt
#: does not suffice."* He is right, and the file's own content made the case: it
#: carries `Discovery URL` and `Primary advert URL` as bare text, so the two links a
#: reader most wants to open are the two they cannot click.
DOSSIER_SUFFIX = ".md"
LEGACY_DOSSIER_SUFFIX = ".txt"

#: Names a pack directory holds that are **not** the dossier — the fixed-name front
#: page and the tailored material. The dossier is the file that matches the case
#: directory's own name, which is the convention a reader can rely on.
_NON_DOSSIER_NAMES = frozenset({
    "application_pack.md", "cover-letter.md", "cv-tailored.md", "answers.md",
    "email-draft.md", "index.md", "README.md",
})


def dossier_path(directory: Path) -> Path | None:
    """**The** dossier file of a case directory, or None — one authority.

    Prefers `<directory name>.md`, then the legacy `<directory name>.txt`, then any
    remaining `*.txt` (packs cut before the name convention settled). Every reader
    asks this rather than globbing: seven modules used to glob `*.txt` and one of
    them said so in a comment — *"`next(glob("*.txt"))` picks arbitrarily when a
    pack holds more than one"*. A glob is not an identity.
    """
    stem = directory.name
    for suffix in (DOSSIER_SUFFIX, LEGACY_DOSSIER_SUFFIX):
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    for candidate in sorted(directory.glob(f"*{LEGACY_DOSSIER_SUFFIX}")):
        if candidate.name not in _NON_DOSSIER_NAMES:
            return candidate
    return None


#: Places where a link is a **re-post**, never the employer's own page.
#: Deliberately narrower than tick's `_AGGREGATOR_MARKERS`, which also names
#: employer ATS hosts (greenhouse.io, lever.co, ashbyhq.com, workable.com,
#: workday): those serve the real advert and are what `resolve_advert` looks for.
MARKETPLACE_MARKERS = (
    "linkedin.com", "indeed.", "totaljobs.com", "cwjobs.", "digitaljobs.com",
    "wellfound.com", "otta.com", "workinstartups.com", "glassdoor.",
    "reed.co.uk", "cv-library.", "monster.", "jobsite.", "adzuna.",
)


def advert_link_is_board(url: str) -> bool:
    """True when a pack's link points at a re-post site, not the employer's page.

    A board link is not an advert link: the advert it shows is a mirror, often a
    redirect stub — 873 characters of a LinkedIn job-alert page scored a pack
    7.1 → 0.0 on 2026-11-03. Such a pack needs `resolve_advert` again, and a
    review must not score the page.
    """
    if not url:
        return False
    host = urlparse(str(url)).netloc.lower()
    return any(marker in host for marker in MARKETPLACE_MARKERS)


def _case_text_path(directory: Path) -> Path | None:
    """Back-compat alias: the dossier, wherever its extension has got to."""
    return dossier_path(directory)


def principal_notes_path(directory: Path) -> Path | None:
    """The principal's own file in a case directory, when it is not the dossier.

    LJ, 2026-10-15: he pasted an advert into `<case>.txt` in a pack whose generated
    dossier is `<case>.md`. Both conventions are legitimate — **LoveWork writes
    markdown, LJ writes plain text** — so a reader that saw only one of them would
    have been blind to whichever it did not expect. His file is never rewritten: it
    is the one document in the pack that is not ours.
    """
    candidate = directory / f"{directory.name}{LEGACY_DOSSIER_SUFFIX}"
    if not candidate.is_file():
        return None
    if dossier_path(directory) == candidate:
        return None                  # it *is* the dossier (an unconverted pack)
    return candidate


def _is_prepared(directory: Path) -> bool:
    text_path = _case_text_path(directory)
    if not text_path:
        return directory.name.endswith("-LoveWork")
    try:
        return PREPARED_MARKER in text_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


#: Markers that mean **the principal applied**, as opposed to "we prepared a pack".
_APPLIED_TEXT_MARKERS = ("LoveWork status: SUBMITTED", "LoveWork outcome: applied",
                         "LoveWork outcome: rejection")


def _is_applied(directory: Path) -> bool:
    """Did the principal actually apply here? **A pack is not an application.**

    The guard this replaces consulted `_is_prepared` and — inverted — treated an
    *unprepared* pack as a "recent submitted application", skipping every other role at
    that organisation for 45 days. Measured 2026-10-20, on the MAYBE tier LJ had just
    asked for: a GO pack cut minutes earlier for Acme/Engineer made Acme/Analyst skip with
    *"recent submitted application: 20261020-Acme-Engineer-LoveWork"* — a submission that
    did not exist. The same shape as reading a mention as a receipt.
    """
    # **A directory the principal made by hand is his own application.** LoveWork only
    # ever creates `*-LoveWork`; a case without that suffix was built by him for a real
    # submission (the 2026-07-07 Example AI case records *"Applied through employer
    # site."* in prose). That is a different fact from "a pack exists", which is what the
    # old predicate actually measured — and measured backwards, since it skipped
    # *prepared* packs and treated unprepared ones as submissions.
    if not directory.name.endswith("-LoveWork"):
        return True
    text_path = _case_text_path(directory)
    if not text_path:
        return False
    try:
        text = text_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(marker in text for marker in _APPLIED_TEXT_MARKERS)


def _directory_date(directory: Path) -> date | None:
    match = re.match(r"(?P<date>\d{8})-", directory.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError:
        return None


def _directory_role(directory: Path, org: str) -> str:
    remainder = re.sub(r"^\d{8}-", "", directory.name)
    org_part = _slug_part(org, 40)
    if remainder.lower().startswith(org_part.lower() + "-"):
        remainder = remainder[len(org_part) + 1:]
    return remainder.removesuffix("-LoveWork").replace("_", " ")


def _enrichment_for(url: str) -> dict[str, str]:
    """Load cached source and primary-page text without performing a new fetch."""
    if not url.startswith(("http://", "https://")):
        return {}
    digest = hashlib.sha256(f"{ENRICHMENT_VERSION}:{url}".encode()).hexdigest()[:24]
    path = config.CACHE_DIR / "enrichment" / f"{digest}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "original_description": str(value.get("original_description") or ""),
        "primary_text": str(value.get("primary_text") or ""),
        "primary_url": str(value.get("primary_url") or url),
        "primary_fetch_method": str(value.get("primary_fetch_method") or ""),
        "primary_fetched_at": str(value.get("primary_fetched_at") or ""),
    }


#: Where a block written before the 2026-10-15 markdown conversion ends: the `=`
#: rule that separated sections in the `.txt` dossiers.
LEGACY_BLOCK_END = "\n====="

#: …and where the same block ends now: the next section heading.
MARKDOWN_BLOCK_END = "\n## "


def block_end(text: str, start: int) -> int:
    """The index where the block beginning at `start` ends, or -1 when it has none.

    One authority for "where does this block stop", because two writers fill a
    block in a dossier — `tick._write_investigation` (the reserved
    `NOT YET INVESTIGATED` block) and `pack_completion.replace_block` (the
    workflow's diligence) — and both looked only for the `.txt` era's `=` rule.
    After the 2026-10-15 conversion to markdown neither could find an end, so
    both refused **in silence**: 238 dossiers kept `Status: NOT YET
    INVESTIGATED` while a completion could report an investigation it had not
    written, and the tick's investigated packs (the other shape, `## Entity`
    subsections under the section heading) got a second diligence block inserted
    beside the first (all measured 2026-10-16).

    The markers are searched from `start`, so a `=` rule *before* the block does
    not count; the earliest of the two endings wins, which keeps the `.txt`
    behaviour exactly as it was.
    """
    ends = [index for index in (text.find(LEGACY_BLOCK_END, start),
                                text.find(MARKDOWN_BLOCK_END, start))
            if index >= 0]
    return min(ends) if ends else -1


#: A markdown link, `[label](target)` — the shape a URL takes in a dossier after the
#: 2026-10-15 conversion to markdown.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def markdown_link_target(value: str) -> str:
    """`value` with a wrapping markdown link unwrapped: `[u](u)` → `u`.

    A URL cell holds one shape or the other — bare in a `.txt`-era file, a markdown
    link in a converted one — and a reader that does not unwrap reads the *markup*
    as the URL. `pack_completion` asked the fetcher for `[https://…](https://…)`
    and the completion died with *"Request URL is missing an 'http://' or
    'https://' protocol"*; `refresh_pack_markdown` had copied the same markup into
    the packs' front pages, which is where the reader got it from (both measured
    2026-10-16, the AMI Scientist pack). One authority, so the two cannot drift
    apart again.
    """
    stripped = (value or "").strip()
    match = _MARKDOWN_LINK.fullmatch(stripped)
    return match.group(1).strip() if match else stripped


def _section(lines: list[str], title: str) -> None:
    """A markdown heading, because the dossier is markdown now (2026-10-15).

    It was `====` separators and a bare title — plain text that no renderer knows
    what to do with. The dashboard renders markdown, so headings and links are what
    make the dossier readable rather than merely present.
    """
    lines.extend(["", f"## {title}", ""])


def _pack_text(entry: WikiEntry, advert_hash: str, evidence: dict[str, str]) -> str:
    """Render a self-contained initial dossier, preserving all crawl evidence."""
    lines = [CUT_MARKER]
    _section(lines, "LoveWork case identity")
    alignment = [f"- {item}" for item in entry.alignment_matrix] or ["- none retained"]
    gaps = [f"- {item}" for item in entry.gaps] or ["- none retained"]
    lines.extend([
        f"Created: {date.today().isoformat()}",
        f"Advert identity: {advert_hash}",
        f"Organisation: {entry.org_name}",
        f"Position: {entry.title}",
        f"Initial decision: {entry.decision} ({entry.score:.1f}/10)",
        f"Recommended action: {entry.recommended_action or 'unspecified'}",
        f"Location: {entry.location or 'unspecified'}",
        "",
    ])
    _section(lines, "Crawl provenance")
    def _link(url: str) -> str:
        """A URL a reader can click — the whole point of the markdown move."""
        return f"[{url}]({url})" if url.startswith("http") else url

    lines.extend([
        f"Source: {entry.source or 'unspecified'}",
        f"Discovery URL: {_link(entry.discovery_url) if entry.discovery_url else 'not retained'}",
        f"Discovery date: {entry.discovery_date or 'not retained'}",
        f"Primary advert URL: {_link(entry.url) if entry.url else 'not retained'}",
        f"First seen: {entry.first_seen or 'not retained'}",
        f"Lifecycle: {entry.lifecycle_status or 'not retained'}",
        f"Primary evidence: {entry.primary_content_hash or 'not retained'}",
        f"Primary fetch: {entry.primary_fetch_method or evidence.get('primary_fetch_method') or 'not retained'}",
        f"Primary fetched: {entry.primary_fetched_at or evidence.get('primary_fetched_at') or 'not retained'}",
        "",
    ])
    _section(lines, "Advert material captured by LoveWork")
    source_text = (
        entry.advert_excerpt
        or evidence.get("original_description")
        or "No source excerpt retained for this lead."
    )
    primary_text = evidence.get("primary_text") or "No primary-page extract retained for this lead."
    lines.extend(["Original source excerpt:", source_text, "", "Primary advert extract:", primary_text, ""])
    _section(lines, "LoveWork assessment")
    lines.extend([
        f"Fit / reach / flourish: {entry.fit_score if entry.fit_score is not None else '?'} / "
        f"{entry.reach_score if entry.reach_score is not None else '?'} / "
        f"{entry.flourish_score if entry.flourish_score is not None else '?'}",
        f"Reasoning: {entry.reasoning or 'not retained'}",
        "",
        "Evidence alignment:",
        *alignment,
        "",
        "Gaps:",
        *gaps,
        "",
        f"Application angle: {entry.application_angle or 'not retained'}",
        f"Screening story: {entry.screening_story or 'not retained'}",
        f"Likely day-to-day: {entry.likely_day_to_day or 'not retained'}",
        f"Prestige-trap risk: {entry.prestige_trap_risk or 'not retained'}",
        "",
    ])
    _section(lines, "Company, hiring, and work reality")
    lines.extend([
        "Status: NOT YET INVESTIGATED",
        "(A GO pack is NOT ready to send until this block is filled — LJ, 2026-10-06;",
        "docs/41 C8. The 07:00 pack job owns the investigation: legal entity, named",
        "people, product, hiring evidence, funding/runway evidence, likely daily",
        "collaborators, risks, sources, confidence, and questions.)",
        "",
    ])
    _section(lines, "Next action")
    lines.extend([
        "- [ ] Review advert and fit",
        "- [ ] Investigate company/hiring reality (MANDATORY — blocks ready; LJ 2026-10-06)",
        "- [ ] Tailor CV / application material",
        "- [ ] Submit (then replace the status line above with SUBMITTED and date)",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def prepare_go_cases(
    entries: Iterable[WikiEntry],
    *,
    cases_root: Path | None = None,
    principal: str = "lj",
    today: date | None = None,
    only_new: bool = True,
    dry_run: bool = False,
    decisions: tuple[str, ...] = ("GO",),
) -> list[PackResult]:
    """Create idempotent PREPARED packs for eligible listings.

    ``only_new`` is used by normal pipeline runs.  Backfill tooling passes
    False to prepare an explicit historical GO report once.

    ``decisions`` is **which verdicts get a pack** — `("GO",)` by default, and
    `("GO", "MAYBE")` since LJ, 2026-10-20: *"Let's make the MAYBE-s also prepared for
    application."* It was hard-coded to GO inside the loop, so a MAYBE entry was silently
    skipped however it arrived — the kind of filter that makes a caller believe it asked.
    """
    root = cases_root or _principal_cases_root(principal)
    current_date = today or date.today()
    existing_dirs = [path for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    results: list[PackResult] = []

    import retire as _retire

    retired = _retire.retired_keys(principal, state_dir=config.STATE_DIR)
    for entry in entries:
        if entry.decision not in decisions:
            continue
        if _retire.is_retired(entry.org_name, entry.title, retired):
            # A retired pack is closed and in `gone/`: cutting a new one here
            # would reopen a door the principal closed (docs/45).
            results.append(PackResult(entry, "existing", None,
                                      "retired: the pack is in gone/"))
            continue
        if only_new and entry.lifecycle_status != "new":
            continue
        advert_hash = _job_hash(entry.org_name, entry.title, entry.url)
        matching_dirs = [path for path in existing_dirs if _matches_org(path.name, entry.org_name)]
        existing_pack = None
        for directory in matching_dirs:
            text_path = _case_text_path(directory)
            if text_path:
                try:
                    if f"Advert identity: {advert_hash}" in text_path.read_text(encoding="utf-8", errors="ignore"):
                        existing_pack = directory
                        break
                except OSError:
                    pass
        if existing_pack is not None:
            results.append(PackResult(entry, "existing", existing_pack, "same advert already prepared"))
            continue

        recent_application = None
        for directory in matching_dirs:
            # Only a real submission blocks a sibling role (see `_is_applied`).
            if not _is_applied(directory):
                continue
            created = _directory_date(directory)
            if created is None or (current_date - created).days > RECENT_APPLICATION_DAYS:
                continue
            if _titles_similar(entry.title, _directory_role(directory, entry.org_name)) or (current_date - created).days <= 45:
                recent_application = directory
                break
        if recent_application is not None:
            results.append(PackResult(
                entry, "skipped_recent_application", recent_application,
                f"recent submitted application: {recent_application.name}",
            ))
            continue

        if _title_requires_repair(entry.title):
            results.append(PackResult(
                entry, "needs_title_repair", None,
                "crawler retained a location, employment label, or URL instead of a role title",
            ))
            continue

        slug = pack_slug(current_date, entry.org_name, entry.title)
        directory = root / slug
        if directory.exists():
            results.append(PackResult(
                entry, "existing", directory,
                "a same-day pack directory already exists for this organisation and title",
            ))
            continue
        if dry_run:
            results.append(PackResult(entry, "created", directory, "would prepare for review; not submitted"))
            continue
        directory.mkdir(parents=True, exist_ok=False)
        evidence = _enrichment_for(entry.url)
        text_path = directory / f"{slug}{DOSSIER_SUFFIX}"
        text_path.write_text(_pack_text(entry, advert_hash, evidence), encoding="utf-8")
        # …and the fixed-name front page, so the case is openable without
        # knowing its slug (and servable by the dashboard).
        try:
            write_pack_markdown(entry, advert_hash, evidence, directory, principal)
        except Exception as exc:              # never lose the dossier over this
            logger.warning("pack markdown failed for %s: %s", slug, exc)
        existing_dirs.append(directory)
        if cases_root is None:
            # Production path: state-owned pack, mirrored into the parent
            # repo's unified applications/ view as a symlink (docs/21).
            _mirror_pack_in_unified_view(directory)
        results.append(PackResult(entry, "created", directory, "prepared for review; not submitted"))
    return results


# ── the pack's front page (LJ, 2026-10-09) ────────────────────────────────
#
# The pack already held the dossier (.txt), the diligence note (.md) and — once
# the 07:00 pack job works it — the tailored CV and letter. What was missing is
# a FIXED-NAME entry point a reader (and the dashboard) can open without knowing
# the case slug, and which states plainly what is present and what is not: the
# principal should start from the pack, never from zero.

PACK_INDEX_NAME = "application_pack.md"

#: Hosts whose URL in the advert is an application form rather than a listing.
_APPLY_HOST_RE = re.compile(
    r"https?://[^\s)\]]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|"
    r"smartrecruiters\.com|myworkdayjobs\.com|recruitee\.com|bamboohr\.com|"
    r"teamtailor\.com|jobs\.eu|/apply|/application)",
    re.I)

_SECTION_TITLES = {
    "cv": ("cv-tailored.md", "Tailored CV"),
    "letter": ("cover-letter.md", "Cover letter"),
    "answers": ("answers.md", "Application answers"),
}


def _sibling(directory: Path, name: str) -> Path | None:
    path = directory / name
    return path if path.is_file() else None


def _investigation(directory: Path) -> Path | None:
    for path in sorted(directory.glob("company-investigation*.md")):
        return path
    return None


def _case_stub(directory: Path) -> Path | None:
    """Back-compat alias for `dossier_path`."""
    return dossier_path(directory)


def _advert_excerpt(entry: WikiEntry, evidence: dict, limit: int = 2500) -> str:
    """The fetched advert text we hold, or an honest absence."""
    text = (evidence.get("primary_text") or entry.advert_excerpt or "").strip()
    if not text:
        return "_No advert text was retained for this lead._"
    body = text[:limit].rstrip()
    if len(text) > limit:
        body += "\n\n_…truncated; the full extract is in the case dossier._"
    # Quoted for the same reason as a lead page: the advert's own markdown
    # headings must not become the pack page's headings.
    return "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())


def _apply_link(entry: WikiEntry, evidence: dict) -> str:
    """The application form, when the advert itself names one."""
    blob = f"{evidence.get('primary_text') or ''}\n{entry.advert_excerpt or ''}"
    m = _APPLY_HOST_RE.search(blob)
    if m:
        return m.group(0).rstrip(".,);")
    return ""


def pack_markdown(entry: WikiEntry, advert_hash: str, evidence: dict,
                  directory: Path, principal: str = "lj") -> str:
    """The pack's front page: what this case holds, and what it still needs."""
    status = CUT_MARKER
    notes = principal_notes_path(directory)
    stub = _case_stub(directory)
    if stub is not None:
        head = stub.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
        if head and head[0].strip():
            status = head[0].strip()
    investigated = _investigation(directory)
    cv, letter, answers = (_sibling(directory, name) for name, _ in
                           (_SECTION_TITLES["cv"], _SECTION_TITLES["letter"],
                            _SECTION_TITLES["answers"]))

    lines = [
        f"# {entry.org_name} — {entry.title}",
        "",
        f"**{status}**",
        "",
        f"- **Decision**: {entry.decision} ({entry.score:.1f}/10) · "
        f"{entry.recommended_action or 'no action recorded'}"
        + (f" · {entry.location}" if entry.location else ""),
        f"- **Advert identity**: `{advert_hash}`",
        f"- **Case directory**: `{directory.name}`",
        (f"- **Dossier**: [{stub.name}]({stub.name})" if stub else ""),
        # **The principal's own file is part of the pack.** He writes `.txt` and
        # LoveWork writes `.md` (2026-10-15), so a pack can hold both — and the one
        # the reader did not expect must not be the one that goes unmentioned. LJ
        # pasted the real PostHog advert into exactly this file while every reader
        # was looking at the dossier beside it.
        (f"- **Notes from the principal**: [{notes.name}]({notes.name})"
         if (notes := principal_notes_path(directory)) else ""),
        "",
        "## 1. The advert",
        "",
        f"- **Link**: {entry.url}" if entry.url else "- **Link**: _not retained_",
    ]
    if entry.original_url and entry.original_url != entry.url:
        lines.append(f"- **Preferred link**: {entry.original_url}")
    if entry.discovery_url:
        lines.append(f"- **Found via**: [{entry.source}]({entry.discovery_url})"
                     + (f" ({entry.discovery_date})" if entry.discovery_date else ""))
    lines.extend([
        f"- **Fetched**: {entry.primary_fetched_at or 'not retained'}"
        f" via {entry.primary_fetch_method or 'unknown'}",
        "",
        _advert_excerpt(entry, evidence),
        "",
        "## 2. Applying",
        "",
    ])
    apply_url = _apply_link(entry, evidence)
    if apply_url:
        lines.append(f"- **Application form**: {apply_url}")
    else:
        lines.append("- **Application form**: not named in the advert — apply from the advert link above")
    lines.append(f"- **Answers**: [{answers.name}]({answers.name})" if answers
                 else "- **Answers**: _not prepared yet_")
    # **How much of the principal is in this case** (LJ, 2026-10-20): "from PoV of
    # re-using what we already prepared … there is a huge difference between 1) we spent
    # significant time in the application, there is high principal SNR there … and 2) we
    # didn't apply". It sits with the material because that is the question it answers:
    # is what follows worth reading as *his* work?
    lines.extend(["", f"- **Principal work**: {principal_engagement(directory)}"])
    _outcome = principal_outcome(directory)
    if _outcome["outcome"] != "none":
        lines.append(f"- **Outcome**: {_outcome['outcome']}"
                     + (f" ({_outcome['basis']})" if _outcome["basis"] else ""))
    lines.extend(["", "## 3. Tailored material", ""])
    lines.append(f"- **Tailored CV**: [{cv.name}]({cv.name})" if cv
                 else "- **Tailored CV**: _not prepared yet_")
    lines.append(f"- **Cover letter**: [{letter.name}]({letter.name})" if letter
                 else "- **Cover letter**: _not prepared yet_")
    lines.extend(["", "## 4. Background", ""])
    if investigated is not None:
        lines.append(f"- **Company & hiring reality**: [{investigated.name}]({investigated.name})")
    else:
        lines.append("- **Company & hiring reality**: _not yet investigated_ "
                     "(the 07:00 pack job fills this block)")
    try:
        from wiki_store import WikiStore
        wiki_root = config.STATE_DIR / principal / "wiki"
        stem = WikiStore(root=wiki_root)._org_stem(entry.org_name)
        lines.append(f"- **Wiki page**: [/{principal}/wiki/orgs/{stem}.md]"
                     f"(/principals/{principal}/wiki/orgs/{stem}.md)")
    except Exception:
        pass
    lines.extend(["", "### Why this is a GO", ""])
    if entry.reasoning:
        lines.append(entry.reasoning)
    if entry.alignment_matrix:
        lines.extend(["", "**Evidence alignment**", ""])
        lines.extend(f"- {item}" for item in entry.alignment_matrix)
    if entry.gaps:
        lines.extend(["", "**Gaps**", ""])
        lines.extend(f"- {item}" for item in entry.gaps)
    for label, value in (("Application angle", entry.application_angle),
                         ("Screening story", entry.screening_story),
                         ("Likely day-to-day", entry.likely_day_to_day),
                         ("Prestige-trap risk", entry.prestige_trap_risk)):
        if value:
            lines.extend(["", f"**{label}**: {value}"])
    lines.extend(["", "---", "",
                  "_Generated by LoveWork. This is a dossier, not a submission — "
                  "the status line above is the authority._", ""])
    return "\n".join(line for line in lines if line is not None)


def write_pack_markdown(entry: WikiEntry, advert_hash: str, evidence: dict,
                        directory: Path, principal: str = "lj") -> Path:
    path = directory / PACK_INDEX_NAME
    path.write_text(pack_markdown(entry, advert_hash, evidence, directory, principal),
                    encoding="utf-8")
    return path


_URL_IN_FIELD_RE = re.compile(r"https?://[^\s)\]<>]+")


def _field_url(value: str) -> str:
    """The first URL in a case field, or "".

    `Primary advert URL` is written as a markdown link,
    `[https://…](https://…)`; the identity and the URL-only key need the raw
    form, or a pack's URL can never meet the wiki block's URL (Elman, 2026-11-04).
    """
    m = _URL_IN_FIELD_RE.search(value or "")
    return m.group(0).rstrip(".,;:") if m else ""


def pack_index(principal: str = "lj", *, root: Path | None = None) -> dict[str, Path]:
    """advert identity → pack directory, for linking packs from the wiki.

    Read once per index rebuild (the stubs carry `Advert identity:`), so the
    wiki can link the pack without knowing the case slug.

    `root` is the case root, when the caller holds it: the loop's decider acts on
    an injected `state/<p>/applications` (a test's temporary state, or a
    principal's tree named by its path) and must not fall back to
    `config.STATE_DIR/<p>` — that is the principal-blind-default class, where a
    read for one principal answers with another's packs.
    """
    index: dict[str, Path] = {}
    from lead_identity import advert_url_key

    root = Path(root) if root is not None else _principal_cases_root(principal)
    if not root.is_dir():
        return index
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        if is_superseded(directory):
            continue
        stub = _case_stub(directory)
        if stub is None:
            continue
        try:
            text = stub.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"(?m)^Advert identity: (\S+)\s*$", text)
        if m:
            index.setdefault(m.group(1), directory)
        # …and by the identity its own fields recompute to, so a pack filed
        # before a name was normalised is still found (16 lj packs from
        # 2026-07-19 carry a stored identity that no longer recomputes).
        fields = dict(re.findall(r"(?m)^([A-Za-z ]+): (.+)$", text))
        org = (fields.get("Organisation") or "").strip()
        title = (fields.get("Position") or "").strip()
        url = _field_url(fields.get("Primary advert URL") or "")
        if org and title:
            index.setdefault(_job_hash(org, title, url), directory)
            index.setdefault(_job_hash(org, title.removesuffix("🆕").strip(), url),
                             directory)
            # …and by org+URL: the advert's *link* is the one fact both sides
            # hold verbatim, so it survives a title that drifted between the pack
            # and the wiki block (lj's Cantina Labs: the pack says "Machine
            # Learning Engineer, TTS", the block says "Machine Learning Engineer,
            # Speech - Joint Audio-Video Modeling" — one advert, two titles, and
            # the row read "NO PACK", 2026-10-14). Title is the last resort, for
            # an observation day whose URL was not captured at all.
            index.setdefault(pack_url_key(org, url), directory)
            index.setdefault(pack_title_key(org, title), directory)
        # …and by the URL alone: the organisation is written twice too and
        # drifts (Elman vs Elman (Deep Science Ventures)); the advert's link is
        # the one fact both sides hold verbatim (lj's Elman GO, 2026-11-04).
        if url:
            index.setdefault(advert_url_key(url), directory)
    return index


def pack_title_key(org: str, title: str) -> str:
    """A pack lookup key that survives a missing URL.

    An advert can be observed one day with its link and another without
    (`- **URL**: _not available_`), and the two blocks then hash differently —
    so the advert would read as "no pack" while its pack sits right there. The
    org+title key is the tolerance: the advert IS the same advert.
    """
    try:
        from company_events import norm_key
    except Exception:                         # pragma: no cover - defensive
        norm_key = lambda value: re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return f"{norm_key(org)}|{(title or '').strip().lower()}"


def pack_url_key(org: str, url: str) -> str:
    """A pack lookup key on the advert's link — the identity both sides hold.

    A pack and a wiki block agree on the advert's URL even when their titles
    differ, because the title is *written* twice (once by the crawler's extractor,
    once by the pack's headings) and the URL is copied. `lead_identity.advert_url_key`
    is the canonical form, so a LinkedIn `/comm/` link and the employer's own URL
    still normalise apart — that is a different advert by construction.
    """
    from lead_identity import advert_url_key

    return f"{pack_title_key(org, '')}|{advert_url_key(url)}"


def pack_for(index: dict, org: str, title: str, url: str = "") -> Optional[Path]:
    """**The one lookup**: which pack belongs to this observation of an advert?

    Four callers ask this question and three of them had grown their own tolerance
    — `wiki_audit`, `leads` and the pack builder all fall back from the advert
    identity hash to the org+title key, and **`wiki_store` did not**, so the GO
    listing said `NO PACK` for adverts whose pack was sitting right there (kj's
    Tacit Labs and lj's Cantina Labs, measured 2026-10-14). The row the principal
    clicks is the one that must not be the odd one out.

    The two keys, in order:
      1. `_job_hash(org, title, url)` — the registry identity, exact;
      2. `pack_title_key(org, title)` — the tolerance for an advert observed one
         day with its link and another without, or filed under a title that was
         later normalised.

    `index` may be keyed by hash and/or by title (see `pack_index`).
    """
    if not index:
        return None
    if url:
        try:
            from job_registry import _job_hash
            from lead_identity import advert_url_key

            for candidate in (_job_hash(org, title, url),
                              _job_hash(org, (title or "").removesuffix("🆕").strip(), url),
                              pack_url_key(org, url),
                              advert_url_key(url)):
                hit = index.get(candidate)
                if hit is not None:
                    return hit
        except Exception:                      # identity is best-effort here too
            pass
    return index.get(pack_title_key(org, title))


def pack_serve_path(principal: str, directory: Path, name: str = PACK_INDEX_NAME) -> str:
    """The dashboard path for a pack file (the `applications` area is published)."""
    return f"/principals/{principal}/applications/{directory.name}/{name}"


NOT_INVESTIGATED_MARKER = "Status: NOT YET INVESTIGATED"

#: Written into a dossier whose advert has a newer pack. Additive and
#: reversible: the directory and its evidence stay, and every reader that asks
#: "is this still to do?" says no.
SUPERSEDED_MARKER = "Superseded by "


def pack_parts(pack_dir: Path) -> tuple[str, str]:
    """Organisation and role from a pack directory's own name.

    `YYYYMMDD-Org-Role-LoveWork` → ("Org", "Role"). The `-LoveWork` suffix is
    not part of the role and the date is not part of the organisation. One
    authority: the decider names a pack with it and the tick investigates with
    it, so the two cannot disagree about which pack a decision is about.
    """
    parts = pack_dir.name.split("-")
    org = parts[1].replace("_", " ") if len(parts) > 1 else pack_dir.name
    rest = parts[2:]
    if rest and rest[-1] == "LoveWork":
        rest = rest[:-1]
    return org, " ".join(rest).replace("_", " ")


#: The two files a pack must carry before a principal can apply with it. Not the
#: dossier (that is the evidence) and not the front page (that is the index): the
#: tailored CV and the letter are the artefacts.
MATERIAL_FOR_APPLYING = ("cover-letter.md", "cv-tailored.md")


#: The calibration marker as it was written into prose, long form first.
_CALIBRATION_PROSE_RE = re.compile(
    r"CALIBRATED(?:\s*\([^)]*\))?:\s*")


#: How much of **the principal** is in a pack. LJ, 2026-10-20: *"from PoV of re-using
#: what we already prepared for an Application, there is a huge difference between 1) we
#: spent significant time in the application, there is high principal SNR there, we can
#: learn and reuse some later; and 2) we didn't apply, principal no time spent, all
#: knowledge there is something Lovework already has — so nothing new to see or learn."*
ENGAGEMENT_KINDS = ("applied", "prepared", "untouched")

_APPLIED_MARKERS = (
    "LoveWork outcome: applied",
    "LoveWork status: SUBMITTED",
)


def principal_engagement(directory: Path) -> str:
    """`applied` | `prepared` | `untouched` — the principal's own time in this case.

    Read from the pack's own dossier, not inferred from a directory name: `applied`
    requires a recorded submission, `prepared` means the material exists (LoveWork's
    work, and the principal's if he edited it), `untouched` means neither. The
    distinction is what makes the piles searchable by *signal*: an applied case is the
    only kind whose letter and answers are worth mining.
    """
    directory = Path(directory)
    dossier = dossier_path(directory)
    text = ""
    if dossier is not None:
        try:
            text = dossier.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    if any(marker in text for marker in _APPLIED_MARKERS):
        return "applied"
    if any((directory / name).is_file() for name in MATERIAL_FOR_APPLYING):
        return "prepared"
    return "untouched"


#: What happened **after** the principal applied (LJ, 2026-10-20: *"the GO-s where we
#: applied but heard back a NO, or not heard anything back (so likely a NO) — whether we
#: should keep a separate list, or … distinguish them in some way — so we can search them
#: easier, mine them for signal better"*). Ordered by how much a reader learns from them.
OUTCOME_KINDS = ("rejected", "interviewing", "applied", "none")

_REJECTED_RE = re.compile(r"\bREJECTED\b|LoveWork outcome:\s*rejection", re.I)


def principal_outcome(directory: Path) -> dict:
    """`rejected | interviewing | applied | none`, with the file the verdict came from.

    Read from the case's own records, never guessed: a rejection is a recorded word (the
    PostHog dossier carries `LoveWork status: SUBMITTED … · REJECTED …`), an interview is
    a file the principal wrote after the application, and *silence* is not invented — a
    case with no answer stays `applied`, because "no reply yet" and "no reply ever" are
    different facts and only one of them is on disk.
    """
    directory = Path(directory)
    dossier = dossier_path(directory)
    text = ""
    if dossier is not None:
        try:
            text = dossier.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    engagement = principal_engagement(directory)
    for pattern in ("interview-*.md", "interview*.md"):
        found = sorted(directory.glob(pattern))
        if found and engagement in ("applied", "prepared"):
            return {"outcome": "interviewing", "basis": found[0].name}
    # **Only a case the principal actually applied to can be rejected** (2026-10-20).
    # Without this guard, a dossier whose *reasoning* quotes another advert's history
    # ("applied for this role … rejected") made an untouched pack read as a rejection —
    # measured: 9 "rejected" against 3 applied cases. A mention is not a receipt, the
    # same distinction `manual_projects` had to draw for a quoted confirmation page.
    if engagement == "applied" and _REJECTED_RE.search(text):
        return {"outcome": "rejected", "basis": dossier.name if dossier else ""}
    if engagement == "applied":
        return {"outcome": "applied", "basis": dossier.name if dossier else ""}
    return {"outcome": "none", "basis": ""}


def principal_cases(principal: str, *, state_dir: Path | None = None) -> list[dict]:
    """Every case LoveWork holds for this principal, with **how much of the principal is
    in it** — the list LJ asked for so the applied ones can be found and mined.

    It spans the live side and all three piles (`applications`, `gone`, `expired`,
    `leads`), because the question "what did LJ write for an application?" is answered by
    the applied ones wherever they now live. Sorted newest first.
    """
    state_root = (state_dir or config.STATE_DIR) / principal
    rows: list[dict] = []
    for area in ("applications", "gone", "expired", "leads"):
        root = state_root / area
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            dossier = dossier_path(directory)
            if dossier is None:
                continue
            try:
                text = dossier.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            org, role = pack_parts(directory)
            stamp = re.search(r"(?m)^Created: (\d{4}-\d{2}-\d{2})", text)
            outcome = principal_outcome(directory)
            rows.append({
                "where": area,
                "pack": str(directory),
                "name": directory.name,
                "org": org,
                "role": role,
                "created": stamp.group(1) if stamp else "",
                "engagement": principal_engagement(directory),
                # …and what came of it, so the applied-and-rejected cases are findable
                # by signal rather than by memory.
                "outcome": outcome["outcome"],
                "outcome_basis": outcome["basis"],
            })
    rows.sort(key=lambda r: (r["created"], r["name"]), reverse=True)
    return rows


def write_principal_cases(principal: str, *, state_dir: Path | None = None,
                          dry_run: bool = False) -> dict:
    """Persist `principal_cases` as JSONL, so the applied ones are searchable.

    `state/<p>/dataset/principal_cases.jsonl` — regenerated, not appended: it is a
    projection of the tree (the tree is the authority), so a case that moves between
    `applications/` and a pile moves in the list too, and a rebuild is always correct.
    """
    rows = principal_cases(principal, state_dir=state_dir)
    out = {"principal": principal, "cases": len(rows),
           "applied": sum(1 for r in rows if r["engagement"] == "applied"),
           "prepared": sum(1 for r in rows if r["engagement"] == "prepared"),
           "untouched": sum(1 for r in rows if r["engagement"] == "untouched"),
           "path": ""}
    if dry_run:
        return out
    target = (state_dir or config.STATE_DIR) / principal / "dataset" / "principal_cases.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.part")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(target)
    out["path"] = str(target)
    return out


def strip_calibration_markers(principal: str, *, state_dir: Path | None = None,
                              dry_run: bool = False) -> dict:
    """Remove the calibration marker from every pack's prose — the 2026-10-20 repair.

    LJ: *"Even if the model used is no longer mimo-v2.5 — so any adjustment we are doing
    for 'mimo-v2.5' is wrong, and the above is misleading, cryptic and needless. Even if
    we were doing the same adjustment, and correctly — it should not be part of the user
    message and reporting!"* The writer stopped emitting it on 2026-10-20 (the offset
    lives in `MatchResult.calibration_offset`); this is what clears the corpus — 155
    files carried `CALIBRATED (+1.30 onto the mimo-v2.5 scale [deepseek-v4.1-flash]): `
    because the 2026-10-18 change only shortened the prefix.

    Idempotent, and it touches only the marker: every other character of the reasoning
    survives. Scans `applications/`, `gone/` and `leads/`, because a principal reads all
    three and the retired packs are the ones he revisits.
    """
    state_root = (state_dir or config.STATE_DIR) / principal
    out = {"principal": principal, "checked": 0, "changed": 0, "names": [],
           "dry_run": dry_run}
    for area in ("applications", "gone", "leads"):
        root = state_root / area
        if not root.is_dir():
            continue
        # `.txt` too: the dossiers cut before 2026-10-18 were plain text, and the
        # repair found 65 of them still carrying the marker after the first pass.
        for path in sorted([*root.glob("*/*.md"), *root.glob("*/*.txt")]):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            out["checked"] += 1
            if not _CALIBRATION_PROSE_RE.search(text):
                continue
            fixed = _CALIBRATION_PROSE_RE.sub("", text)
            out["changed"] += 1
            out["names"].append(path.name)
            if dry_run:
                continue
            path.write_text(fixed, encoding="utf-8")
    return out


def pack_readiness(directory: Path) -> dict:
    """**Can the principal apply with this pack?** — one authority, one question.

    LJ, 2026-10-19, reading a vj GO whose front page said *"Tailored CV: not prepared
    yet / Cover letter: not prepared yet"*: *"either application pack is created =>
    remains on GO; or if we fail to create an application pack, we retry; but if not
    possible … then this should not be in GO, should be in leads."* A GO is a promise
    that the work is done; this is the predicate that decides whether it is.

    It exists because **three callers were asking weaker questions and each answered
    "fine"**: `wiki_audit.go_without_pack` tested whether a *directory* existed,
    `decide._pack_work` tested whether the *investigation block* was empty, and the
    index's `NO PACK` marker tested the directory again. Measured the day this landed:
    **44 of 51 GO rows had no material** while all three said the packs were in place.
    The only place the real requirement was written down was inline inside
    `prepare_honest_markers`, which used it to downgrade lying PREPARED markers.

    Returns `{"ready", "material", "investigated", "dossier", "missing", "why"}`.
    `material` is the requirement; `investigated` is the gate docs/41 C8 sets before
    the material is worth writing (a CV tailored to a company nobody has looked at).
    """
    directory = Path(directory)
    dossier = dossier_path(directory)
    text = ""
    if dossier is not None:
        try:
            text = dossier.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    missing = [name for name in MATERIAL_FOR_APPLYING
               if not (directory / name).is_file()]
    # A pack whose dossier was *never* written is a scaffold: nothing to investigate.
    investigated = bool(text) and (NOT_INVESTIGATED_MARKER not in text
                                  and "Advert review —" in text)
    ready = not missing and investigated
    # **Name every cause, not the first one found** (2026-10-19). This returned a
    # single reason, so a pack that was both uninvestigated *and* missing its
    # material reported only the block — and a breakdown that bucketed by the
    # reason then said "every remaining gap is only the block" about 45 packs that
    # had no cover letter at all. A report that hides a cause is the same defect
    # class as a count that hides a duplicate.
    reasons: list[str] = []
    if not text:
        reasons.append("no dossier")
    else:
        if not investigated:
            reasons.append("the investigation block is unfilled")
        if missing:
            reasons.append("no " + ", ".join(missing))
    why = "; ".join(reasons)
    return {"ready": ready, "material": not missing, "investigated": investigated,
            "dossier": dossier, "missing": missing, "why": why}


def go_pack_gap(principal: str) -> list[dict]:
    """**The Monday invariant**: GOs whose pack the principal cannot apply with yet.

    LJ, 2026-10-19: *"all GO-s should have application packs before the Monday 19:00
    wiki state email."* One reader, shared by the state email's gate, the audit
    (`go_unprepared`) and the loop's queue (`decide._go_pack_work`), so the three
    cannot disagree about whether the rule holds.

    Returns one row per gap: `{org, role, pack, why, has_advert}`. A row with
    `has_advert` False is one the loop cannot fix by working (there is nothing to
    fetch) — `retire.retire_unpreparable` is where that goes.
    """
    from tick import pack_advert_url

    try:
        identities = go_identities(principal)
        import retire

        # **A retired advert is not a gap** (2026-10-19). `go_identities` reads the
        # pages, so it still calls an advert GO after its pack has moved to
        # `leads/` — and this function then demanded a pack for something the
        # decision space no longer holds. Measured: one lj row, a duplicate pack
        # whose twin was already in `leads/`, where the retirement's own
        # never-overwrite guard refused the move and said nothing. The piles are
        # the authority; ask them.
        retired = retire.retired_keys(principal)
        retired_urls = retire.retired_urls(principal)
    except Exception:
        return []
    applications = config.STATE_DIR / principal / "applications"
    if not applications.is_dir():
        return []
    index = pack_index(principal, root=applications)
    out: list[dict] = []
    for identity, directory in index.items():
        if identity not in identities:
            continue
        org, role = pack_parts(directory)
        # `retire.is_retired` is the public predicate over `retired_keys` — the
        # same pair every builder consults before it would recreate retired work.
        advert = pack_advert_url(directory) or ""
        if retire.is_retired(org, role, retired) or (advert and advert in retired_urls):
            continue
        readiness = pack_readiness(directory)
        if readiness["ready"]:
            continue
        org, role = pack_parts(directory)
        out.append({"org": org, "role": role, "pack": str(directory),
                    "why": readiness["why"],
                    "has_advert": bool(pack_advert_url(directory))})
    out.sort(key=lambda r: (r["org"], r["role"]))
    return out


def unprepared_packs(applications_dir: Path) -> list[Path]:
    """Every **GO-side** pack a principal could not apply with yet.

    The queue the loop drains before the Monday state email (LJ, 2026-10-19: *"all
    GO-s should have application packs before the Monday 19:00 wiki state email"*).
    One reader, one definition: `pack_readiness`.
    """
    out: list[Path] = []
    if not applications_dir.is_dir():
        return out
    for directory in sorted(applications_dir.glob("*-LoveWork")):
        if not directory.is_dir():
            continue
        dossier = dossier_path(directory)
        if dossier is None:
            continue
        try:
            head = dossier.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # History is not work: a superseded duplicate, a pack the principal already
        # applied with, or one whose status says it closed.
        if SUPERSEDED_MARKER in head or "LoveWork outcome: applied" in head:
            continue
        if not pack_readiness(directory)["ready"]:
            out.append(directory)
    return out


def uninvestigated_packs(applications_dir: Path) -> list[Path]:
    """Prepared packs whose investigation block is still unfilled.

    A GO pack is not ready to send until its 'Company, hiring, and work
    reality' block is filled (LJ, 2026-10-06; docs/41 C8). This lists the
    ones that are not — the 07:00 pack job's first queue.
    """
    out: list[Path] = []
    if not applications_dir.is_dir():
        return out
    for txt in sorted(applications_dir.glob("*-LoveWork/*.txt")):
        try:
            text = txt.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # A superseded duplicate is history, not work: the same advert has a
        # newer pack (2026-10-13).
        if SUPERSEDED_MARKER in text:
            continue
        if NOT_INVESTIGATED_MARKER in text:
            out.append(txt)
    return out


def advert_identity(pack_dir: Path) -> str:
    """The advert this pack is about, as the dossier records it.

    `job_registry._job_hash(org, title, url)` written by `prepare_go_cases`.
    Empty when the dossier does not carry it (an older or hand-made pack).
    """
    text_path = _case_text_path(pack_dir)
    if not text_path:
        return ""
    try:
        match = re.search(r"^Advert identity:\s*(\S+)", text_path.read_text(
            encoding="utf-8", errors="ignore"), re.MULTILINE)
    except OSError:
        return ""
    return match.group(1) if match else ""


def pack_advert_key(pack_dir: Path) -> str:
    """What advert this pack is about, for grouping duplicates.

    The advert's own **link** when the pack carries one — the most stable key —
    and the recorded `Advert identity` otherwise. The recorded identity alone
    under-groups: the same advert gets a different `_job_hash` on different days
    when the organisation or title string drifts, so lj's 55 duplicate groups
    looked like 5 (measured 2026-10-13).
    """
    markdown = pack_dir / "application_pack.md"
    if markdown.is_file():
        try:
            match = re.search(r"^- \*\*Link\*\*: (.+)$",
                              markdown.read_text(encoding="utf-8", errors="ignore"),
                              re.MULTILINE)
        except OSError:
            match = None
        if match:
            link = match.group(1).strip().strip("<>")
            if "://" in link:
                return link
    identity = advert_identity(pack_dir)
    return identity or ""


def duplicate_packs(applications_dir: Path) -> list[tuple[str, list[Path]]]:
    """Adverts with more than one pack, newest first inside each group.

    The defect this detects (2026-10-13): `_matches_org` required the whole
    organisation against a 40-character slug, so a long-named employer got a new
    dated pack every day. 129 redundant directories across the three principals,
    each one a fresh "pending" item for the queue and a fresh investigation for
    the loop — which is why the loop looked like it was doing two packs a turn.
    """
    groups: dict[str, list[Path]] = {}
    if not applications_dir.is_dir():
        return []
    for pack in sorted(applications_dir.glob("*-LoveWork")):
        if not pack.is_dir():
            continue
        key = pack_advert_key(pack)
        if key:
            groups.setdefault(key, []).append(pack)
    out = [(key, sorted(packs, reverse=True))
           for key, packs in groups.items() if len(packs) > 1]
    return sorted(out, key=lambda item: -len(item[1]))


def is_superseded(pack_dir: Path) -> bool:
    """Has this pack been superseded by a newer one for the same advert?"""
    text_path = _case_text_path(pack_dir)
    if not text_path:
        return False
    try:
        return SUPERSEDED_MARKER in text_path.read_text(encoding="utf-8",
                                                        errors="ignore")
    except OSError:
        return False


def _superseded_by(text: str) -> str:
    """The pack named by a supersession marker, or ""."""
    m = re.search(rf"(?m)^{re.escape(SUPERSEDED_MARKER)}(\S+)", text)
    return m.group(1) if m else ""


def _cycle_marker(pack_dir: Path, packs: list[Path]) -> bool:
    """Is this pack marked superseded by a pack in its own duplicate group?"""
    text_path = _case_text_path(pack_dir)
    if not text_path:
        return False
    try:
        text = text_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _superseded_by(text) in {p.name for p in packs}


def _restore_superseded(pack_dir: Path) -> bool:
    """Remove a supersession marker from the chosen live pack.

    Only called for a cycle: every pack in the group was marked, so `pack_index`
    had no live pack and the audit reported the advert as "no pack". One mark is
    removed from the chosen best; the other directories keep theirs.
    """
    text_path = _case_text_path(pack_dir)
    if not text_path:
        return False
    try:
        text = text_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    kept, removed = [], False
    for line in text.splitlines():
        if not removed and line.startswith(SUPERSEDED_MARKER):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    text_path.write_text("\n".join(kept).lstrip("\n") + "\n", encoding="utf-8")
    return True


def mark_superseded_duplicates(applications_dir: Path, *,
                               dry_run: bool = False) -> dict:
    """Mark every duplicate except the best one (a ready pack, else the newest).

    Idempotent, additive and never destructive: the note goes into the dossier
    beside its own header, the directory and its evidence stay where they are,
    and `uninvestigated_packs`/`pack_queue`/`pack_index` stop treating it as work.

    "Newest" is not always "best": a later scaffold can be unprepared while the
    earlier pack already carries the investigation and the material. Keeping the
    scaffold would make the audit report a GO as unprepared when the principal
    can apply with the older pack (measured on Humand Talent and John Wiley,
    2026-11-04).

    A **cycle** is the one case where marking is reversed: if every pack in a
    group is marked, no live pack exists and the GO reads as unpacked. The chosen
    best's marker is removed; every other marker stays (Soniox GPU, 2026-11-04).
    """
    marked, groups, restored = 0, 0, 0
    for identity, packs in duplicate_packs(applications_dir):
        best = packs[0]
        for candidate in packs:
            try:
                if pack_readiness(candidate).get("ready"):
                    best = candidate
                    break
            except Exception:                 # a malformed pack is not the best
                continue
        if is_superseded(best):
            if not _cycle_marker(best, packs):
                continue
            restored += 1
            if not dry_run:
                _restore_superseded(best)
        groups += 1
        for older in packs:
            if older == best or is_superseded(older):
                continue
            marked += 1
            if dry_run:
                continue
            text_path = _case_text_path(older)
            if not text_path:
                continue
            try:
                text = text_path.read_text(encoding="utf-8", errors="ignore")
                text_path.write_text(
                    f"{SUPERSEDED_MARKER}{best.name} — the same advert "
                    f"(identity {identity}) was prepared again, and this copy is "
                    "kept for its history only (defect fixed 2026-10-13; the "
                    "matcher required the whole organisation against a "
                    "40-character slug).\n\n" + text,
                    encoding="utf-8")
            except OSError:
                continue
    return {"groups": groups, "marked": marked, "restored": restored,
            "dry_run": dry_run}


def prepare_crawl_packs(
    entries: Iterable[WikiEntry], principal: str
) -> list[PackResult]:
    """Prepare this principal's packs during its own crawl (the pipeline hook).

    Every principal LoveWork runs for gets its GO dossiers cut in its own root —
    the guard this replaced pinned the crawl path to ``lj``, so VJ's and KJ's
    crawls prepared nothing. A principal with *no* state directory is skipped
    outright: ``_principal_cases_root`` would fall back to the unified view, which
    is LJ's case root, and a second principal's cases must never be materialised
    there. Never raises — a crawl's real output is already durable, and pack
    preparation must not sink a run.
    """
    normalized = (principal or "").lower()
    if not (config.STATE_DIR / normalized).is_dir():
        logger.info("[packs] %s has no state dir — no packs prepared", normalized)
        return []
    try:
        # every GO in the run gets a pack, not only first-seen ones: a lead upgraded
        # MAYBE → GO was previously left without one (LJ's 63-GO run on 2026-10-03 cut
        # 36 packs, KJ's 2-GO run cut none), which is exactly the gap where the pack
        # should have come before the decision. Idempotent by advert identity: a pack
        # already cut for this advert reports `existing`.
        results = prepare_go_cases(entries, principal=normalized, only_new=False)
    except Exception as exc:                      # noqa: BLE001
        logger.warning("LoveWork application-pack preparation failed for %s: %s",
                       normalized, exc)
        return []
    created = sum(1 for r in results if r.status == "created")
    logger.info("[packs] %s: %d prepared, %d existing/skipped",
                normalized, created, len(results) - created)
    return results


def go_entries_from_report(report_path: Path) -> list[WikiEntry]:
    """Read the explicit ``## GO`` section of an existing full report.

    This is intentionally a backfill boundary: normal runs use their in-memory
    entries, while a one-off historical report can seed packs without crawling
    or rescoring again.
    """
    text = report_path.read_text(encoding="utf-8", errors="ignore")
    section_match = re.search(r"^## GO \(.*?\)\n(?P<body>.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not section_match:
        return []
    entries: list[WikiEntry] = []
    for block in re.split(r"^### ", section_match.group("body"), flags=re.MULTILINE):
        lines = block.strip().splitlines()
        if not lines or " — " not in lines[0]:
            continue
        org, title = lines[0].split(" — ", 1)
        # Numbered sections render "### 1. Org — Title"; the ordinal is
        # report layout, not part of the organisation name.
        org = re.sub(r"^\d+\.\s+", "", org)
        # …and the lifecycle marker is report furniture too. Left on, it lands in
        # the advert identity: a pack filed under `Title 🆕` can never be found
        # by the registry's own hash, so the wiki would report "NO PACK" for an
        # advert that has one (found 2026-10-09, joining packs to GO rows).
        title = title.strip().removesuffix("🆕").strip()

        def field(name: str) -> str:
            match = re.search(rf"^- \*\*{re.escape(name)}\*\*:\s*(.+)$", block, re.MULTILINE)
            return match.group(1).strip() if match else ""

        score_match = re.search(r"^- \*\*Score\*\*:\s*(\d+(?:\.\d+)?)/10", block, re.MULTILINE)
        if not score_match:
            continue
        found_match = re.search(
            r"^- \*\*Found via\*\*:\s*\[(?P<source>[^]]+)\]\((?P<url>[^)]+)\)"
            r"(?:\s+\((?P<date>\d{4}-\d{2}-\d{2})\))?",
            block,
            re.MULTILINE,
        )
        entries.append(WikiEntry(
            org_name=org,
            title=title,
            url=field("URL"),
            location=field("Location"),
            score=float(score_match.group(1)),
            decision="GO",
            reasoning=field("Reasoning"),
            source=field("Source"),
            fit_score=_float_field(field("Fit")),
            reach_score=_float_field(field("Reach")),
            flourish_score=_float_field(field("Flourish")),
            combined_score=_float_field(field("Combined")),
            recommended_action=field("Action"),
            discovery_url=found_match.group("url") if found_match else "",
            discovery_date=found_match.group("date") if found_match else "",
            alignment_matrix=_list_field(block, "Evidence alignment"),
            gaps=_list_field(block, "Gaps"),
            application_angle=field("Application angle"),
            screening_story=field("Screening story"),
            likely_day_to_day=field("Likely day-to-day"),
            prestige_trap_risk=field("Prestige-trap risk"),
            lifecycle_status=field("Lifecycle"),
        ))
    return entries


def _float_field(value: str) -> float | None:
    match = re.match(r"(\d+(?:\.\d+)?)/10", value)
    return float(match.group(1)) if match else None


def _list_field(block: str, heading: str) -> list[str]:
    match = re.search(
        rf"^- \*\*{re.escape(heading)}\*\*:\n(?P<items>(?:  - .*\n?)*)",
        block,
        re.MULTILINE,
    )
    if not match:
        return []
    return [line[4:].strip() for line in match.group("items").splitlines() if line.startswith("  - ")]


def render_pack_report_section(
    results: Iterable[PackResult], base_dir: Path | None = None
) -> list[str]:
    """Render a report section that makes GO-to-case handoff explicit.

    ``base_dir`` is the directory of the report file the section is inserted
    into; pack links are then real relative paths from the report to each pack
    (packs live under ``state/<principal>/applications/``). When absent, the
    legacy ``../applications/<name>/`` form is kept for callers that render
    outside a report file.
    """
    values = list(results)
    if not values:
        return []

    def pack_link(result: PackResult) -> str:
        if result.path is None:
            return ""
        if base_dir is not None:
            return os.path.relpath(result.path, base_dir)
        return f"../applications/{result.path.name}/"

    lines = ["## LoveWork application packs", ""]
    created = [result for result in values if result.status == "created"]
    skipped = [result for result in values if result.status != "created"]
    if created:
        lines.extend(["### Created — ready for review", ""])
        for result in created:
            lines.append(
                f"- [{result.entry.score:.1f}] {result.entry.org_name} — {result.entry.title}"
            )
            lines.append(f"  `{pack_link(result)}`")
        lines.append("")
    if skipped:
        lines.extend(["### Existing / not created", ""])
        for result in skipped:
            location = f" (`{pack_link(result)}`)" if result.path else ""
            lines.append(f"- {result.entry.org_name} — {result.entry.title}: {result.reason}{location}")
        lines.append("")
    return lines


def insert_pack_report_section(report_path: Path, results: Iterable[PackResult]) -> None:
    """Insert this run's pack hand-off directly after a full report's GO section."""
    section = render_pack_report_section(results, base_dir=report_path.parent)
    if not section:
        return
    text = report_path.read_text(encoding="utf-8")
    text = re.sub(
        r"^## LoveWork application packs\n.*?(?=^## MAYBE \(|\Z)",
        "",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    marker = re.search(r"^## MAYBE \(", text, re.MULTILINE)
    if not marker:
        text = text.rstrip() + "\n\n" + "\n".join(section)
    else:
        text = text[:marker.start()].rstrip() + "\n\n" + "\n".join(section) + "\n" + text[marker.start():]
    report_path.write_text(text, encoding="utf-8")


def go_entries_from_reports(principal: str = "lj", *, newest_first: bool = True) -> list[WikiEntry]:
    """Every GO advert the principal's own reports describe, one per advert.

    The reports are the only place that carries the FULL assessment block
    (reasoning, evidence alignment, gaps, application angle, screening story),
    which is what a pack's "why this is a GO" section needs — the org pages
    carry it too, but their reader keeps less of it. Newest observation per
    advert identity wins, so a role re-seen across sweeps is prepared once.
    """
    reports_dir = config.STATE_DIR / principal / "wiki" / "reports"
    if not reports_dir.is_dir():
        return []
    seen: dict[str, WikiEntry] = {}
    reports = sorted(reports_dir.glob("*.md"),
                     key=lambda p: p.stat().st_mtime, reverse=newest_first)
    for report in reports:
        for entry in go_entries_from_report(report):
            identity = _job_hash(entry.org_name, entry.title, entry.url or "")
            if identity and identity not in seen:
                seen[identity] = entry
    return list(seen.values())


def go_identities(principal: str) -> set[str]:
    """Advert identities whose **current** verdict is GO — one authority.

    "Current" is the rule the whole system already states: **the newest observation
    of an advert is its verdict** (the index and the lead pages are built that way,
    and the reason is recorded there — Latent Labs' protein designer was DROP 1.8
    on 2026-10-01 and MAYBE 6.1 on 2026-10-04, and showing the older block showed a
    verdict nobody held any more).

    This function used to answer a *different* question — "is there a GO block
    anywhere on the page" — and the two readings disagreed by a factor of two: lj
    had **266** adverts with some GO block but only **121** whose current verdict is
    GO, so the hourly pass kept preparing material for 145 adverts the index had
    since dropped (2026-10-14). One question, one answer: the audit asks this, the
    pack pass asks this, and the index agrees with both.
    """
    from wiki_store import block_identity

    from wiki_store import org_page_paths

    pages = org_page_paths(config.STATE_DIR / principal / "wiki")
    if not pages:
        return set()
    newest: dict[str, tuple[str, str]] = {}
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="ignore")
        h1 = re.search(r"(?m)^# (.+)$", text)
        org = h1.group(1).strip() if h1 else page.stem.replace("_", " ")
        for block in re.split(r"(?m)^### ", text)[1:]:
            decision = re.search(r"(?m)^- \*\*Decision\*\*: (\w+)", block)
            if not decision:
                continue
            date = block.split(" — ", 1)[0].strip()[:10]
            identity = block_identity(org, "### " + block)
            prior = newest.get(identity)
            if prior is None or date > prior[0]:
                newest[identity] = (date, decision.group(1))
    return {identity for identity, (_date, decision) in newest.items()
            if decision == "GO"}


def _go_entries_from_org_pages(principal: str) -> list[WikiEntry]:
    """GO adverts read off the org pages — the lean source, used when no report
    describes the advert."""
    from wiki_store import WikiStore

    wiki_root = config.STATE_DIR / principal / "wiki"
    try:
        store = WikiStore(root=wiki_root)
        entries = store._entries_from_org_pages()
    except Exception:
        return []
    return [e for e in entries if (e.decision or "").upper() == "GO"]


def maybe_entries(principal: str = "lj") -> list:
    """The current MAYBE adverts, as entries a pack can be cut from — one authority.

    LJ, 2026-10-20: *"Atm the number of GO-s for vj is slim. Let's make the MAYBE-s also
    prepared for application. So Add the requirement for preparing application packs for
    MAYBE-s too."* Read from the wiki's current view, the same authority the loop's queue
    uses (`decide._go_pack_work`), so a pack is cut for exactly the rows the index shows.
    """
    from wiki_store import WikiStore

    try:
        store = WikiStore(root=config.STATE_DIR / principal / "wiki", create=False)
        _current, fresh = store.current_view()
    except Exception as exc:                     # never silently swallow
        logger.warning("[packs] MAYBE view unreadable for %s: %s", principal, exc)
        return []
    from lead_identity import is_noise_title

    return [e for e in fresh if e.decision == "MAYBE" and not is_noise_title(e.title or "")]


def prepare_missing_packs(principal: str = "lj", *, dry_run: bool = False,
                          decisions: tuple[str, ...] = ("GO",)) -> dict:
    """Create the pack for every GO advert that has none (the self-healing step).

    LJ, 2026-10-09: "all GO-s must have packs! That's the point of LoveWork. To
    save the principal time." A GO without a pack is the product failing at its
    own promise, so it is a defect class (`wiki_audit` counts it as
    `go_without_pack`) and this is the repair. Idempotent: `prepare_go_cases`
    matches an existing pack by advert identity.
    """
    # Which adverts are GO **now**: the wiki is the authority, because a ruling
    # may have overturned a GO the report still shows as one (the 2026-10-09
    # eligibility drain rewrote such rows in place). Creating a pack for a row we
    # have since dropped would prepare material for a role the principal cannot
    # take — the exact defect the ruling exists to prevent.
    live_go = go_identities(principal)
    entries = [e for e in go_entries_from_reports(principal)
               if _job_hash(e.org_name, e.title, e.url or "") in live_go]
    # …and the GOs the reports do not describe (an old run whose report is gone,
    # or a row that arrived through consume/reassess): the org page still carries
    # the identity, the advert link, the verdict and its reasoning, which is
    # enough for a pack. The reports win where they exist — they carry the full
    # assessment block.
    described = {_job_hash(e.org_name, e.title, e.url or "") for e in entries}
    entries.extend(e for e in _go_entries_from_org_pages(principal)
                   if _job_hash(e.org_name, e.title, e.url or "") in live_go
                   and _job_hash(e.org_name, e.title, e.url or "") not in described)
    # **A title that is not a role gets no pack** (2026-10-14). The hourly pass
    # cuts a pack for every GO, and one of lj's GOs is titled "Madrid, Spain" —
    # the row is a `junk_title` defect (the extractor took a location for the
    # position), and packing it would spend model calls to prepare the principal
    # for a place. Same authority the crawl's own junk split uses, so the two
    # cannot drift. These are *named* in the stats rather than silently dropped:
    # a GO that cannot be packed is a fact about the row, and the wiki audit
    # already reports it.
    from lead_identity import is_noise_title

    if "MAYBE" in decisions:
        # Only the ones that have no pack yet: `prepare_go_cases` matches by advert
        # identity, so a MAYBE that is already packed comes back as `existing` and costs
        # nothing — but the list stays honest about the work.
        packed = pack_index(principal)
        for entry in maybe_entries(principal):
            identity = _job_hash(entry.org_name, entry.title, entry.url or "")
            if identity in packed:
                continue
            entries.append(entry)
    junk = [e for e in entries if is_noise_title(e.title or "")]
    entries = [e for e in entries if not is_noise_title(e.title or "")]
    if not entries:
        return {"principal": principal, "go_entries": 0, "created": 0,
                "existing": 0, "skipped": 0,
                "junk_titles": [f"{e.org_name} — {e.title}" for e in junk]}
    results = prepare_go_cases(entries, principal=principal, only_new=False,
                               dry_run=dry_run, decisions=tuple(decisions))
    stats = {"principal": principal, "go_entries": len(entries),
             "created": sum(1 for r in results if r.status == "created"),
             "existing": sum(1 for r in results if r.status == "existing"),
             "skipped": sum(1 for r in results if r.status not in ("created", "existing"))}
    if junk:
        stats["junk_titles"] = [f"{e.org_name} — {e.title}" for e in junk]
    return stats


def dossier_entry(directory: Path) -> Optional[tuple["WikiEntry", str]]:
    """**The entry a pack was built from**, read back from its own dossier.

    One reader for the dossier's identity fields, because two callers need them
    and they must not disagree about what the file says:
    `refresh_pack_markdown` rebuilds the pack's front page from them, and the
    loop's `review_go` needs `primary_content_hash` — the evidence the GO was
    *scored* with, which is the whole question when a score is re-read against
    the advert (LJ, 2026-10-18).

    Returns `(entry, advert identity)`, or `None` when the directory holds no
    LoveWork dossier. A MANUAL case (LJ's own application, or a case from another
    tool) has no advert identity, and inventing one would be a claim about the
    principal's own submission — so the absence is reported, never filled in.
    """
    from wiki_store import WikiEntry

    stub = dossier_path(directory)
    if stub is None:
        return None
    text = stub.read_text(encoding="utf-8", errors="ignore")
    fields = dict(re.findall(r"(?m)^([A-Za-z ]+): (.+)$", text))
    identity = (fields.get("Advert identity") or "").strip()
    if not identity:
        return None

    def cell(name: str) -> str:
        """A dossier cell, with the writer's own placeholders read as *absent*.

        `_pack_text` writes the literal `not retained` where it holds nothing, so
        the placeholder is not a value: carried through as one it is a truthy
        string, and every reader that asks "do we hold this?" answers yes for
        every pack. That is not hypothetical — it is the difference between "this
        GO was scored with the advert's own text" and "it was not", which is the
        question the review pass is built on (2026-10-18).
        """
        value = (fields.get(name) or "").strip()
        return "" if value.lower() in UNKNOWN_CELL_VALUES else value

    entry = WikiEntry(
        org_name=(fields.get("Organisation") or directory.name).strip(),
        title=(fields.get("Position") or "").strip() or directory.name,
        url=advert_url(cell("Primary advert URL")),
        location=cell("Location"),
        score=float((re.search(r"([0-9.]+)/10", fields.get("Initial decision", ""))
                     or re.match(r"([0-9.]+)", "0")).group(1)),
        decision=(fields.get("Initial decision") or "GO").split(" ")[0].strip(),
        reasoning="", source=cell("Source"),
        lifecycle_status=cell("Lifecycle"),
        first_seen=cell("First seen"),
        discovery_url=markdown_link_target(fields.get("Discovery URL") or ""),
        primary_content_hash=cell("Primary evidence"),
        primary_fetch_method=cell("Primary fetch"),
        primary_fetched_at=cell("Primary fetched"),
        recommended_action=cell("Recommended action"),
    )
    # the assessment block, when the dossier carries one
    for label, field in (("Application angle", "application_angle"),
                         ("Screening story", "screening_story"),
                         ("Likely day-to-day", "likely_day_to_day")):
        m = re.search(rf"(?m)^{re.escape(label)}: (.+)$", text)
        if m:
            setattr(entry, field, m.group(1).strip())
    return entry, identity


def refresh_pack_page(directory: Path, principal: str) -> bool:
    """Rebuild **one** pack's front page from its own dossier. True when written.

    LJ, 2026-10-20, on vj's top GO: *"Tailored CV: not prepared yet / Cover letter: not
    prepared yet"* — while both files were sitting in the directory. The material writer
    patched the page's **status marker** from CUT to PREPARED and never regenerated the
    links, so a finished pack kept advertising itself as empty. **A page that is written
    once and patched afterwards is a page that will lie**; this is the one call that makes
    it true again, and `pack_completion.complete_pack` now makes it the moment the material
    lands.

    Returns False (and writes nothing) for a case that is not a LoveWork pack — a manual
    application gets a file index, never an invented status.
    """
    rebuilt = dossier_entry(directory)
    if rebuilt is None:
        write_case_index(directory, principal)
        return True
    entry, identity = rebuilt
    write_pack_markdown(entry, identity, _enrichment_for(entry.url), directory,
                        principal)
    return True


def refresh_pack_markdown(principal: str = "lj", *, dry_run: bool = False) -> dict:
    """Write/refresh `application_pack.md` for every pack on disk.

    The packs created before 2026-10-09 have a dossier but no fixed-name front
    page; this backfills them. The entry is reconstructed from the dossier's own
    fields (it is the record the pack was built from), so the page never invents
    anything the dossier does not say.
    """
    from wiki_store import WikiEntry

    # **The piles too** (2026-10-20): LJ went looking for the Hugging Face case he had
    # applied to himself and "took me too long to find the application" — it was in
    # `gone/`, whose front pages this refresh never touched. A retired pack is still a
    # case he may want to read, so its page carries the same facts, including how much
    # of the principal is in it.
    state_root = (config.STATE_DIR / principal)
    roots = [_principal_cases_root(principal)] + [
        state_root / area for area in ("gone", "expired", "leads")]
    stats = {"packs": 0, "written": 0, "skipped": 0, "errors": 0}
    directories = []
    for root in roots:
        if root.is_dir():
            directories += [d for d in sorted(root.iterdir()) if d.is_dir()]
    if not directories:
        return stats
    for directory in directories:
        stub = _case_stub(directory)
        if stub is None:
            stats["skipped"] += 1
            continue
        stats["packs"] += 1
        try:
            rebuilt = dossier_entry(directory)
            if rebuilt is None:
                # A MANUAL case (LJ's own application, or a case from another
                # tool): it is not a LoveWork pack, and rendering a status or a
                # decision for it would be an invention — the first version of
                # this page said "PREPARED / GO (0.0/10)" over LJ's real
                # submissions. Render a file index instead, which is what a
                # reader wants from a case directory anyway.
                if dry_run:
                    continue
                write_case_index(directory, principal)
                stats["written"] += 1
                continue
            entry, identity = rebuilt
            if dry_run:
                continue
            write_pack_markdown(entry, identity, _enrichment_for(entry.url),
                                directory, principal)
            stats["written"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("pack markdown refresh failed for %s: %s", directory.name, exc)
    return stats


def advert_url(value: str) -> str:
    """A URL cell's value, or "" — **a URL has a scheme**.

    The rule the tick's own pack reader states ("A URL has a scheme. Anything else
    is the placeholder, and the honest answer is *this pack carries no advert
    link*"): the dossier writes `not retained` where it holds no link, and a
    placeholder carried through as a value makes every pack look fetchable. One
    predicate, so the two readers of the same cell cannot disagree — the failure
    that produced a fetcher request for the URL `_not` (2026-10-11).
    """
    text = markdown_link_target((value or "").strip()).strip("<>").strip()
    return text if "://" in text else ""


#: A dossier cell's own spelling of "we hold nothing here". `_pack_text` writes
#: the first one literally, so a reader must treat it as absence rather than as a
#: value (see `dossier_entry.cell`).
UNKNOWN_CELL_VALUES = frozenset({"", "not retained", "unspecified", "unknown",
                                 "n/a", "-"})

#: A separator line in the old plain-text dossier: 72 `=`.
_OLD_SEPARATOR_RE = re.compile(r"\n={10,}\n\n(?P<title>[^\n]+)\n")
#: A provenance line whose value is a URL, which markdown can make clickable.
_BARE_URL_LINE_RE = re.compile(r"(?m)^(?P<label>(?:Discovery URL|Primary advert URL)): "
                               r"(?P<url>https?://\S+)$")


def to_markdown_dossier(text: str) -> str:
    """The old plain-text dossier, as markdown — **mechanically, never an edit**.

    Two substitutions and nothing else: the `====` separator plus the bare title
    becomes a `##` heading, and a provenance URL becomes a link. Every other
    character survives, so a dossier that carries a hand-written note keeps it.
    """
    text = _OLD_SEPARATOR_RE.sub(lambda m: f"\n## {m.group('title').strip()}\n", text)
    text = _BARE_URL_LINE_RE.sub(
        lambda m: f"{m.group('label')}: [{m.group('url')}]({m.group('url')})", text)
    return text


def migrate_dossiers_to_markdown(principal: str, *, dry_run: bool = False) -> dict:
    """Rename every **pack** dossier to `.md` and give it markdown content.

    LJ, 2026-10-15: *"when LJ creates it, it will be .txt. But when lovework creates
    the yyyymmdd-Company_name-Opening_name — it should be .md not .txt."*

    So this touches `*-LoveWork` directories only: a case the principal wrote keeps
    its own convention. Idempotent — a dossier already `.md` is left alone.
    """
    root = _principal_cases_root(principal)
    out: dict = {"principal": principal, "converted": 0, "already": 0,
                 "skipped": 0, "dry_run": dry_run, "names": []}
    if not root.is_dir():
        return out
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not directory.name.endswith("-LoveWork"):
            continue
        legacy = directory / f"{directory.name}{LEGACY_DOSSIER_SUFFIX}"
        modern = directory / f"{directory.name}{DOSSIER_SUFFIX}"
        if modern.is_file() and not legacy.is_file():
            out["already"] += 1
            continue
        if not legacy.is_file():
            out["skipped"] += 1
            continue
        # **A `.txt` that LoveWork did not write is the principal's own file.**
        # LJ, 2026-10-15: he pasted the real advert for PostHog's *AI Research
        # Engineer* into `<case>.txt` — his convention, and the file an editor was
        # open on — while the generated dossier was the `.md` beside it. Renaming
        # that would have taken his file. The generated dossier carries
        # `LoveWork status:`; his notes do not, and that is the test.
        try:
            head = legacy.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            out["skipped"] += 1
            continue
        if "LoveWork status:" not in head and NOT_INVESTIGATED_MARKER not in head:
            out["skipped"] += 1
            out["kept_principal_files"] = out.get("kept_principal_files", 0) + 1
            continue
        out["converted"] += 1
        out["names"].append(directory.name)
        if dry_run:
            continue
        text = legacy.read_text(encoding="utf-8", errors="replace")
        modern.write_text(to_markdown_dossier(text), encoding="utf-8")
        legacy.unlink()          # the content is in the .md now, byte for byte
    return out


def write_case_index(directory: Path, principal: str = "lj") -> Path:
    """A file index for a case directory that is NOT a LoveWork pack.

    Manual cases (LJ's own applications) share the tree with LoveWork's packs.
    They have no `Advert identity`, so the pack page must not claim a LoveWork
    status or a decision for them — it lists what the directory holds and points
    at the advert it can find.
    """
    stub = _case_stub(directory)
    advert = ""
    if stub is not None:
        try:
            text = stub.read_text(encoding="utf-8", errors="ignore")
            m = (re.search(r"(?m)^Primary advert URL: (\S+)", text)
                 or re.search(r"https?://\S+", text))
            if m:
                advert = (m.group(1) if m.re.groups else m.group(0)).rstrip(".,);")
        except OSError:
            pass
    lines = [f"# {directory.name}", "",
             "**Manual case — not a LoveWork pack.** No LoveWork status or "
             "decision applies; the files in this directory are the record.", ""]
    if advert:
        lines.extend([f"- **Advert**: {advert}", ""])
    lines.extend(["## Files in this case", ""])
    for path in sorted(directory.iterdir()):
        if path.is_dir() or path.name == PACK_INDEX_NAME:
            continue
        label = path.name
        if path.suffix in (".md", ".txt"):
            lines.append(f"- [{label}]({label}) — {path.stat().st_size:,} bytes")
        else:
            lines.append(f"- {label} — {path.stat().st_size:,} bytes")
    lines.extend(["", "---", "",
                  "_Generated by LoveWork as a browsable index of this case "
                  "directory._", ""])
    path = directory / PACK_INDEX_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def honest_markers(principal: str = "lj", *, dry_run: bool = False) -> dict:
    """Downgrade lying PREPARED markers to CUT, in place, honestly.

    LJ, 2026-10-18 (the Together AI case): a pack directory whose scaffold
    says `PREPARED — not submitted` while the tailored material is absent and
    the investigation block is unfilled **claims work that was never done**.
    The tells are mechanical — `NOT_INVESTIGATED_MARKER` in the dossier, or
    the material files missing — so the repair is mechanical too: every
    *-LoveWork pack whose dossier still carries PREPARED_MARKER and either
    tell becomes CUT. A completed pack (material on disk, block filled)
    keeps PREPARED. Report-only by default, like every repair that touches
    the principal's tree.
    """
    root = config.STATE_DIR / principal / "applications"
    out = {"checked": 0, "downgraded": 0, "kept_prepared": 0,
           "names": [], "dry_run": dry_run}
    if not root.is_dir():
        return out
    for directory in sorted(root.glob("*-LoveWork")):
        if not directory.is_dir():
            continue
        out["checked"] += 1
        dossier = dossier_path(directory)
        if dossier is None:
            continue
        try:
            text = dossier.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if PREPARED_MARKER not in text:
            continue                      # already honest (CUT, or principal's)
        material_present = all((directory / name).is_file()
                               for name in ("cover-letter.md", "cv-tailored.md"))
        investigated = (NOT_INVESTIGATED_MARKER not in text
                        and "Advert review —" in text)
        if material_present and investigated:
            out["kept_prepared"] += 1
            continue
        out["downgraded"] += 1
        out["names"].append(directory.name)
        if dry_run:
            continue
        fixed = text.replace(PREPARED_MARKER, CUT_MARKER)
        tmp = dossier.with_name(dossier.name + ".honest-tmp")
        tmp.write_text(fixed, encoding="utf-8")
        os.replace(tmp, dossier)
        front = directory / "application_pack.md"
        if front.is_file():
            try:
                front_text = front.read_text(encoding="utf-8", errors="ignore")
                if PREPARED_MARKER in front_text:
                    side = front.with_name(front.name + ".honest-tmp")
                    side.write_text(
                        front_text.replace(PREPARED_MARKER, CUT_MARKER),
                        encoding="utf-8")
                    os.replace(side, front)
            except OSError:
                pass                      # the dossier is the authority
    return out


if __name__ == "__main__":                    # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--refresh-markdown", action="store_true",
                        help="write/refresh application_pack.md in every pack")
    parser.add_argument("--decisions", default="GO",
                        help="with --prepare-missing: which verdicts get a pack "
                             "(GO, or GO,MAYBE)")
    parser.add_argument("--prepare-missing", action="store_true",
                        help="create the pack for every GO advert that has none")
    parser.add_argument("--dossier-markdown", action="store_true",
                        help="rename every pack dossier to .md and make it markdown")
    parser.add_argument("--honest-markers", action="store_true",
                        help="downgrade PREPARED markers to CUT where the pack "
                             "was never completed (report-only with --dry-run)")
    parser.add_argument("--principal-cases", action="store_true",
                        help="list every case with how much of the principal is in it "
                             "(applied | prepared | untouched); --write persists the index")
    parser.add_argument("--write", action="store_true",
                        help="with --principal-cases: write the JSONL index")
    parser.add_argument("--strip-calibration", action="store_true",
                        help="remove the calibration marker from every pack's prose "
                             "(the 2026-10-20 repair; idempotent)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    status = 0
    if args.principal_cases:
        stats = write_principal_cases(args.profile, dry_run=not args.write)
        outcomes = {}
        for row in principal_cases(args.profile):
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        print(f"{args.profile}: {stats['cases']} case(s) — applied {stats['applied']}, "
              f"prepared {stats['prepared']}, untouched {stats['untouched']}; "
              f"outcomes " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
              + (f"; wrote {stats['path']}" if stats.get("path") else "  (dry run)"))
        for row in [r for r in principal_cases(args.profile)
                    if r["engagement"] == "applied"]:
            print(f"   {row['outcome']:12s} {row['created'] or '—':10s} "
                  f"{row['where']:12s} {row['org'][:28]} — {row['role'][:38]}")
        raise SystemExit(0)
    if args.strip_calibration:
        stats = strip_calibration_markers(args.profile, dry_run=args.dry_run)
        print(f"{args.profile}: {stats['changed']} of {stats['checked']} pack file(s) "
              f"carried the calibration marker"
              + ("  (dry run)" if stats["dry_run"] else " — removed"))
        raise SystemExit(0)
    if args.honest_markers:
        stats = honest_markers(args.profile, dry_run=args.dry_run)
        print(f"{args.profile}: {stats['downgraded']} lying PREPARED → CUT "
              f"({stats['kept_prepared']} kept prepared, {stats['checked']} checked)"
              + ("  (dry run)" if stats["dry_run"] else ""))
        for name in stats["names"][:8]:
            print(f"   {name}")
        if len(stats["names"]) > 8:
            print(f"   … and {len(stats['names']) - 8} more")
        raise SystemExit(0)
    if args.refresh_markdown:
        stats = refresh_pack_markdown(args.profile, dry_run=args.dry_run)
        print(args.profile, stats)
        status = status or (1 if stats.get("errors") else 0)
    if args.dossier_markdown:
        stats = migrate_dossiers_to_markdown(args.profile, dry_run=args.dry_run)
        print(f"{args.profile} {stats['converted']} converted, "
              f"{stats['already']} already markdown, {stats['skipped']} skipped"
              + (" (dry run)" if args.dry_run else ""))
        raise SystemExit(1 if stats.get("errors") else 0)
    if args.prepare_missing:
        # A pass the loop runs every hour must say, in its exit code, whether it
        # worked: the maintenance phase reads the code, not our prose (a pack
        # step that failed quietly is how kj's GOs sat unpacked for a month).
        stats = prepare_missing_packs(
            args.profile, dry_run=args.dry_run,
            decisions=tuple(d.strip() for d in args.decisions.split(",") if d.strip()))
        print(f"{args.profile} {stats}")
        status = status or (1 if stats.get("errors") else 0)
    raise SystemExit(status)


def pack_advert_url(directory: Path) -> str:
    """The advert link a pack's front page carries, or "" — one reader.

    `application_pack.md`'s `- **Link**: …` cell is the authority (the dossier
    may be `.txt`-era and carry no link). The cell is bare in older packs and a
    markdown link in converted ones; `advert_url` refuses a placeholder. The
    tick's `pack_advert_url` and the decider's "can this pack be worked?" both
    read through this, so they cannot disagree.
    """
    page = Path(directory) / "application_pack.md"
    if not page.is_file():
        return ""
    match = re.search(r"^- \*\*Link\*\*: (.+)$",
                      page.read_text(encoding="utf-8", errors="ignore"), re.M)
    if not match:
        return ""
    return advert_url(markdown_link_target(match.group(1)).strip("<>"))


def set_advert_link(directory: Path, url: str) -> bool:
    """Write `url` into the pack's front page; False when the page is absent.

    Used by the loop's `resolve_advert` action after a web search finds the
    employer's own advert. Only the link cell changes — the rest of the page is
    the pack's record and is left byte-for-byte.
    """
    page = Path(directory) / "application_pack.md"
    if not page.is_file() or not url:
        return False
    text = page.read_text(encoding="utf-8")
    cell = f"- **Link**: {url}"
    if re.search(r"^- \*\*Link\*\*:.*$", text, re.M):
        text = re.sub(r"^- \*\*Link\*\*:.*$", cell, text, count=1, flags=re.M)
    else:
        lines = text.splitlines()
        heading = next((i for i, line in enumerate(lines) if line.startswith("#")), -1)
        at = heading + 1 if heading >= 0 else 0
        lines[at:at] = ([""] if at and lines[at - 1].strip() else []) + [cell]
        text = "\n".join(lines) + "\n"
    page.write_text(text, encoding="utf-8")
    return True

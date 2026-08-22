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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import config
from enrichment import ENRICHMENT_VERSION
from job_registry import _job_hash
from wiki_store import WikiEntry

logger = logging.getLogger(__name__)

SEPARATOR = "=" * 72
PREPARED_MARKER = "LoveWork status: PREPARED — not submitted"
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
    Fall back to the unified view only when the principal state dir is absent
    (pre-migration / non-LJ setups).
    """
    state_path = config.STATE_DIR / principal.lower() / "applications"
    if state_path.is_dir():
        return state_path
    return config.APPLICATIONS_DIR


def _mirror_pack_in_unified_view(pack_dir: Path) -> None:
    """Expose a state-owned pack in the parent repo's applications/ view.

    The parent repo (LJ-work-2026/applications) is the unified human view:
    principal-created dirs live there directly, while LoveWork-created packs
    appear as relative symlinks pointing down into state/<principal>/applications.
    """
    try:
        link_path = config.APPLICATIONS_DIR / pack_dir.name
        if link_path.exists() or link_path.is_symlink():
            return  # already mirrored
        config.APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(os.path.relpath(pack_dir, config.APPLICATIONS_DIR))
        logger.info("[packs] Mirrored %s -> %s", link_path, pack_dir)
    except OSError as exc:
        logger.warning("[packs] Could not mirror pack %s in unified view: %s", pack_dir, exc)


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


def _matches_org(directory_name: str, org: str) -> bool:
    compact_org = re.sub(r"[^a-z0-9]", "", org.lower())
    compact_dir = re.sub(r"[^a-z0-9]", "", directory_name.lower())
    return bool(compact_org and compact_org in compact_dir)


def _case_text_path(directory: Path) -> Path | None:
    txt_files = sorted(directory.glob("*.txt"))
    return txt_files[0] if txt_files else None


def _is_prepared(directory: Path) -> bool:
    text_path = _case_text_path(directory)
    if not text_path:
        return directory.name.endswith("-LoveWork")
    try:
        return PREPARED_MARKER in text_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


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


def _section(lines: list[str], title: str) -> None:
    lines.extend(["", SEPARATOR, "", title, ""])


def _pack_text(entry: WikiEntry, advert_hash: str, evidence: dict[str, str]) -> str:
    """Render a self-contained initial dossier, preserving all crawl evidence."""
    lines = [PREPARED_MARKER]
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
    lines.extend([
        f"Source: {entry.source or 'unspecified'}",
        f"Discovery URL: {entry.discovery_url or 'not retained'}",
        f"Discovery date: {entry.discovery_date or 'not retained'}",
        f"Primary advert URL: {entry.url or 'not retained'}",
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
        "Record legal entity, named people, product, hiring evidence, funding/runway evidence,",
        "likely daily collaborators, risks, sources, confidence, and questions here.",
        "",
    ])
    _section(lines, "Next action")
    lines.extend([
        "- [ ] Review advert and fit",
        "- [ ] Decide whether to commission company/hiring diligence",
        "- [ ] Tailor CV / application material",
        "- [ ] Submit (then replace PREPARED status above with SUBMITTED and date)",
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
) -> list[PackResult]:
    """Create idempotent PREPARED packs for eligible GO listings.

    ``only_new`` is used by normal pipeline runs.  Backfill tooling passes
    False to prepare an explicit historical GO report once.
    """
    root = cases_root or _principal_cases_root(principal)
    current_date = today or date.today()
    existing_dirs = [path for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    results: list[PackResult] = []

    for entry in entries:
        if entry.decision != "GO":
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
            if _is_prepared(directory):
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
        text_path = directory / f"{slug}.txt"
        text_path.write_text(_pack_text(entry, advert_hash, _enrichment_for(entry.url)), encoding="utf-8")
        existing_dirs.append(directory)
        if cases_root is None:
            # Production path: state-owned pack, mirrored into the parent
            # repo's unified applications/ view as a symlink (docs/21).
            _mirror_pack_in_unified_view(directory)
        results.append(PackResult(entry, "created", directory, "prepared for review; not submitted"))
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

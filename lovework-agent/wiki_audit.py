"""Wiki QA audit (2026-10-08) — the defect classes of a principal's wiki, and
the automatic detector for each, so "when it happens tomorrow, we detect it" is
one command.

**A DEFECT is something this project produced wrongly and can put right. A GAP
is a limit of the evidence**: it is reported, never counted as a defect, because
a detector that cries wolf stops being read — an earlier version flagged bare
domains (`Checkout.com`) as "url in org name" and real companies
(`Aioi Nissay Dowa Insurance Company Of Europe SE`) as "role in org name".

This file is the merge of the two audit lines that ran in parallel on host-b
and host-a (2026-10-07/08). Both were kept: the name classes are now *mutually
exclusive per page* (most specific first) so one bad page is counted once, and
every detection either side had is still made.

Name classes, one per page, in precedence order:

  url_in_org_name      the name is a name CONCATENATED with a URL or a domain
                       tail ("Adyen_https_www.adyen.com", "LAGO_-_https_…").
                       A *bare* domain is a company name and is not this.
  recoverable_name     the name carries a tail that `lead_identity.clean_org_name`
                       recovers ("Tesco&nbsp;Welwyn …" -> "Tesco",
                       "Jefferies_New_York_NY" -> "Jefferies").
  not_an_org           the name is not an organisation at all — a salary string,
                       a mail-digest line, a sentence from the advert body, a
                       street address, an opaque token, a bare role
                       (`lead_identity.is_non_org_name`).
  junk_org_name        a placeholder, a UI string, a salary band or an age
                       (`lead_identity.is_noise_company`).
  role_in_org_name     a name long enough to be a mis-split header
                       (`lead_identity.is_implausible_header`).

Per-page and per-block classes:

  name_mangle          the stem lost characters its own H1 kept
                       ("Deutsche_B_rse" for "Deutsche Börse") — the filename
                       rule transliterates now.
  entity_artifacts     an HTML entity or an `_xNN_` escape survived into the
                       page or a block ("&#39;", "&nbsp;").
  foreign_principal_links   a document in this principal's tree links into
                       another principal's tree.
  foreign_block_schema a block that is not the org-page writer's shape (the
                       backfill v1 report shape, or a hand-built note).
  junk_title           an actionable GO/MAYBE block whose title is not a role
                       (`is_noise_title`); non-actionable history is a gap.
  implausible_location an actionable GO/MAYBE block whose location is not a
                       place; non-actionable history is a gap.
  tracking_urls        an advert URL whose tracking params can be stripped
                       (`wiki_store.clean_url` changes it).
  duplicate_org_blocks two blocks in one page with the same identity and day.
  duplicate_org_pages  two pages for one company ("Guild.ai" / "Guild_ai").
  dead_local_links     a local link that resolves to nothing.
  masked_local_links   a local link that only resolves through a fallback — the
                       dashboard's fold/rename map, not the literal path.

Gaps (reported, not defects):

  linkless_go_maybe    a GO/MAYBE in the *current* block format with no advert
                       URL: the source never captured one. Whether the matcher
                       should GO an unactionable row is a policy call, not a
                       repair.
  legacy_linkless      the same, in the pre-URL-field block format — history.
  junk_title_history   a non-actionable block (FLAG/DROP/UNSCORED) whose title
                       is not a role — history, not work.
  implausible_location_history  the same for a location that is not a place.
  sealed_report_links  dead/masked local links inside sealed reports — the
                       report is evidence and is never rewritten.
  redirect_url         the URL is a mail redirector (`email.lensa.com/f/a/…`),
                       not the advert; the fix belongs at the source.

Usage:
    wiki_audit.py --profile lj [--json] [--examples] [--no-links]
                  [--write-inventory]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import config
from lead_identity import (clean_org_name, is_implausible_header,
                           is_ineligible_work_location, is_noise_company,
                           is_noise_location, is_noise_title, is_non_org_name,
                           is_role_shaped_name)

JUNK_NAMES = {"more", "n", "confidential", "confidential company",
              "undisclosed", "unknown", ""}

#: Decisions a recommendation class applies to (an ineligible DROP is correct).
_ACTIONABLE_DECISIONS = ("GO", "MAYBE")

#: A finding block starts at a `### ` heading. Two shapes exist — the crawl
#: report's `### <date> — <title>` and the backfilled page's `### <title>` — so
#: the heading delimits a block, not the date.
BLOCK_RE = re.compile(r"(?m)^### ")
#: the canonical org-page heading: a date, an em dash, a title
CANONICAL_HEAD_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}) — (.+?)\s*$")
DECISION_RE = re.compile(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE|FLAG|DROP|UNSCORED)\b")
NOTE_RE = re.compile(r"(?m)^- \*\*Note\*\*: \S")
URL_LINE_RE = re.compile(r"(?m)^- \*\*URL\*\*: (\S+)")
LOCATION_LINE_RE = re.compile(r"(?m)^- \*\*Location\*\*: (.+)$")
H1_RE = re.compile(r"^# (.+)$", re.M)

#: markdown links, including the angle-bracket form used for long local paths
LINK_RE = re.compile(r"\]\(<?([^)>\s]+)>?\)")
ENTITY_RE = re.compile(r"&(?:amp|apos|quot|lt|gt|#\d+|#x[0-9a-fA-F]+);|_x[0-9a-fA-F]{2}_")

#: Hosts that are click-tracking redirectors rather than an advert page.
_REDIRECT_HOST_RE = re.compile(
    r"email\.lensa\.com/f/a/|johnsonjobs\.com/v1/redirect|/v1/redirect/|"
    r"lnkd\.in/|/f/a/|/ls/click|/track/click",
    re.I,
)


def _norm_key(name: str) -> str:
    try:
        from company_events import norm_key
        return norm_key(name) or ""
    except Exception:
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _ascii_fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if ord(c) < 128)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _ascii_fold(s).lower())


def _is_redirect_url(url: str) -> bool:
    """A URL that is a tracking redirector, not the advert itself."""
    return len(url) > 300 or bool(_REDIRECT_HOST_RE.search(url))


def _blocks(text: str) -> list[str]:
    """Every `### ` block of a page, heading included."""
    parts = BLOCK_RE.split(text)
    return ["### " + p for p in parts[1:]]


def _head(block: str) -> str:
    return block.split("\n", 1)[0]


def _identity(org: str, block: str) -> str:
    from wiki_store import block_identity

    try:
        return block_identity(org, block)
    except Exception:                     # pragma: no cover - defensive
        return _head(block)


def _day(block: str) -> str:
    m = CANONICAL_HEAD_RE.match(_head(block))
    return m.group(1) if m else ""


class _LinkResolver:
    """Resolve a local link exactly as the dashboard does.

    A link in a report or an org page is an absolute URL path
    (`/principals/<p>/wiki/orgs/<slug>.md`). The dashboard splits it into
    principal + area, maps the area through
    `dashboard_server.PUBLISHED_PRINCIPAL_AREAS`, and asks
    `doc_serve.resolve_served_path` for the file. This asks the same two
    functions, so the count the brief shows is the count a reader meets — the
    previous version mirrored only the repo-root shape the *unit test* used, and
    reported 4,516 "resolvable" links that the live route 404s (2026-10-07,
    link-integrity audit: 0 of 582 lj / 886 vj / 74 kj repaired).
    """

    PRINCIPAL_RE = re.compile(r"^principals/(?P<p>[^/]+)/(?P<area>[^/]+)/(?P<rest>.+)$")

    def __init__(self, lovework_root: Path):
        self.root = lovework_root
        self._roots: dict[tuple[str, str], Path | None] = {}
        # the same broken link appears in dozens of reports; the fold fallback
        # globs the orgs directory, so resolve each distinct link once
        self._memo: dict[tuple[str, str], tuple[Path | None, bool]] = {}

    def _area_root(self, principal: str, area: str):
        """The published root for `principal`/`area`, inside THIS audit's tree.

        The mapping (which areas are public at all) is imported from the
        server so the two cannot disagree about the policy; the paths are
        derived from the audit's own root so the audit measures the tree it
        was handed, not whatever tree the server process happens to run in.
        """
        key = (principal, area)
        if key not in self._roots:
            try:
                from dashboard_server import PUBLISHED_PRINCIPAL_AREAS

                relative = PUBLISHED_PRINCIPAL_AREAS.get(area)
            except Exception:                 # pragma: no cover - defensive
                relative = None
            state = self.root / "state" / principal
            self._roots[key] = (state / relative
                                if relative and state.is_dir() else None)
        return self._roots[key]

    def resolve(self, rel: str):
        """(target, used_fallback) — (None, False) when the link is dead."""
        rel = rel.lstrip("/")
        m = self.PRINCIPAL_RE.match(rel)
        if not m:
            return None, False
        root = self._area_root(m.group("p"), m.group("area"))
        if root is None:
            return None, False                # area not published -> 404
        rest = m.group("rest")
        key = (str(root), rest)
        if key not in self._memo:
            try:
                from doc_serve import resolve_served_path

                target = resolve_served_path(rest, root)
            except Exception:                 # pragma: no cover - defensive
                target = None
            self._memo[key] = ((target, not (root / rest).exists())
                               if target is not None else (None, False))
        return self._memo[key]


FOREIGN_PRINCIPAL_RE = re.compile(r"/principals/([a-z0-9_]+)/")


def _foreign_principals(text: str, principal: str) -> list[str]:
    """Principals other than `principal` whose tree this document links into."""
    out: list[str] = []
    for m in FOREIGN_PRINCIPAL_RE.finditer(text):
        p = m.group(1)
        if p != principal and p not in out:
            out.append(p)
    return out


def _lead_directories(principal: str):
    """advert identity → lead directory, or None when the feature is unused.

    The **expired** pile is included: a retired advert is closed, not missing, so
    its page existing there is what "has a lead page" means now (docs/45). Without
    this the audit would call every retirement a defect on the day it happened.
    """
    try:
        from leads import lead_index, leads_root

        roots = [leads_root(principal), config.STATE_DIR / principal / "expired"]
        if not any(root.is_dir() for root in roots):
            return None
        index = lead_index(principal)
        for root in roots:
            if root.is_dir():
                for directory in root.iterdir():
                    if directory.is_dir():
                        index.setdefault(directory.name, directory)
        return index
    except Exception:
        return None


def _pack_directories(principal: str):
    """advert identity → pack directory, read once per audit.

    The class it serves is the product's own promise: a GO is prepared material
    for the principal, so a GO without a pack is a defect the wiki should show
    and the daily pass should heal (LJ, 2026-10-09: "all GO-s must have packs!
    That's the point of LoveWork. To save the principal time.").
    """
    try:
        from application_packs import pack_index

        # `None` (not `{}`) when the principal has no cases root of its OWN: the
        # pack machinery is not in use there, so "a GO has no pack" is not a
        # statement the audit can make — and a fixture wiki in a temp state is
        # exactly that case. Deliberately not `_principal_cases_root`, which
        # falls back to the unified view and would make every fixture look
        # equipped.
        if not (config.STATE_DIR / principal.lower() / "applications").is_dir():
            return None
        index = pack_index(principal)
        # The **gone** pile counts as having a pack: a retired GO's material is
        # there, and calling its retirement a missing pack would be false
        # (docs/45). Keyed by the directory name, which is what the pack index
        # uses too.
        gone = config.STATE_DIR / principal.lower() / "gone"
        if gone.is_dir():
            for directory in gone.iterdir():
                if directory.is_dir():
                    index.setdefault(directory.name, directory)
        return index
    except Exception:
        return None


#: A DROP that says the principal already applied — the reapply rule's own words.
_ALREADY_APPLIED_RE = re.compile(
    r"applied for this|already applied|re-?appl(y|ying) is ineffective|cooldown",
    re.I)


def _already_applied(page_text: str, block: str) -> bool:
    """True when a later block dropped this same advert because LJ applied."""
    url = re.search(r"(?m)^- \*\*URL\*\*: (\S+)", block)
    head = _head(block)
    title = head.split(" — ", 1)[1].strip().removesuffix("🆕").strip() if " — " in head else ""
    for later in re.split(r"(?m)^### ", page_text)[1:]:
        # Same advert by link OR by title: the block that records the
        # application often carries no URL (the 2026-06-24 myDataValue DROP says
        # "Already applied for this exact role" and nothing else).
        same = (url and url.group(1) in later) or (title and f"— {title}" in later)
        if not same:
            continue
        if re.search(r"(?m)^- \*\*Decision\*\*: DROP\b", later) and \
                _ALREADY_APPLIED_RE.search(later):
            return True
    return False


def _unpackable_title(head: str) -> bool:
    """True when a GO block's title is not a role, so no pack can be named."""
    try:
        from application_packs import _title_requires_repair
        from lead_identity import is_noise_title

        title = head.split(" — ", 1)[1] if " — " in head else head
        title = title.strip().removesuffix("🆕").strip()
        return bool(is_noise_title(title) or _title_requires_repair(title))
    except Exception:
        return False


def _title_from_head(head: str) -> str:
    """The advert's title as the heading writes it (`### DATE — Title`).

    One reader for the shape, so the pack key and the retirement key cannot
    disagree — passing the raw heading to `retire.is_retired` made the compact key
    `###20260812foundingsoftwareengineer`, which matches nothing.
    """
    title = head.split(" — ", 1)[1] if " — " in head else head
    return title.strip().removesuffix("🆕").strip()


def _pack_url_key(org: str, block: str) -> str:
    """The pack lookup key on the advert URL, or "" when the block has none.

    `pack_index` records this key because a title can drift between the pack and
    the wiki block; the audit must ask the same question or a pack that exists
    reads as "no pack" (the Elman / John Wiley / Soniox rows, 2026-11-04).
    """
    m = URL_LINE_RE.search(block)
    if not m:
        return ""
    # The organisation is deliberately not part of the key: it is written twice
    # and drifts (Elman vs Elman (Deep Science Ventures)), while the advert link
    # is copied verbatim (lj's Elman GO, 2026-11-04).
    from lead_identity import advert_url_key

    return advert_url_key(m.group(1))


def _title_key(org: str, head: str) -> str:
    """The org+title pack key (tolerates an observation day with no URL)."""
    try:
        from application_packs import pack_title_key

        return pack_title_key(org, _title_from_head(head))
    except Exception:
        return ""


def _allowed_work_places(principal: str) -> list[str]:
    """The principal's declared allowed workplaces, from its own work_auth.md.

    Absent (or unreadable) returns [] — the class then reports nothing, because
    a principal who has not declared the rule is not subject to it.
    """
    try:
        import config
        text = (config.PROFILES_DIR / principal / "work_auth.md").read_text(
            encoding="utf-8", errors="ignore")
        from matcher import _allowed_work_places as _parse
        return _parse(text)
    except Exception:
        return []


def _local_links(text: str) -> list[str]:
    """Local link targets (repo-relative) in a markdown document, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for m in LINK_RE.finditer(text):
        rel = m.group(1)
        if rel.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        rel = rel.lstrip("/")
        if not rel.startswith("principals/"):
            continue
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


#: A name CONCATENATED with a URL or a domain tail. The leading `\S` is the
#: whole point: a BARE domain is a company name ("Checkout.com", "IPinfo.io").
_URL_TAIL_IN_NAME_RE = re.compile(
    r"\S[ _].*(https?://|www\.)|\S[ _][A-Za-z0-9-]+[._](com|org|net|io|pro|dev)$",
    re.I,
)


def audit_wiki(wiki_root: Path, *, examples_per_class: int = 4,
               check_links: bool = True, include_attic: bool = False,
               include_reports: bool = False) -> dict:
    """One wiki tree → defect classes, gaps and examples. Pure read.

    `include_attic` audits the quarantine too (the full scope); by default the
    attic is excluded from the classes and reported as a gap, because its
    blocks live on in the pages that are audited.
    """
    defects: dict[str, int] = {}
    gaps: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    lovework_root = Path(wiki_root).resolve().parents[2]
    resolver = _LinkResolver(lovework_root)
    principal = Path(wiki_root).parent.name
    # The principal's own allowed workplaces — one authority shared with the
    # matcher's kill (docs/27: observe state, never assert it).
    allowed_work_places = _allowed_work_places(principal)
    pack_dirs = _pack_directories(principal)
    # Which adverts are GO **now** — one authority, shared with the pack pass
    # (`application_packs.go_identities`), so the audit cannot demand a pack the
    # repair will never cut.
    current_go: set = set()
    try:
        from application_packs import go_identities

        current_go = go_identities(principal)
    except Exception as exc:                    # never silently swallow
        print(f"[audit] the current-GO set could not be read: {exc}", file=sys.stderr)
    lead_dirs = _lead_directories(principal)
    # The two closing piles, read once: an advert that is closed is not a missing
    # pack, a missing lead page or a stale decision (docs/45).
    try:
        import retire

        retired_keys = retire.retired_keys(principal)
        retired_url_set = retire.retired_urls(principal)
    except Exception as exc:                    # never silently swallow
        retired_keys, retired_url_set = set(), set()
        print(f"[audit] the closing piles could not be read: {exc}", file=sys.stderr)

    def _retired(org: str, role: str, block: str = "") -> bool:
        """Is this advert in a closing pile? The URL decides when there is one.

        The organisation as a *page* names it (`40grid---full-time-remote`) and as
        a *pile* records it (`40grid`) are different strings, so the `(org, role)`
        key is a fallback and the advert URL — verbatim on both sides — is the
        test (found 2026-10-14).

        **Two ways this check silently said "not retired" (fixed 2026-10-14):**
        the caller passed the whole *heading* (`"### 2026-08-12 — Founding Software
        Engineer"`) where the pile stores a *title*, so `is_retired`'s compact key
        could never match; and the URL was compared **raw against a canonical set**
        (the piles store `advert_url_key`, which until today threw the query away,
        so an HN advert's `…/item?id=…` never met the pile's `…/item`). Both sides
        now answer in the same form. The cost of getting this wrong was 45 GOs
        reported as "missing a pack" when their packs were in `gone/`.
        """
        import retire
        from lead_identity import advert_url_key

        for found in URL_LINE_RE.finditer(block or ""):
            if advert_url_key(found.group(1)) in retired_url_set:
                return True
        return retire.is_retired(org, role, retired_keys)

    def hit(bucket: dict, name: str, example: str) -> None:
        bucket[name] = bucket.get(name, 0) + 1
        if len(examples.setdefault(name, [])) < examples_per_class:
            examples[name].append(example)

    # **Every organisation page, in both directories** (2026-10-15). The kind a page
    # records decides whether it lives in `orgs/` or `agencies/`, and an audit that
    # globbed only `orgs/` would report a cleaner tree the moment the agencies moved
    # — "the audit got quieter, not cleaner", which this file already paid for once.
    from wiki_store import org_page_paths

    files = org_page_paths(wiki_root)
    # The attic is a QUARANTINE, not a gap in the tree: a page whose name was
    # not an organisation was moved there and its blocks were copied into a
    # page that IS audited, so auditing the residue would double-count them.
    # It is therefore excluded by default and REPORTED, never silent — a
    # detector whose count can be reduced by moving a page is not measuring the
    # tree ("the audit got quieter, not cleaner", 2026-10-08 forensics).
    attic = wiki_root / "_attic"
    attic_files = sorted(attic.glob("*.md")) if attic.is_dir() else []
    gaps["attic_pages"] = len(attic_files)
    if include_attic:
        files = files + attic_files
    by_key: dict[str, list[str]] = {}
    # (advert identity → example) of GO adverts with no pack. Declared ONCE:
    # inside the page loop it would reset per page and the class would read 0
    # (found the moment it did).
    missing_pack: dict[str, str] = {}
    # …and (advert identity → example) of GO adverts whose pack exists but carries
    # no material. Two classes, not one, because they are two different jobs: a
    # missing pack needs a scaffold cut, an unprepared one needs the loop to write
    # it (`decide._go_pack_work`).
    unprepared: dict[str, str] = {}
    missing_lead: dict[str, str] = {}
    # The same advert observed on five days is five blocks: a *gap* per advert, so
    # the class does not rise merely because a row was re-observed (the same rule
    # `missing_pack` follows).
    unpackable: dict[str, str] = {}
    already_applied: dict[str, str] = {}
    for f in files:
        stem = f.stem
        # ── name classes: one per page, most specific first ──────────────
        if stem != "_Unattributed" and _URL_TAIL_IN_NAME_RE.search(stem):
            hit(defects, "url_in_org_name", stem)
        else:
            cleaned = clean_org_name(stem)
            from wiki_store import WikiStore
            renamed_to = WikiStore._safe_filename(cleaned) if cleaned else ""
            if stem != "_Unattributed" and renamed_to and renamed_to != stem:
                hit(defects, "recoverable_name", f"{stem} -> {renamed_to}")
            elif stem != "_Unattributed" and is_non_org_name(stem):
                hit(defects, "not_an_org", stem)
            elif stem != "_Unattributed" and (
                    is_noise_company(stem.replace("_", " "))
                    or stem.replace("_", " ").strip().lower() in JUNK_NAMES
                    or len(stem.strip("_")) < 2):
                hit(defects, "junk_org_name", stem)
            elif stem != "_Unattributed" and is_role_shaped_name(
                    stem.replace("_", " ")):
                hit(defects, "role_in_org_name", stem)
        by_key.setdefault(_norm_key(stem), []).append(stem)
        if ENTITY_RE.search(stem):
            hit(defects, "entity_artifacts", f"{stem} (in the page NAME)")

        text = f.read_text(encoding="utf-8", errors="ignore")
        h1 = H1_RE.search(text)
        if h1:
            # Compare against the name the *writer* would mint from the H1 — one
            # authority, so the detector and the filename rule cannot disagree
            # about whether a stem is mangled.
            from wiki_store import WikiStore
            would_be = WikiStore._safe_filename(h1.group(1).strip())
            # Separators are not letters: `ADVANCE_AI` (minted when a mark
            # became `_`) and `ADVANCEAI` (minted now that the mark is dropped)
            # are the same name, and renaming a page over a separator is churn.
            def _fold(s: str) -> str:
                return s.casefold().replace("_", "")
            if would_be and _fold(would_be) != _fold(stem):
                hit(defects, "name_mangle", f"{stem} ← H1 {h1.group(1)[:40]}")
        org_label = h1.group(1).strip() if h1 else stem.replace("_", " ")
        foreign = _foreign_principals(text, principal)
        if foreign:
            hit(defects, "foreign_principal_links",
                f"{stem}: -> {', '.join(foreign)}")

        seen_identity: dict[tuple, int] = {}
        first_head: dict[tuple, str] = {}
        for block in _blocks(text):
            head = _head(block)
            day = _day(block)
            if not day or not (DECISION_RE.search(block) or NOTE_RE.search(block)):
                # a block written by someone other than the org-page writer:
                # the report shape (backfill v1) or a hand-built note
                hit(defects, "foreign_block_schema", f"{stem}: {head[:60]}")
            decision = DECISION_RE.search(block)
            actionable = bool(decision
                              and decision.group(1) in _ACTIONABLE_DECISIONS)
            title = head.split(" — ", 1)[1] if " — " in head else ""
            if title and is_noise_title(title.replace("🆕", "").strip()):
                if actionable:
                    hit(defects, "junk_title", f"{stem}: {title[:60]}")
                else:
                    hit(gaps, "junk_title_history", f"{stem}: {title[:60]}")
            loc = LOCATION_LINE_RE.search(block)
            if loc:
                value = loc.group(1).strip()
                if is_noise_location(value) or len(value) > 90:
                    if actionable:
                        hit(defects, "implausible_location", f"{stem}: {value[:70]}")
                    else:
                        hit(gaps, "implausible_location_history",
                            f"{stem}: {value[:70]}")
                # An ELIGIBILITY ruling, not a field defect: the principal
                # declares its allowed workplaces in work_auth.md, and a GO/MAYBE
                # whose location sits outside them is a recommendation the
                # principal cannot take (the Gradium/Paris canary, 2026-10-09:
                # GO 9.7 and six packs prepared while the file said the
                # opposite). One predicate with the matcher's kill, so the audit
                # and the scorer cannot disagree about "where is this job".
                elif (allowed_work_places and is_ineligible_work_location(value)
                        and DECISION_RE.search(block)
                        and DECISION_RE.search(block).group(1) in _ACTIONABLE_DECISIONS):
                    hit(defects, "ineligible_location", f"{stem}: {value[:70]}")
            for m in ENTITY_RE.finditer(block):
                hit(defects, "entity_artifacts", f"{stem}: {m.group(0)!r}")
                break
            key = (_identity(org_label, block), day)
            seen_identity[key] = seen_identity.get(key, 0) + 1
            # Every GO must lead to prepared material: the pack is the product
            # (LJ, 2026-10-09). One pack per ADVERT, not per observation day —
            # the same advert observed five times is five rows and still one
            # pack — so the class counts distinct advert identities.
            decision = DECISION_RE.search(block)
            # The advert's identity, computed once: both branches below ask about
            # the same advert, and one of them now asks whether it is *currently*
            # GO (`current_go`, the same authority the pack pass uses).
            identity = _identity(org_label, block)
            url_key = _pack_url_key(org_label, block)
            if (lead_dirs is not None and decision
                    and decision.group(1) in ("MAYBE", "FLAG", "DROP")):
                if identity not in lead_dirs and _title_key(org_label, head) not in lead_dirs:
                    # A retired advert is closed, not missing: its page is in the
                    # `expired` pile and its row is in that pile's index (docs/45).
                    # Without this, every retirement would be reported as a defect
                    # on the day it happened — the audit measuring its own work.
                    if not _retired(org_label, _title_from_head(head), block):
                        missing_lead.setdefault(identity, f"{stem}: {head[:60]}")
            # A GO **block** is history when a later block re-judged the same
            # advert: 266 adverts in lj carry some GO block, 121 are currently GO,
            # and the index reads the newest (2026-10-14). Demanding a pack for the
            # other 145 is the audit measuring a verdict nobody holds.
            if (pack_dirs is not None and decision and decision.group(1) == "GO"
                    and identity in current_go):
                if (identity not in pack_dirs
                        and _title_key(org_label, head) not in pack_dirs
                        and url_key not in pack_dirs):
                    # A GO whose TITLE is not a role cannot be packed — a pack
                    # named after "Full-Time" is meaningless. That is the
                    # junk-title class wearing a pack-shaped hat, so it is
                    # reported apart: `go_without_pack` is the class the daily
                    # repair drains to zero, `go_unpackable` is data reality.
                    if _retired(org_label, _title_from_head(head), block):
                        # …same for a retired GO: its pack is in `gone/`.
                        pass
                    elif _unpackable_title(head):
                        unpackable.setdefault(identity, f"{stem}: {head[:60]}")
                    elif _already_applied(text, block):
                        # The page carries a LATER block that dropped this same
                        # advert because the principal applied: no pack is
                        # needed, and calling it a missing pack would be wrong
                        # (the 2026-06-23 CData GO, applied the same day).
                        already_applied.setdefault(identity, f"{stem}: {head[:60]}")
                    else:
                        missing_pack.setdefault(identity, f"{stem}: {head[:60]}")
                else:
                    # **It has a pack — but can the principal apply with it?**
                    # LJ, 2026-10-19: *"either the application pack is created =>
                    # remains on GO; or if not possible (the advert is gone and we no
                    # longer have it) => then this should not be in GO, it should be
                    # in leads."* The directory is not the promise; the material is.
                    # This class exists because the audit asked the weaker question
                    # and answered "fine" for 44 of 51 GO rows (measured the day it
                    # landed) — `go_without_pack` counted scaffolds as packs.
                    directory = (pack_dirs.get(identity)
                                 or pack_dirs.get(_title_key(org_label, head))
                                 or pack_dirs.get(url_key))
                    if directory is not None and _retired(org_label,
                                                          _title_from_head(head),
                                                          block):
                        # **A retired advert is not an unprepared one** (2026-10-19):
                        # its pack moved to a pile, and demanding material for a case
                        # the decision space no longer holds is the audit measuring
                        # its own work — the same rule the `missing_pack` branch above
                        # already follows.
                        pass
                    elif directory is not None:
                        try:
                            from application_packs import pack_readiness

                            state = pack_readiness(Path(directory))
                        except Exception as exc:    # never silently swallow
                            print(f"[audit] pack readiness unreadable: {exc}",
                                  file=sys.stderr)
                            state = {"ready": True, "why": ""}
                        if not state["ready"]:
                            unprepared.setdefault(
                                identity, f"{stem}: {head[:60]} — {state['why']}")
            first_head.setdefault(key, head)
            for m in URL_LINE_RE.finditer(block):
                url = m.group(1)
                from wiki_store import clean_url
                if clean_url(url) != url:
                    # Fixable: the tracking params come off and the advert URL
                    # underneath is the real destination.
                    hit(defects, "tracking_urls", f"{stem}: {url[:60]}…")
                elif _is_redirect_url(url):
                    # Not fixable here: the source captured a mail redirector,
                    # not the advert URL.
                    hit(gaps, "redirect_url", f"{stem}: {url[:60]}…")
            if "**Decision**: GO" in block or "**Decision**: MAYBE" in block:
                if "**URL**" not in block:
                    # A current-format block is one the pipeline writes today
                    # (it carries the multi-axis lines); a legacy block predates
                    # the URL field, so its missing link is history.
                    current = "**Action**:" in block or "**Fit**:" in block
                    hit(gaps, "linkless_go_maybe" if current else "legacy_linkless",
                        f"{stem}: {head[:60]}")

        for key, n in seen_identity.items():
            if n > 1:
                hit(defects, "duplicate_org_blocks",
                    f"{stem}: {first_head[key][:60]} (x{n}, {n - 1} redundant)")

        if check_links:
            for rel in _local_links(text):
                target, fallback = resolver.resolve(rel)
                if target is None:
                    hit(defects, "dead_local_links", f"{stem}: {rel[:70]}")
                elif fallback:
                    hit(defects, "masked_local_links", f"{stem}: {rel[:70]}")

    for _identity_missing, _example in unprepared.items():
        hit(defects, "go_unprepared", _example)
    for _identity_missing, _example in missing_pack.items():
        hit(defects, "go_without_pack", _example)
    for _identity_missing, _example in missing_lead.items():
        hit(defects, "lead_without_page", _example)
    for _identity_missing, _example in unpackable.items():
        hit(gaps, "go_unpackable", _example)
    for _identity_missing, _example in already_applied.items():
        hit(gaps, "go_already_applied", _example)

    for key, stems in by_key.items():
        if key and len(stems) > 1:
            for stem in stems[1:]:
                hit(defects, "duplicate_org_pages", f"{stem} ↔ {stems[0]}")

    # Reports are sealed evidence, never rewritten. Their local links are
    # measured as a GAP by default: a dead link in a 2026-08 report is history,
    # not a defect someone can repair today. `include_reports` keeps the old
    # link-integrity view for an explicit audit.
    if check_links:
        reports = wiki_root / "reports"
        report_dead = report_masked = 0
        for rep in sorted(reports.glob("*.md")) if reports.is_dir() else []:
            text = rep.read_text(encoding="utf-8", errors="ignore")
            foreign = _foreign_principals(text, principal)
            if foreign:
                hit(defects, "foreign_principal_links",
                    f"{rep.name}: -> {', '.join(foreign)}")
            for rel in _local_links(text):
                target, fallback = resolver.resolve(rel)
                if target is None:
                    report_dead += 1
                    if include_reports:
                        hit(defects, "dead_local_links", f"{rep.name}: {rel[:70]}")
                elif fallback:
                    report_masked += 1
                    if include_reports:
                        hit(defects, "masked_local_links", f"{rep.name}: {rel[:70]}")
        if report_dead or report_masked:
            gaps["sealed_report_links"] = report_dead + report_masked
            examples.setdefault("sealed_report_links", []).append(
                f"{report_dead} dead + {report_masked} masked in sealed reports")

    return {"wiki": str(wiki_root), "org_pages": len(files),
            "classes": defects, "gaps": gaps,
            "total": sum(defects.values()),
            "examples": examples}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--examples", action="store_true")
    parser.add_argument("--include-attic", action="store_true",
                        help="audit wiki/_attic/ too (the quarantine, off by default)")
    parser.add_argument("--include-reports", action="store_true",
                        help="count dead/masked links in sealed reports as defects "
                             "(default: they are a gap — reports are never rewritten)")
    parser.add_argument("--no-links", action="store_true",
                        help="skip link resolution (faster)")
    parser.add_argument("--write-inventory", action="store_true",
                        help="write wiki/defects-<p>-<date>.md for review")
    args = parser.parse_args(argv)

    root = config.STATE_DIR / args.profile / "wiki"
    if not root.is_dir():
        print(f"no wiki at {root}", file=sys.stderr)
        return 1
    result = audit_wiki(root, check_links=not args.no_links,
                         include_attic=args.include_attic,
                         include_reports=args.include_reports)
    if args.write_inventory:
        import datetime as _dt

        day = _dt.date.today().isoformat()
        out_path = root / f"defects-{args.profile}-{day}.md"
        lines = [f"# {args.profile} wiki defects — {result['total']} on file "
                 f"({day})", "",
                 f"{result['org_pages']} org pages. Classes, worst first. "
                 f"Detectors: `wiki_audit.py --profile {args.profile} "
                 f"--examples`.", ""]
        for name, count in sorted(result["classes"].items(),
                                  key=lambda kv: -kv[1]):
            lines.append(f"## {name} ({count})")
            lines.append("")
            for ex in result["examples"].get(name, []):
                lines.append(f"- {ex}")
            lines.append("")
        if result["gaps"]:
            lines.append("## gaps (evidence limits, not defects)")
            lines.append("")
            for name, count in sorted(result["gaps"].items(),
                                      key=lambda kv: -kv[1]):
                lines.append(f"- {name}: {count}")
            lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"inventory: {out_path}")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{args.profile}: {result['org_pages']} org pages, "
              f"{result['total']} defect(s)")
        for k, v in sorted(result["classes"].items(), key=lambda kv: -kv[1]):
            print(f"  {k:22s} {v}")
        if result["gaps"]:
            print("  gaps (evidence limits, not defects):")
            for k, v in sorted(result["gaps"].items(), key=lambda kv: -kv[1]):
                print(f"    {k:20s} {v}")
        if args.examples:
            for k, ex in result["examples"].items():
                print(f"  [{k}]")
                for e in ex:
                    print(f"    {e}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

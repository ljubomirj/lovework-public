"""
Outcome -> assessment linkage (business plan step 0).

The outcome ledger records what happened after a recommendation. The
assessment ledger records what LoveWork recommended. Nothing joined them, so
the system could not answer "did the things we recommended work?".

This module owns that join. Two halves:

1. **Identity at capture.** An outcome carries the identifiers needed to
   attach it to the assessment that produced it. A case directory created by
   ``application_packs.prepare_go_cases`` already contains an exact
   ``Advert identity: <hash>`` line plus organisation, position, initial
   decision and score. ``read_case_identity`` reads it back; nothing is
   invented when it is absent.

2. **Resolution.** ``AssessmentIndex`` indexes the assessment ledger and
   ``resolve_outcome`` walks a fixed, conservative ladder:

   ====== ============================== ========== ================================
   Tier   Method                         Confidence Evidence
   ====== ============================== ========== ================================
   1      ``exact_advert_hash``          1.00       key carried by the outcome row
   1      ``case_identity``              1.00       key read from the case dossier
   1      ``exact_url``                  0.95       normalised advert URL on the row
   1      ``case_url``                   0.90       advert URL found in the case text
   2      ``org_title_exact``            0.70       normalised organisation + title
   2      ``org_title_fuzzy``            0.50       organisation + >=0.90 title ratio
   3      ``org_only``                   0.00       association, not a join
   -      ``none``                       0.00       no evidence
   ====== ============================== ========== ================================

   **Ambiguity policy.** At every rung, candidates are collapsed to the set of
   distinct ``advert_hash`` values. Exactly one distinct advert joins. Two or
   more means the evidence cannot distinguish the adverts, and the row is
   recorded as ``ambiguous_<rung>`` with an empty ``matched_assessment_id``.
   A wrong join is worse than no join: a mis-joined outcome would corrupt
   score calibration silently, while an unjoined one is visibly missing.
   When several *rows* share the winning advert (the same advert assessed on
   several runs), the earliest ``observed_at`` row is used — that is the
   assessment that produced the recommendation.

Legacy rows written before the capture fix carry only the organisations
scanned during one crawl plus a hash built with an empty advert URL, so most
of them cannot be resolved at Tier 1. That is a data limitation, not a
resolver limitation, and ``join_dataset`` reports it as such.

The join is written to a new append-only ledger, ``outcome_joins.jsonl``, and
one metric row per run goes to ``join_metrics.jsonl``. Neither the assessment
ledger nor the outcome ledger is rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import difflib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

logger = logging.getLogger(__name__)

POLICY_VERSION = "outcome-join-v1"
JOINS_FILENAME = "outcome_joins.jsonl"
METRICS_FILENAME = "join_metrics.jsonl"

# Business plan target (docs/29-business-model.md, §6 and §11).
TARGET_TIER1_JOINS = 100
TARGET_REACTIONS_PER_MONTH = 20

# ── rungs ────────────────────────────────────────────────────────────────
METHOD_EXACT = "exact_advert_hash"
METHOD_CASE_IDENTITY = "case_identity"
METHOD_EXACT_URL = "exact_url"
METHOD_CASE_URL = "case_url"
METHOD_ORG_TITLE = "org_title_exact"
METHOD_ORG_TITLE_FUZZY = "org_title_fuzzy"
METHOD_ORG_ONLY = "org_only"
METHOD_NONE = "none"

TIER_BY_METHOD = {
    METHOD_EXACT: 1,
    METHOD_CASE_IDENTITY: 1,
    METHOD_EXACT_URL: 1,
    METHOD_CASE_URL: 1,
    METHOD_ORG_TITLE: 2,
    METHOD_ORG_TITLE_FUZZY: 2,
    METHOD_ORG_ONLY: 3,
    METHOD_NONE: 0,
}
CONFIDENCE_BY_METHOD = {
    METHOD_EXACT: 1.0,
    METHOD_CASE_IDENTITY: 1.0,
    METHOD_EXACT_URL: 0.95,
    METHOD_CASE_URL: 0.9,
    METHOD_ORG_TITLE: 0.7,
    METHOD_ORG_TITLE_FUZZY: 0.5,
    METHOD_ORG_ONLY: 0.0,
    METHOD_NONE: 0.0,
}
# A join at these methods identifies one advert, not just one organisation.
TIER1_METHODS = frozenset({METHOD_EXACT, METHOD_CASE_IDENTITY, METHOD_EXACT_URL, METHOD_CASE_URL})
# Association-only rungs. Kept in the ledger for cooldown / reapply logic
# (docs/05-matcher.md) but never counted as a join.
ASSOCIATION_METHODS = frozenset({METHOD_ORG_ONLY})

FUZZY_MIN_RATIO = 0.90
TIER1_WINDOW_DAYS = 30
# A case .txt holds the advert copy, notes and links; cap the read so a large
# note file cannot make the resolver slow.
MAX_CASE_FILE_BYTES = 2 * 1024 * 1024
MAX_CASE_URLS = 500
URL_RE = re.compile(r"https?://[^\s<>()\]\"'`,;]+")

# ── case-dossier parsing ─────────────────────────────────────────────────
# application_packs._pack_text writes these lines into the case .txt that LJ
# keeps beside every application (lovework-agent/application_packs.py:186-206).
CASE_IDENTITY_RE = re.compile(r"^[ \t]*Advert identity:[ \t]*([0-9a-fA-F]{6,64})[ \t]*$", re.MULTILINE)
CASE_FIELD_RES = {
    "org_name": re.compile(r"^[ \t]*Organisation:[ \t]*(.+?)[ \t]*$", re.MULTILINE),
    "title": re.compile(r"^[ \t]*Position:[ \t]*(.+?)[ \t]*$", re.MULTILINE),
    "url": re.compile(r"^[ \t]*Primary advert URL:[ \t]*(.+?)[ \t]*$", re.MULTILINE),
    "decision_line": re.compile(r"^[ \t]*Initial decision:[ \t]*(.+?)[ \t]*$", re.MULTILINE),
    "first_seen": re.compile(r"^[ \t]*First seen:[ \t]*(.+?)[ \t]*$", re.MULTILINE),
}
CASE_DECISION_RE = re.compile(r"^(?P<decision>[A-Za-z]+)\s*(?:\((?P<score>[0-9]+(?:\.[0-9]+)?)\s*/\s*10\))?")
# A deliberate post-application marker, in the same style as the existing
# "LoveWork status: PREPARED - not submitted" convention in history.py.
CASE_OUTCOME_RE = re.compile(
    r"^[ \t]*LoveWork outcome:[ \t]*(?P<kind>[a-z_]+)(?:[ \t]+(?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
UNKNOWN_FIELD_VALUES = {"", "not retained", "unspecified", "unknown", "n/a", "-"}

# Tracking parameters that never identify an advert.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"gh_src", "ref", "source", "src", "trk", "trackingid", "fbclid", "gclid"}

VALID_OUTCOME_KINDS = ("applied", "application_sent", "interview", "offer", "withdrawal", "rejection", "accepted")


def _clean_field(value: str) -> str:
    text = (value or "").strip()
    return "" if text.lower() in UNKNOWN_FIELD_VALUES else text


def normalise_org_key(value: str) -> str:
    """Collapse an organisation name to a comparison key.

    Punctuation, spacing and case are dropped: "FAR.AI" and "far ai" agree.
    """
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def normalise_title_key(value: str) -> str:
    """Collapse a role title to a comparison key (lower case, single spaces)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def normalise_url(value: str) -> str:
    """Normalise an advert URL for comparison.

    Case, fragment, trailing slash and tracking parameters are removed; other
    query parameters are kept and sorted, because some ATS postings carry the
    job id in the query string.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        return text.lower()
    parts = urlsplit(text)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def normalise_hash(value: str) -> str:
    return (value or "").strip().lower()


@dataclass(frozen=True)
class CaseIdentity:
    """Exact advert identity recovered from a case directory."""

    advert_hash: str = ""
    org_name: str = ""
    title: str = ""
    url: str = ""
    decision: str = ""
    initial_score: Optional[float] = None
    first_seen: str = ""
    case_path: str = ""
    source: str = ""  # which file the identity was read from

    @property
    def is_exact(self) -> bool:
        return bool(self.advert_hash)


def parse_case_identity(text: str, *, case_path: str = "", source: str = "") -> Optional[CaseIdentity]:
    """Read an ``Advert identity:`` block out of case text. None when absent."""
    match = CASE_IDENTITY_RE.search(text or "")
    if not match:
        return None
    fields = {}
    for name, pattern in CASE_FIELD_RES.items():
        found = pattern.search(text)
        fields[name] = _clean_field(found.group(1)) if found else ""
    decision = ""
    score: Optional[float] = None
    decision_match = CASE_DECISION_RE.match(fields.get("decision_line", ""))
    if decision_match:
        decision = decision_match.group("decision").upper()
        if decision_match.group("score"):
            score = float(decision_match.group("score"))
    return CaseIdentity(
        advert_hash=normalise_hash(match.group(1)),
        org_name=fields.get("org_name", ""),
        title=fields.get("title", ""),
        url=fields.get("url", ""),
        decision=decision,
        initial_score=score,
        first_seen=fields.get("first_seen", ""),
        case_path=case_path,
        source=source,
    )


def parse_case_outcome_markers(text: str) -> list[tuple[str, str]]:
    """Read deliberate ``LoveWork outcome: <kind> [YYYY-MM-DD]`` markers.

    This is the cheap, deterministic counterpart to the passive Gmail scan:
    post-application events (interview, offer, withdrawal) are recorded on
    purpose, in the case file that already holds the advert identity, instead
    of being guessed from email subjects.
    """
    found: list[tuple[str, str]] = []
    for match in CASE_OUTCOME_RE.finditer(text or ""):
        kind = match.group("kind").strip().lower()
        if kind not in VALID_OUTCOME_KINDS:
            logger.debug("Ignoring unknown case outcome marker: %r", kind)
            continue
        found.append((kind, (match.group("date") or "").strip()))
    return found


def case_text_files(case_dir: Path) -> list[Path]:
    """Text files that may carry a case dossier.

    **Two conventions, one reader** (LJ, 2026-10-15): *"when LJ creates it, it will
    be .txt. But when lovework creates the yyyymmdd-Company_name-Opening_name — it
    should be .md not .txt."* So the dossier is asked for by the directory's own
    name, in markdown first and plain text second — a case LoveWork cut and one the
    principal wrote are both found, and neither has to know which it is.
    """
    if not case_dir.is_dir():
        return []
    from application_packs import dossier_path

    files: list[Path] = []
    named = dossier_path(case_dir)
    if named is not None:
        files.append(named)
    files.extend(p for p in sorted(case_dir.glob("*.md"))
                 if p.name not in {f.name for f in files}
                 and p.name in ("README.md",))
    for extra in sorted(case_dir.glob("*.txt")):
        if extra.name not in {f.name for f in files}:
            files.append(extra)
    return files


def read_case_identity(case_dir: Path, *, case_path: str = "") -> Optional[CaseIdentity]:
    """Read the advert identity from a case directory, or None."""
    for path in case_text_files(case_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:  # pragma: no cover - unreadable case file
            logger.debug("Could not read %s: %s", path, exc)
            continue
        identity = parse_case_identity(text, case_path=case_path or str(case_dir), source=str(path))
        if identity is not None:
            return identity
    return None


def read_case_outcome_markers(case_dir: Path) -> list[tuple[str, str]]:
    """Read all deliberate outcome markers in a case directory."""
    found: list[tuple[str, str]] = []
    for path in case_text_files(case_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, date in parse_case_outcome_markers(text):
            found.append((kind, date))
    return found


def read_case_urls(case_dir: Path) -> set[str]:
    """Normalised URLs found in a case directory's text files.

    Legacy case directories predate the dossier stamp, but LJ pasted the
    advert (or a link to it) into the case file. Those URLs are evidence, so
    the resolver may use them - under the same ambiguity policy as every other
    rung, and only against assessments of the same organisation.
    """
    urls: set[str] = set()
    for path in case_text_files(case_dir):
        try:
            if path.stat().st_size > MAX_CASE_FILE_BYTES:
                logger.debug("Skipping oversized case file %s", path)
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in URL_RE.findall(text):
            urls.add(normalise_url(raw.rstrip(".,;:")))
            if len(urls) >= MAX_CASE_URLS:
                return urls
    urls.discard("")
    return urls


def locate_case_dir(path_value: str, applications_dir: Optional[Path]) -> Optional[Path]:
    """Find the case directory for a recorded path.

    Ledger rows record the path as it existed when the row was written, which
    is frequently another host (``/opt/user/...`` on host-a). The
    directory name is the stable part, so a missing path is re-based onto this
    host's ``applications/`` root by basename. A path that resolves nowhere
    yields None - the resolver then falls through to its other rungs.
    """
    if not path_value:
        return None
    candidate = Path(path_value)
    if candidate.is_dir():
        return candidate
    if applications_dir is not None:
        rebased = Path(applications_dir) / candidate.name
        if rebased.is_dir():
            return rebased
    return None


# ── assessment index ─────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
    """Read a JSONL ledger, skipping blank lines. Missing file -> empty list."""
    path = Path(path)
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping unreadable ledger line in %s", path)
    return rows


def _observed_sort_key(row: dict) -> str:
    return str(row.get("observed_at") or row.get("first_seen") or "")


class AssessmentIndex:
    """Lookup tables over one principal's assessment ledger."""

    def __init__(self, rows: Iterable[dict]) -> None:
        self.rows: list[dict] = [row for row in rows if isinstance(row, dict)]
        self.by_hash: dict[str, list[dict]] = defaultdict(list)
        self.by_url: dict[str, list[dict]] = defaultdict(list)
        self.by_org: dict[str, list[dict]] = defaultdict(list)
        self.by_org_title: dict[tuple[str, str], list[dict]] = defaultdict(list)
        # Title keys per organisation, so the fuzzy rung never scans the whole
        # index (the ledger grows; the comparison must not grow with it).
        self.title_keys_by_org: dict[str, list[str]] = defaultdict(list)
        for row in self.rows:
            advert_hash = normalise_hash(row.get("advert_hash") or "")
            if advert_hash:
                self.by_hash[advert_hash].append(row)
            url = normalise_url(row.get("url") or "")
            if url:
                self.by_url[url].append(row)
            org_key = normalise_org_key(row.get("org_name") or "")
            if not org_key:
                continue
            self.by_org[org_key].append(row)
            title_key = normalise_title_key(row.get("title") or "")
            if title_key:
                self.by_org_title[(org_key, title_key)].append(row)
                if title_key not in self.title_keys_by_org[org_key]:
                    self.title_keys_by_org[org_key].append(title_key)

    @classmethod
    def from_file(cls, path: Path) -> "AssessmentIndex":
        return cls(load_jsonl(path))

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class OutcomeMatch:
    """The resolution verdict for one outcome row."""

    match_method: str = METHOD_NONE
    match_tier: int = 0
    match_confidence: float = 0.0
    matched_assessment_id: str = ""
    matched_advert_hash: str = ""
    matched_org_name: str = ""
    matched_title: str = ""
    matched_url: str = ""
    matched_decision: str = ""
    matched_score: Optional[float] = None
    matched_run_id: str = ""
    matched_observed_at: str = ""
    matched_assessment_count: int = 0
    candidate_count: int = 0
    ambiguous: bool = False
    note: str = ""

    @property
    def is_join(self) -> bool:
        """True when the outcome is attached to one specific assessment."""
        return bool(self.matched_assessment_id) and self.match_method not in ASSOCIATION_METHODS and not self.ambiguous

    @property
    def is_tier1(self) -> bool:
        return self.is_join and self.match_tier == 1


def _choose_assessment(candidates: list[dict]) -> dict:
    """Pick the assessment that produced the recommendation.

    Several rows can share one advert because the same advert is assessed on
    every run it stays open. The earliest observation is the recommendation
    the principal acted on, so that row wins; the total is recorded alongside.
    """
    return sorted(candidates, key=_observed_sort_key)[0]


def _match_from_candidates(
    candidates: list[dict],
    method: str,
    *,
    note: str = "",
) -> OutcomeMatch:
    """Build a join verdict, refusing to guess when the adverts differ."""
    hashes = {normalise_hash(row.get("advert_hash") or "") for row in candidates}
    hashes.discard("")
    if len(hashes) > 1:
        return OutcomeMatch(
            match_method=f"ambiguous_{method}",
            match_tier=0,
            match_confidence=0.0,
            candidate_count=len(hashes),
            ambiguous=True,
            note=(
                f"{len(hashes)} distinct adverts share this {method} evidence; "
                "no join recorded (a wrong join is worse than no join)"
                + (f" — {note}" if note else "")
            ),
        )
    chosen = _choose_assessment(candidates)
    advert_hash = normalise_hash(chosen.get("advert_hash") or "")
    if not advert_hash:
        return OutcomeMatch(
            match_method=f"invalid_{method}",
            note="matched assessment row carries no advert_hash",
            candidate_count=len(candidates),
        )
    score = chosen.get("score")
    return OutcomeMatch(
        match_method=method,
        match_tier=TIER_BY_METHOD.get(method, 0),
        match_confidence=CONFIDENCE_BY_METHOD.get(method, 0.0),
        matched_assessment_id=str(chosen.get("assessment_id") or ""),
        matched_advert_hash=advert_hash,
        matched_org_name=str(chosen.get("org_name") or ""),
        matched_title=str(chosen.get("title") or ""),
        matched_url=str(chosen.get("url") or ""),
        matched_decision=str(chosen.get("decision") or ""),
        matched_score=float(score) if isinstance(score, (int, float)) else None,
        matched_run_id=str(chosen.get("run_id") or ""),
        matched_observed_at=str(chosen.get("observed_at") or ""),
        matched_assessment_count=len(candidates),
        candidate_count=len(hashes) or 1,
        note=note,
    )


def resolve_outcome(
    outcome: dict,
    index: AssessmentIndex,
    *,
    applications_dir: Optional[Path] = None,
    use_case_identity: bool = True,
) -> OutcomeMatch:
    """Resolve one outcome row against the assessment index.

    The ladder is walked in order and the first rung with evidence decides;
    a later rung never overrides an earlier one. ``use_case_identity=False``
    disables the two rungs that read the case directory, which is how the
    before/after measurement in the step-0 notes reproduces the legacy
    behaviour.
    """
    org = str(outcome.get("org_name") or outcome.get("case_org") or "")
    title = str(outcome.get("title") or outcome.get("role") or "")
    url = str(outcome.get("url") or "")
    stored_hash = normalise_hash(outcome.get("advert_hash") or "")

    case_identity: Optional[CaseIdentity] = None
    case_dir: Optional[Path] = None
    if use_case_identity:
        case_dir = locate_case_dir(
            str(outcome.get("case_path") or outcome.get("path") or ""), applications_dir
        )
        if case_dir is not None:
            case_identity = read_case_identity(case_dir)
            if case_identity is not None:
                org = org or case_identity.org_name
                title = title or case_identity.title
                url = url or case_identity.url

    # Tier 1a — the outcome already carries the assessment's own key.
    if stored_hash:
        candidates = index.by_hash.get(stored_hash)
        if candidates:
            return _match_from_candidates(candidates, METHOD_EXACT)

    # Tier 1b — the key is recovered from the case dossier LoveWork wrote.
    if case_identity is not None and case_identity.advert_hash:
        candidates = index.by_hash.get(case_identity.advert_hash)
        if candidates:
            return _match_from_candidates(
                candidates,
                METHOD_CASE_IDENTITY,
                note=f"identity read from {Path(case_identity.source).name or 'case dossier'}",
            )

    # Tier 1c — the same advert URL, normalised.
    norm_url = normalise_url(url)
    if norm_url:
        candidates = index.by_url.get(norm_url)
        if candidates:
            return _match_from_candidates(candidates, METHOD_EXACT_URL, note=f"url={norm_url}")

    # Tier 1d — the case file links to an assessment advert of the same
    # organisation. Restricted to the same organisation because an unrelated
    # link in a notes file must never create a join.
    org_key = normalise_org_key(org)
    if use_case_identity and case_dir is not None and org_key:
        case_urls = read_case_urls(case_dir)
        candidates = [
            row
            for url_key in case_urls
            for row in index.by_url.get(url_key, ())
            if normalise_org_key(row.get("org_name") or "") == org_key
        ]
        if candidates:
            deduped = {
                str(row.get("assessment_id") or row.get("advert_hash") or ""): row
                for row in candidates
            }
            return _match_from_candidates(
                list(deduped.values()),
                METHOD_CASE_URL,
                note=f"{len(case_urls)} urls in case text, {len(deduped)} matching assessment(s)",
            )

    # Tier 2a — organisation and role title agree exactly.
    title_key = normalise_title_key(title)
    if org_key and title_key:
        candidates = index.by_org_title.get((org_key, title_key))
        if candidates:
            return _match_from_candidates(candidates, METHOD_ORG_TITLE, note=f"org={org_key}")

        # Tier 2b — the same organisation, near-identical title.
        best_ratio = 0.0
        best_rows: list[dict] = []
        for candidate_title in index.title_keys_by_org.get(org_key, ()):
            ratio = difflib.SequenceMatcher(None, title_key, candidate_title).ratio()
            if ratio > best_ratio:
                best_ratio, best_rows = ratio, index.by_org_title[(org_key, candidate_title)]
        if best_rows and best_ratio >= FUZZY_MIN_RATIO:
            return _match_from_candidates(
                best_rows,
                METHOD_ORG_TITLE_FUZZY,
                note=f"title ratio={best_ratio:.2f}",
            )

    # Tier 3 — the organisation is known, the advert is not. This is an
    # association for cooldown/reapply logic, never a score label.
    if org_key:
        candidates = index.by_org.get(org_key)
        if candidates:
            hashes = {normalise_hash(row.get("advert_hash") or "") for row in candidates}
            hashes.discard("")
            return OutcomeMatch(
                match_method=METHOD_ORG_ONLY,
                match_tier=3,
                match_confidence=0.0,
                matched_org_name=str(candidates[0].get("org_name") or org),
                matched_assessment_count=len(candidates),
                candidate_count=len(hashes),
                note="organisation matched; no advert attribution",
            )

    return OutcomeMatch(match_method=METHOD_NONE, note="no assessment evidence for this outcome")


def make_outcome_key(source: str, path: str, role: str, kind: str, date: str) -> str:
    """Stable identity for one outcome *event*.

    ``append_passive_outcomes`` re-emits the same application on every run
    that re-scans its organisation, so raw row counts inflate. The key is
    written onto new rows and recomputed for old ones, which dedupes repeats
    without rewriting the ledger.
    """
    parts = [str(source or ""), str(path or ""), str(role or ""), str(kind or ""), str(date or "")]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def outcome_identity_key(outcome: dict) -> str:
    """Stable identity for one *event*, not one ledger row.

    ``append_passive_outcomes`` re-emits the same application every run that
    re-scans its organisation, so raw row counts inflate. The key dedupes
    those repeats without rewriting the ledger.
    """
    explicit = str(outcome.get("outcome_key") or "")
    if explicit:
        return explicit
    return make_outcome_key(
        str(outcome.get("source") or ""),
        str(outcome.get("case_path") or outcome.get("path") or ""),
        str(outcome.get("role") or outcome.get("title") or ""),
        str(outcome.get("kind") or ""),
        str(outcome.get("date") or ""),
    )


def build_join_row(
    outcome: dict,
    match: OutcomeMatch,
    *,
    run_id: str = "",
    profile_name: str = "",
    observed_at: str = "",
    joined_at: str = "",
) -> dict:
    """One ledger row: an outcome, its resolution verdict, and the evidence."""
    identity = outcome_identity_key(outcome)
    join_id = hashlib.sha1(
        f"{identity}|{match.match_method}|{match.matched_advert_hash}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "event_type": "outcome_join",
        "policy_version": POLICY_VERSION,
        "join_id": join_id,
        "run_id": run_id,
        "profile_name": profile_name,
        "outcome_id": str(outcome.get("outcome_id") or ""),
        "outcome_key": identity,
        "outcome_run_id": str(outcome.get("run_id") or ""),
        "outcome_observed_at": str(outcome.get("observed_at") or ""),
        "joined_at": joined_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "outcome_kind": str(outcome.get("kind") or ""),
        "outcome_date": str(outcome.get("date") or ""),
        "outcome_source": str(outcome.get("source") or ""),
        "outcome_case_path": str(outcome.get("case_path") or outcome.get("path") or ""),
        "outcome_org_name": str(outcome.get("org_name") or ""),
        "outcome_role": str(outcome.get("role") or ""),
        "outcome_title": str(outcome.get("title") or ""),
        "outcome_url": str(outcome.get("url") or ""),
        "outcome_advert_hash": normalise_hash(outcome.get("advert_hash") or ""),
        "match_method": match.match_method,
        "match_tier": match.match_tier,
        "match_confidence": match.match_confidence,
        "match_note": match.note,
        "ambiguous": match.ambiguous,
        "candidate_adverts": match.candidate_count,
        "matched_assessment_id": match.matched_assessment_id,
        "matched_advert_hash": match.matched_advert_hash,
        "matched_org_name": match.matched_org_name,
        "matched_title": match.matched_title,
        "matched_url": match.matched_url,
        "matched_decision": match.matched_decision,
        "matched_score": match.matched_score,
        "matched_run_id": match.matched_run_id,
        "matched_observed_at": match.matched_observed_at,
        "matched_assessment_rows": match.matched_assessment_count,
    }


def summarise_joins(join_rows: Iterable[dict], *, now: Optional[datetime] = None) -> dict:
    """Turn join rows into the join-rate metric recorded for this run.

    ``*_rows`` counts ledger rows; ``*_distinct_adverts`` counts distinct
    adverts, which is the number the business plan targets (>= 100 Tier-1
    joins). Rows alone would inflate on every re-scan.
    """
    rows = list(join_rows)
    current = now or datetime.now()
    window_start = (current - timedelta(days=TIER1_WINDOW_DAYS)).date().isoformat()

    tier1_rows = tier2_rows = tier3_rows = ambiguous_rows = no_match_rows = 0
    tier1_adverts: set[str] = set()
    tier2_adverts: set[str] = set()
    tier1_hash_adverts: set[str] = set()
    tier1_url_adverts: set[str] = set()
    tier1_adverts_recent: set[str] = set()

    for row in rows:
        method = str(row.get("match_method") or METHOD_NONE)
        if row.get("ambiguous"):
            ambiguous_rows += 1
            continue
        tier = int(row.get("match_tier") or 0)
        advert = normalise_hash(row.get("matched_advert_hash") or "")
        if tier == 1 and advert:
            tier1_rows += 1
            tier1_adverts.add(advert)
            if method in {METHOD_EXACT_URL, METHOD_CASE_URL}:
                tier1_url_adverts.add(advert)
            else:
                tier1_hash_adverts.add(advert)
            if str(row.get("outcome_date") or "") >= window_start:
                tier1_adverts_recent.add(advert)
        elif tier == 2 and advert:
            tier2_rows += 1
            tier2_adverts.add(advert)
        elif tier == 3:
            tier3_rows += 1
        elif method == METHOD_NONE:
            no_match_rows += 1
        else:
            no_match_rows += 1

    distinct_outcomes = len({str(row.get("outcome_key") or "") for row in rows})
    return {
        "policy_version": POLICY_VERSION,
        "outcome_rows": len(rows),
        "distinct_outcomes": distinct_outcomes,
        "tier1_rows": tier1_rows,
        "tier1_distinct_adverts": len(tier1_adverts),
        "tier1_hash_distinct_adverts": len(tier1_hash_adverts),
        "tier1_url_distinct_adverts": len(tier1_url_adverts),
        "tier1_distinct_adverts_last_30d": len(tier1_adverts_recent),
        "tier2_rows": tier2_rows,
        "tier2_distinct_adverts": len(tier2_adverts),
        "tier3_rows": tier3_rows,
        "ambiguous_rows": ambiguous_rows,
        "no_match_rows": no_match_rows,
        "join_rate": (tier1_rows / len(rows)) if rows else 0.0,
        "target_tier1_joins": TARGET_TIER1_JOINS,
        "target_reactions_per_month": TARGET_REACTIONS_PER_MONTH,
        "target_met": len(tier1_adverts) >= TARGET_TIER1_JOINS,
    }


def render_summary(metrics: dict) -> str:
    """Human-readable one-block summary for logs and the CLI."""
    lines = [
        "Outcome->assessment join ({policy_version}): outcomes={outcome_rows} rows / "
        "{distinct_outcomes} distinct events".format(**metrics),
        "  Tier 1 (advert-level): {tier1_rows} rows, {tier1_distinct_adverts} distinct adverts "
        "({tier1_hash_distinct_adverts} by advert_hash, {tier1_url_distinct_adverts} by advert URL)".format(**metrics),
        "  Tier 2 (org+title):    {tier2_rows} rows, {tier2_distinct_adverts} distinct adverts".format(**metrics),
        "  Tier 3 (org only):     {tier3_rows} rows (association, not a join)".format(**metrics),
        "  Ambiguous:             {ambiguous_rows} rows (excluded on purpose)".format(**metrics),
        "  No match:              {no_match_rows} rows".format(**metrics),
        "  Tier-1 adverts in the last {window} days: {tier1_distinct_adverts_last_30d}".format(
            window=TIER1_WINDOW_DAYS, **metrics
        ),
        "  Target: >= {target_tier1_joins} Tier-1 joins -> {verdict}".format(
            verdict="MET" if metrics["target_met"] else "NOT MET", **metrics
        ),
    ]
    return "\n".join(lines)


def append_outcome_joins(
    dataset_dir: Path,
    join_rows: Iterable[dict],
    *,
    skip_existing: bool = True,
) -> Path:
    """Append join rows to ``outcome_joins.jsonl`` (append-only, deduped)."""
    dataset_dir = Path(dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / JOINS_FILENAME
    existing: set[tuple[str, str, str]] = set()
    if skip_existing and path.is_file():
        for row in load_jsonl(path):
            existing.add((
                str(row.get("outcome_id") or ""),
                str(row.get("match_method") or ""),
                str(row.get("matched_advert_hash") or ""),
            ))
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in join_rows:
            key = (
                str(row.get("outcome_id") or ""),
                str(row.get("match_method") or ""),
                str(row.get("matched_advert_hash") or ""),
            )
            if key in existing:
                continue
            existing.add(key)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    if written:
        logger.info("Outcome joins appended: %s (%d rows)", path, written)
    return path


def append_join_metrics(dataset_dir: Path, metrics: dict, *, run_id: str = "", profile_name: str = "") -> Path:
    """Append this run's join-rate metric row to ``join_metrics.jsonl``."""
    dataset_dir = Path(dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / METRICS_FILENAME
    row = {
        "event_type": "join_metrics",
        "run_id": run_id,
        "profile_name": profile_name,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    row.update(metrics)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    logger.info(render_summary(metrics).replace("\n", " | "))
    return path


def join_dataset(
    dataset_dir: Path,
    *,
    run_id: str = "",
    profile_name: str = "",
    applications_dir: Optional[Path] = None,
    write: bool = False,
    use_case_identity: bool = True,
    index: Optional[AssessmentIndex] = None,
) -> dict:
    """Resolve every outcome row against the assessment ledger.

    ``write=False`` (the default) is a dry run: it computes the joins and the
    metric and touches no file. The crawl sets ``write=True``.
    """
    dataset_dir = Path(dataset_dir)
    assessments_path = dataset_dir / "assessments.jsonl"
    outcomes_path = dataset_dir / "outcomes.jsonl"
    index = index if index is not None else AssessmentIndex.from_file(assessments_path)
    outcomes = load_jsonl(outcomes_path)

    joined_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    join_rows = [
        build_join_row(
            outcome,
            resolve_outcome(
                outcome,
                index,
                applications_dir=applications_dir,
                use_case_identity=use_case_identity,
            ),
            run_id=run_id,
            profile_name=profile_name,
            observed_at=str(outcome.get("observed_at") or ""),
            joined_at=joined_at,
        )
        for outcome in outcomes
    ]
    metrics = summarise_joins(join_rows)

    result = {"joins": join_rows, "metrics": metrics, "written": False}
    if write:
        append_outcome_joins(dataset_dir, join_rows)
        append_join_metrics(dataset_dir, metrics, run_id=run_id, profile_name=profile_name)
        result["written"] = True
    return result


#: Join fields a consumer should read off an outcome. Kept in one place so a
#: reader never has to know the ledger layout.
JOIN_FIELDS = (
    "matched_assessment_id",
    "matched_advert_hash",
    "match_method",
    "match_tier",
    "match_confidence",
    "matched_org_name",
    "matched_title",
    "matched_url",
    "matched_decision",
    "matched_score",
    "matched_run_id",
    "matched_observed_at",
    "matched_assessment_rows",
    "ambiguous",
    "candidate_adverts",
)


def read_joined_outcomes(dataset_dir: Path) -> list[dict]:
    """Outcome rows with their resolution inlined.

    The ledgers stay append-only, so the join is not written back onto
    ``outcomes.jsonl``. This is the read view a consumer wants: every outcome
    field, plus ``matched_assessment_id`` and friends when a join exists.
    When several join rows exist for one outcome (the ladder can improve once
    a case dossier appears), the newest wins.
    """
    dataset_dir = Path(dataset_dir)
    joins_by_outcome: dict[str, dict] = {}
    for row in load_jsonl(dataset_dir / JOINS_FILENAME):
        joins_by_outcome[str(row.get("outcome_id") or "")] = row
    joined: list[dict] = []
    for outcome in load_jsonl(dataset_dir / "outcomes.jsonl"):
        merged = dict(outcome)
        join = joins_by_outcome.get(str(outcome.get("outcome_id") or ""))
        if join is not None:
            for field in JOIN_FIELDS:
                merged[field] = join.get(field)
        joined.append(merged)
    return joined


def _main() -> int:
    import config
    from principal_runtime import resolve_principal_runtime

    parser = argparse.ArgumentParser(
        description="Resolve outcome rows against the assessment ledger (business plan step 0)"
    )
    parser.add_argument("--profile", default="lj", choices=["lj", "vj", "kj", "pk"],
                        help="principal whose ledgers to read")
    parser.add_argument("--dataset-dir", type=Path, help="override the dataset directory")
    parser.add_argument("--applications-dir", type=Path, help="override the applications directory")
    parser.add_argument("--run-id", default="", help="run id to stamp on join rows")
    parser.add_argument(
        "--write",
        action="store_true",
        help="append outcome_joins.jsonl and join_metrics.jsonl (default: dry run)",
    )
    parser.add_argument(
        "--no-case-identity",
        action="store_true",
        help="legacy behaviour: ignore the advert identity stored in case dossiers",
    )
    parser.add_argument(
        "--breakdown",
        action="store_true",
        help="also print counts per match method",
    )
    parser.add_argument("--json", action="store_true", help="print the metric row as JSON")
    args = parser.parse_args()

    dataset_dir = args.dataset_dir or (config.STATE_DIR / args.profile / "dataset")
    applications_dir = args.applications_dir or resolve_principal_runtime(args.profile).applications_dir

    result = join_dataset(
        dataset_dir,
        run_id=args.run_id,
        profile_name=args.profile,
        applications_dir=applications_dir,
        write=args.write,
        use_case_identity=not args.no_case_identity,
    )
    metrics = result["metrics"]
    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print(f"Dataset:      {dataset_dir}")
        print(f"Applications: {applications_dir}")
        print(f"Mode:         {'WRITE' if args.write else 'dry run (no files touched)'}")
        if args.no_case_identity:
            print("              case-dossier identity DISABLED (legacy behaviour)")
        print()
        print(render_summary(metrics))
        if args.breakdown:
            counts: dict[str, int] = defaultdict(int)
            for row in result["joins"]:
                counts[str(row.get("match_method") or METHOD_NONE)] += 1
            print()
            print("By match method:")
            for method, count in sorted(counts.items(), key=lambda item: -item[1]):
                print(f"  {method:28s} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""
Job registry — persistent CSV store for tracking job lifecycle.

Tracks every job ever seen by the crawler, with:
- org, title, url, careers_url
- discovery_url, discovery_date (where/when LoveWork first found the lead)
- first_seen, last_seen
- status: new, still_open, disappeared, long_lasting
- hash (sha256 of org+title+url) for dedup

This enables:
- "What new jobs appeared since last run?"
- "Is this job still open after 3 months?" (suspicious)
- "Did this job disappear?" (likely filled)
- "How long has this been open?" (track lj's "low chance" heuristic)

Uses CSV instead of SQLite so the registry is git-friendly (text diff/merge).
Writes atomically via temp file + os.replace.
"""

import csv
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Iterable

import config

logger = logging.getLogger(__name__)

CSV_PATH = config.CACHE_DIR / "jobs.csv"
DB_PATH = config.CACHE_DIR / "jobs.db"  # kept for auto-migration only

# Status constants
STATUS_NEW = "new"
STATUS_STILL_OPEN = "still_open"
STATUS_DISAPPEARED = "disappeared"
STATUS_LONG_LASTING = "long_lasting"
#: Absent from a source whose *whole scope* we enumerated this run. Stronger
#: than `disappeared` (which only means "we did not see it"), and still not
#: closure: the scope may be one of many, and a vacancy can be re-listed
#: (docs/28 §WP3b, box 2).
STATUS_NOT_LISTED_IN_SCOPE = "not_listed_in_scope"

# A job is "long-lasting" if it's been open more than this many days
LONG_LASTING_THRESHOLD_DAYS = 30

# The primary/secondary tier is **declared by the source itself** (`primary` on
# each source class), never inferred from a name here: a registry-side list is a
# second authority that a new source can be left out of silently. `tests/
# test_source_tiers.py` audits that every source declares its tier explicitly.
#
# **Primary** = the advert came from the hiring party's own publication (the
# org's careers page, the employer's own post, the employer-side feed).
# **Secondary** = someone relaying it: boards, alert mailboxes, recruiters,
# aggregators. Only a primary sighting can support "we verified it open"; a
# secondary sighting is a mention, however fresh (docs/28 §WP3b box 1).

# CSV column order
COMPLETE_SCOPES_FILE = "complete-scopes.json"

_COLUMNS = [
    "id", "org", "title", "url", "careers_url",
    "first_seen", "last_seen", "status", "source",
    "discovery_url", "discovery_date", "posted_at",
    "last_primary_seen", "hash",
]


@dataclass
class JobRecord:
    """A job in the registry."""

    id: int
    org: str
    title: str
    url: str
    careers_url: str
    first_seen: str  # ISO date
    last_seen: str   # ISO date
    status: str
    source: str = ""
    discovery_url: str = ""
    discovery_date: str = ""
    #: When the *advert* was posted, as reported by the source. Empty for every
    #: source that does not carry a posting date (docs/28 §WP3a). This is the
    #: true age signal; `first_seen`/`last_seen` only bound it from below.
    posted_at: str = ""
    #: Last time a **primary** source showed this advert open (docs/28 §WP3b).
    #: Empty means "we have only ever seen it relayed", which is a different and
    #: weaker fact than "it is closed" — never read an empty value as closure.
    last_primary_seen: str = ""

    @property
    def age_days(self) -> int:
        first = datetime.fromisoformat(self.first_seen)
        last = datetime.fromisoformat(self.last_seen)
        return (last - first).days

    @property
    def hash(self) -> str:
        return _job_hash(self.org, self.title, self.url)


def _job_hash(org: str, title: str, url: str) -> str:
    """Stable hash for dedup."""
    key = f"{org.lower().strip()}|{title.lower().strip()}|{(url or '').lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── CSV I/O ───────────────────────────────────────────────────────────────

def _read_rows(csv_path: Path) -> List[dict]:
    """Read all rows from the CSV file. Returns empty list if file is missing."""
    if not csv_path.exists():
        return []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _write_rows(csv_path: Path, rows: List[dict]):
    """Atomically write rows to CSV (temp file + os.replace)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".csv", prefix="jobs_", dir=str(csv_path.parent)
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, str(csv_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _outer_run_env() -> str:
    """The env var naming the enclosing operation (single authority:
    `writer_boundary`). Imported lazily: this module is imported very early."""
    from writer_boundary import OUTER_RUN_ENV

    return OUTER_RUN_ENV


def _row_to_record(row: dict) -> JobRecord:
    """Convert a CSV row dict to a JobRecord."""
    return JobRecord(
        id=int(row["id"]),
        org=row["org"],
        title=row["title"],
        url=row.get("url", ""),
        careers_url=row.get("careers_url", ""),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        status=row["status"],
        source=row.get("source", ""),
        discovery_url=row.get("discovery_url", ""),
        discovery_date=row.get("discovery_date", ""),
        posted_at=row.get("posted_at", ""),
        last_primary_seen=row.get("last_primary_seen", ""),
    )


def _record_to_row(rec: JobRecord) -> dict:
    """Convert a JobRecord to a CSV row dict."""
    return {
        "id": str(rec.id),
        "org": rec.org,
        "title": rec.title,
        "url": rec.url,
        "careers_url": rec.careers_url,
        "first_seen": rec.first_seen,
        "last_seen": rec.last_seen,
        "status": rec.status,
        "source": rec.source,
        "discovery_url": rec.discovery_url,
        "discovery_date": rec.discovery_date,
        "posted_at": rec.posted_at,
        "last_primary_seen": rec.last_primary_seen,
        "hash": rec.hash,
    }


# ── Auto-migration from SQLite ────────────────────────────────────────────

def _migrate_from_sqlite(sqlite_path: Path, csv_path: Path):
    """Read jobs.db → write jobs.csv. Called once when CSV doesn't exist."""
    if not sqlite_path.exists():
        return
    logger.info("Migrating %s → %s", sqlite_path, csv_path)
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute(
            "SELECT id, org, title, url, careers_url, first_seen, last_seen, "
            "status, source, hash FROM jobs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    csv_rows = []
    for r in rows:
        rid, org, title, url, cu, fs, ls, st, src, h = r
        csv_rows.append({
            "id": str(rid),
            "org": org,
            "title": title,
            "url": url or "",
            "careers_url": cu or "",
            "first_seen": fs,
            "last_seen": ls,
            "status": st,
            "source": src or "",
            "discovery_url": "",
            "discovery_date": "",
            "hash": h,
        })

    if csv_rows:
        _write_rows(csv_path, csv_rows)
        logger.info("Migrated %d rows to %s", len(csv_rows), csv_path)


def _init_csv(csv_path: Optional[Path] = None):
    """Create CSV file with header if it doesn't exist.
    Auto-migrates from jobs.db if present and jobs.csv is missing.
    """
    path = csv_path or CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        # Try auto-migration from legacy SQLite DB
        sqlite_path = path.parent / "jobs.db"
        if sqlite_path.exists():
            _migrate_from_sqlite(sqlite_path, path)

    if not path.exists():
        # Write empty header
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_COLUMNS)


# ── Legacy alias (for test compat) ────────────────────────────────────────

def _init_db(db_path: Optional[Path] = None):
    """Legacy entry point — delegates to _init_csv.
    Kept for backward compat with tests that call _init_db directly.
    """
    _init_csv(db_path)


# ── Registry ──────────────────────────────────────────────────────────────

#: Sources where absence from a run is *not* evidence about the advert.
#: A mail alert (a Gmail label) or a LinkedIn alert digest does not enumerate a
#: scope: it ages out of the query window, or the principal simply reads the
#: message. The SLB postdoctoral advert (KJ, first seen 2026-09-22) was still
#: live and interviewing while the registry had marked it `disappeared` — which
#: then made the reassessment skip it, because that gate honours `disappeared`
#: (fixed 2026-09-23).
ABSENCE_IS_NOT_EVIDENCE = (
    "gmail_lj_jobs", "gmail_vj_jobs", "gmail_kj_jobs", "linkedin_related",
)


class JobRegistry:
    """Persistent registry of all jobs ever seen, backed by CSV."""

    def __init__(self, csv_path: Optional[Path] = None, db_path: Optional[Path] = None):
        """Create registry. Accepts csv_path (preferred) or db_path (legacy).
        If neither given, uses default CSV_PATH.
        """
        # Support legacy db_path parameter for test compat
        if csv_path is not None:
            self.csv_path = Path(csv_path)
        elif db_path is not None:
            # Convert db_path → csv_path by changing extension
            self.csv_path = Path(str(db_path).replace(".db", ".csv"))
            logger.warning(
                "db_path is deprecated; use csv_path=%s instead", self.csv_path
            )
        else:
            self.csv_path = CSV_PATH
        _init_csv(self.csv_path)
        #: Scopes that declared a complete enumeration this run (see
        #: note_complete_scope). Run-scoped by construction: a fresh
        #: registry per run, so a declaration cannot leak between runs.
        self._complete_scopes: set[str] = set()

    def _load(self) -> List[dict]:
        return _read_rows(self.csv_path)

    # ── Public API ──────────────────────────────────────────────────────

    def upsert(
        self,
        org: str,
        title: str,
        url: str = "",
        careers_url: str = "",
        source: str = "",
        discovery_url: str = "",
        discovery_date: str = "",
        first_seen: str = "",
        posted_at: str = "",
        primary: bool = False,
        last_primary_seen: str = "",
    ) -> JobRecord:
        """Insert a job or refresh lifecycle and provenance for an existing one.

        `first_seen` is only used when inserting a new row; it lets a consumer
        of another principal's corpus preserve the true discovery date instead
        of stamping the ingest date (which would reset long-lasting age).

        `primary=True` records that **this run** saw the advert at the hiring
        party's own publication (the source declares its own tier); it stamps
        `last_primary_seen` with today. `last_primary_seen` instead *carries* a
        date observed elsewhere — the cross-principal consume path passes the
        owner's value rather than re-dating it, because the owner is the
        authority on what it verified. `last_primary_seen` is the only field
        that supports "we verified it open"; a fresh mention from a board or
        alert box is not that.
        """
        h = _job_hash(org, title, url)
        today = _now()
        rows = self._load()

        # Find existing by hash
        for row in rows:
            if row["hash"] == h:
                row["last_seen"] = today
                row["status"] = STATUS_STILL_OPEN
                # Old rows often predate provenance support. Enrich them when
                # the source is seen again, but retain the first known source
                # values once recorded.
                if careers_url and not row.get("careers_url"):
                    row["careers_url"] = careers_url
                if source and not row.get("source"):
                    row["source"] = source
                if discovery_url and not row.get("discovery_url"):
                    row["discovery_url"] = discovery_url
                if discovery_date and not row.get("discovery_date"):
                    row["discovery_date"] = discovery_date
                # A posting date never changes; keep the first one we learned.
                if posted_at and not row.get("posted_at"):
                    row["posted_at"] = posted_at
                # Verified openness only advances on a primary sighting, and it
                # is a *last* fact: the newest sighting always wins. A carried
                # date (the owner's, via consume) is only accepted when it is
                # actually newer, so re-ingesting cannot walk it backwards.
                if primary:
                    row["last_primary_seen"] = today
                elif last_primary_seen and last_primary_seen > row.get("last_primary_seen", ""):
                    row["last_primary_seen"] = last_primary_seen
                elif row["status"] == STATUS_DISAPPEARED:
                    # A relaying source seen again is a mention, not evidence of
                    # openness — but it does say the advert is being circulated.
                    row["status"] = STATUS_STILL_OPEN
                _write_rows(self.csv_path, rows)
                return _row_to_record(row)

        # New job
        new_id = max((int(r["id"]) for r in rows), default=0) + 1
        rec = JobRecord(
            id=new_id, org=org, title=title, url=url,
            careers_url=careers_url, first_seen=first_seen or today,
            last_seen=today, status=STATUS_NEW, source=source,
            discovery_url=discovery_url, discovery_date=discovery_date,
            posted_at=posted_at,
            last_primary_seen=(today if primary else last_primary_seen),
        )
        rows.append(_record_to_row(rec))
        _write_rows(self.csv_path, rows)
        return rec

    def note_complete_scope(self, source_name: str) -> None:
        """Record that `source_name` enumerated its whole scope this run.

        A source calls this only when it really did: a successful full-feed
        fetch, an untruncated page, a query that did not change. A capped feed,
        a failed fetch or a partial traversal must not — absence from those says
        nothing about the advert (docs/28 §WP3b box 2).
        """
        if not source_name:
            return
        self._complete_scopes.add(source_name)
        # Also write it where the *sealing* parent can see it: the crawl runs in
        # a subprocess, so an in-memory declaration would die with it and the
        # seal would record only `crawl:<mode>` — leaving box 2's qualified
        # absence and box 4's closures unable to fire in production. Stamped with
        # the outer run id so a later run cannot inherit this one's claim.
        sidecar = self.csv_path.parent / COMPLETE_SCOPES_FILE
        try:
            existing = {}
            if sidecar.is_file():
                existing = json.loads(sidecar.read_text(encoding="utf-8"))
            existing[source_name] = {
                "scope": source_name,
                "run_id": os.environ.get(_outer_run_env(), ""),
                "at": _now(),
            }
            tmp = sidecar.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, indent=1, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, sidecar)
        except Exception as e:  # never break a crawl over bookkeeping
            logger.warning("Could not record complete scope %s: %s", source_name, e)

    def mark_run_complete(
        self,
        sources_run: Optional[List[str]] = None,
        complete_scopes: Optional[Iterable[str]] = None,
    ) -> int:
        """Called at the end of a crawl run. Marks all still_open jobs NOT seen
        in this run as disappeared, and promotes any long-lasting ones.

        Args:
            sources_run: optional list of source names that were run. When
                provided, only jobs from these sources are checked for
                disappeared status — jobs from other sources are left
                untouched (their absence this run is not a signal). When
                None, behaves as before (mark all unseen still_open jobs).
            complete_scopes: source names that enumerated their **whole scope**
                this run without truncation, caps or failures. Absence from such
                a scope records `not_listed_in_scope` — a stronger statement than
                `disappeared`, and still not closure: the scope may be one of
                many, and an advert can be re-listed (docs/28 §WP3b, box 2). A
                capped feed, a failed fetch or a changed query must NOT be listed
                here, because then absence says nothing about the advert.

        Returns count of jobs marked absent in this call (both kinds).
        """
        today = _now()
        rows = self._load()
        # Declared by the sources themselves during this run, plus
        # anything a caller knows (a replay, a test).
        complete = set(complete_scopes or ()) | self._complete_scopes
        disappeared = 0

        for row in rows:
            if row["status"] not in (STATUS_STILL_OPEN, STATUS_NEW):
                continue
            if sources_run and row.get("source", "") not in sources_run:
                continue

            if row.get("source", "") in ABSENCE_IS_NOT_EVIDENCE:
                # Alert sources: keep the row live. Its age (from the posted
                # date, or first_seen as a floor) is what retires it, via the
                # staleness rule — not our own reading of a mail window.
                continue

            if row["last_seen"] < today:
                # Absence is only qualified when we know the scope was whole.
                row["status"] = (STATUS_NOT_LISTED_IN_SCOPE
                                 if row.get("source", "") in complete
                                 else STATUS_DISAPPEARED)
                disappeared += 1
            else:
                first_dt = datetime.fromisoformat(row["first_seen"])
                last_dt = datetime.fromisoformat(row["last_seen"])
                age = (last_dt - first_dt).days
                if age > LONG_LASTING_THRESHOLD_DAYS and row["status"] == STATUS_STILL_OPEN:
                    row["status"] = STATUS_LONG_LASTING

        _write_rows(self.csv_path, rows)
        return disappeared

    def retire_hashes(self, hashes) -> int:
        """Mark specific rows as disappeared; return how many changed.

        Used by the cross-principal consumer: it retires exactly the adverts
        that vanished from the owner's live corpus since the previous ingest, so
        adverts the consumer discovered independently are never touched. Only
        rows currently `new`/`still_open` change; a later upsert revives them if
        the owner lists them again.
        """
        wanted = {h for h in hashes if h}
        if not wanted:
            return 0
        rows = self._load()
        changed = 0
        for row in rows:
            if row["hash"] in wanted and row["status"] in (STATUS_NEW, STATUS_STILL_OPEN):
                row["status"] = STATUS_DISAPPEARED
                changed += 1
        if changed:
            _write_rows(self.csv_path, rows)
        return changed

    def get(self, org: str, title: str, url: str = "") -> Optional[JobRecord]:
        """Look up a job by hash."""
        h = _job_hash(org, title, url)
        for row in self._load():
            if row["hash"] == h:
                return _row_to_record(row)
        return None

    def by_org(self, org: str) -> List[JobRecord]:
        """All jobs ever seen for an org (case-insensitive), newest first."""
        org_lower = org.lower()
        rows = sorted(
            self._load(),
            key=lambda r: r["last_seen"],
            reverse=True,
        )
        return [
            _row_to_record(row)
            for row in rows
            if row["org"].lower() == org_lower
        ]

    def all_jobs(self, status: Optional[str] = None) -> List[JobRecord]:
        """All jobs, optionally filtered by status. Newest first."""
        rows = sorted(
            self._load(),
            key=lambda r: r["last_seen"],
            reverse=True,
        )
        if status:
            rows = [r for r in rows if r["status"] == status]
        return [_row_to_record(r) for r in rows]

    def stats(self) -> dict:
        """Return counts by status."""
        result = {}
        for row in self._load():
            st = row["status"]
            result[st] = result.get(st, 0) + 1
        return result

    def get_status_for(self, org: str, title: str, url: str = "") -> str:
        """Get the status of a job (for the matcher). Returns 'unknown' if not in DB."""
        record = self.get(org, title, url)
        if record is None:
            return "unknown"
        return record.status

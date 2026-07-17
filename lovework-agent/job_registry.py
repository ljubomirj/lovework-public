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
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

CSV_PATH = config.CACHE_DIR / "jobs.csv"
DB_PATH = config.CACHE_DIR / "jobs.db"  # kept for auto-migration only

# Status constants
STATUS_NEW = "new"
STATUS_STILL_OPEN = "still_open"
STATUS_DISAPPEARED = "disappeared"
STATUS_LONG_LASTING = "long_lasting"

# A job is "long-lasting" if it's been open more than this many days
LONG_LASTING_THRESHOLD_DAYS = 30

# CSV column order
_COLUMNS = [
    "id", "org", "title", "url", "careers_url",
    "first_seen", "last_seen", "status", "source",
    "discovery_url", "discovery_date", "hash",
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
    ) -> JobRecord:
        """Insert a job or refresh lifecycle and provenance for an existing one."""
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
                _write_rows(self.csv_path, rows)
                return _row_to_record(row)

        # New job
        new_id = max((int(r["id"]) for r in rows), default=0) + 1
        rec = JobRecord(
            id=new_id, org=org, title=title, url=url,
            careers_url=careers_url, first_seen=today,
            last_seen=today, status=STATUS_NEW, source=source,
            discovery_url=discovery_url, discovery_date=discovery_date,
        )
        rows.append(_record_to_row(rec))
        _write_rows(self.csv_path, rows)
        return rec

    def mark_run_complete(
        self,
        sources_run: Optional[List[str]] = None,
    ) -> int:
        """Called at the end of a crawl run. Marks all still_open jobs NOT seen
        in this run as disappeared, and promotes any long-lasting ones.

        Args:
            sources_run: optional list of source names that were run. When
                provided, only jobs from these sources are checked for
                disappeared status — jobs from other sources are left
                untouched (their absence this run is not a signal). When
                None, behaves as before (mark all unseen still_open jobs).

        Returns count of jobs marked as disappeared in this call.
        """
        today = _now()
        rows = self._load()
        disappeared = 0

        for row in rows:
            if row["status"] not in (STATUS_STILL_OPEN, STATUS_NEW):
                continue
            if sources_run and row.get("source", "") not in sources_run:
                continue

            if row["last_seen"] < today:
                row["status"] = STATUS_DISAPPEARED
                disappeared += 1
            else:
                first_dt = datetime.fromisoformat(row["first_seen"])
                last_dt = datetime.fromisoformat(row["last_seen"])
                age = (last_dt - first_dt).days
                if age > LONG_LASTING_THRESHOLD_DAYS and row["status"] == STATUS_STILL_OPEN:
                    row["status"] = STATUS_LONG_LASTING

        _write_rows(self.csv_path, rows)
        return disappeared

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

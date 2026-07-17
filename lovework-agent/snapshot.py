"""
Cache snapshotting — archive jobs.csv and jobs.db before each crawl run.

Every crawl rewrites the registry. This module saves a timestamped copy
so we build a historical dataset. Use from pipeline.py and
incremental_crawl.py before the crawl starts:

    from snapshot import snapshot_cache
    snapshot_cache(CACHE_DIR)
"""

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, List
from uuid import uuid4

logger = logging.getLogger(__name__)

POLICY_VERSION = "matcher-multiaxis-v1"


def snapshot_cache(cache_dir: Path) -> List[Path]:
    """Copy jobs.csv and jobs.db to a dated archive before the crawl runs.

    Creates: cache/archive/jobs-2026-07-06T143022.csv
    Returns list of snapshot paths created (empty if nothing to snapshot).
    """
    archive_dir = cache_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    snapshots = []
    for name in ("jobs.csv", "jobs.db"):
        src = cache_dir / name
        if src.exists():
            stem = name.rsplit(".", 1)[0]
            ext = name.rsplit(".", 1)[1]
            dst = archive_dir / f"{stem}-{ts}.{ext}"
            shutil.copy2(src, dst)
            snapshots.append(dst)
            logger.info(f"Snapshot: {src} → {dst}")
    return snapshots


def list_snapshots(cache_dir: Path, limit: int = 10) -> List[Path]:
    """Return the N most recent snapshot files, newest first."""
    archive_dir = cache_dir / "archive"
    if not archive_dir.is_dir():
        return []
    files = sorted(archive_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def snapshot_count(cache_dir: Path) -> int:
    """Count total snapshots on disk."""
    archive_dir = cache_dir / "archive"
    if not archive_dir.is_dir():
        return 0
    return len(list(archive_dir.iterdir()))


def new_run_id() -> str:
    """Return a unique run identifier for joining dataset rows."""
    return uuid4().hex


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _git_commit() -> str:
    """Best-effort current git commit. Empty when git is unavailable."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _advert_hash(org_name: str, title: str, url: str = "") -> str:
    """Stable advert identity, matching job_registry.JobRecord.hash."""
    try:
        from job_registry import _job_hash

        return _job_hash(org_name, title, url or "")
    except Exception:
        key = f"{org_name.lower().strip()}|{title.lower().strip()}|{(url or '').lower().strip()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


def append_run(
    dataset_dir: Path,
    *,
    run_id: str,
    profile_name: str,
    role: str,
    sources: Iterable[str],
    profile_text: str,
    model: str = "",
    provider: str = "",
    policy_version: str = POLICY_VERSION,
) -> Path:
    """Append one pipeline-run header to the dataset ledger."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "runs.jsonl"
    row = {
        "event_type": "run",
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "profile_name": profile_name,
        "role": role,
        "sources": list(sources),
        "git_commit": _git_commit(),
        "profile_hash": _sha256_text(profile_text),
        "policy_version": policy_version,
        "model": model,
        "provider": provider,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    logger.info(f"Run ledger appended: {path}")
    return path


def append_assessments(
    dataset_dir: Path,
    entries: Iterable,
    *,
    run_id: str = "",
    profile_name: str,
    role: str,
    sources: Iterable[str],
    policy_version: str = POLICY_VERSION,
) -> Path:
    """Append this run's scored findings to a JSONL assessment ledger."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "assessments.jsonl"
    observed_at = datetime.now().isoformat(timespec="seconds")
    source_list = list(sources)
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            org_name = getattr(entry, "org_name", "")
            title = getattr(entry, "title", "")
            url = getattr(entry, "url", "") or ""
            row = {
                "event_type": "assessment",
                "assessment_id": uuid4().hex,
                "run_id": run_id,
                "advert_hash": _advert_hash(org_name, title, url),
                "observed_at": observed_at,
                "profile_name": profile_name,
                "role": role,
                "sources_run": source_list,
                "source": getattr(entry, "source", ""),
                "discovery_url": getattr(entry, "discovery_url", ""),
                "discovery_date": getattr(entry, "discovery_date", ""),
                "primary_content_hash": getattr(entry, "primary_content_hash", ""),
                "primary_fetched_at": getattr(entry, "primary_fetched_at", ""),
                "primary_fetch_method": getattr(entry, "primary_fetch_method", ""),
                "alignment_matrix": getattr(entry, "alignment_matrix", []),
                "gaps": getattr(entry, "gaps", []),
                "application_angle": getattr(entry, "application_angle", ""),
                "assessment_status": getattr(entry, "assessment_status", "SCORED"),
                "org_name": org_name,
                "title": title,
                "url": url,
                "location": getattr(entry, "location", ""),
                "score": getattr(entry, "score", None),
                "decision": getattr(entry, "decision", ""),
                "fit_score": getattr(entry, "fit_score", None),
                "reach_score": getattr(entry, "reach_score", None),
                "flourish_score": getattr(entry, "flourish_score", None),
                "combined_score": getattr(entry, "combined_score", None),
                "recommended_action": getattr(entry, "recommended_action", None),
                "reasoning": getattr(entry, "reasoning", ""),
                "lifecycle_status": getattr(entry, "lifecycle_status", None),
                "first_seen": getattr(entry, "first_seen", None),
                "policy_version": policy_version,
            }
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    logger.info(f"Assessment log appended: {path}")
    return path


def append_passive_outcomes(
    dataset_dir: Path,
    entries: Iterable,
    *,
    run_id: str = "",
    use_gmail: bool = True,
) -> Path:
    """Append passively discovered actions/outcomes from applications/ and Gmail.

    This intentionally does not require a user action. It reuses the existing
    history scanner and records org-level outcome evidence when exact advert
    attribution is not available.
    """
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "outcomes.jsonl"
    observed_at = datetime.now().isoformat(timespec="seconds")
    orgs = []
    seen = set()
    for entry in entries:
        org = (getattr(entry, "org_name", "") or "").strip()
        if not org:
            continue
        key = org.lower()
        if key in seen:
            continue
        seen.add(key)
        orgs.append(org)

    with path.open("a", encoding="utf-8") as f:
        for org in orgs:
            try:
                from history import scan_history

                prior = scan_history(org, use_gmail=use_gmail)
            except Exception as e:
                logger.debug(f"Passive outcome scan failed for {org}: {e}")
                continue

            for app in prior.applications:
                app_hash = _advert_hash(app.company or org, app.role or "", "")
                applied = {
                    "event_type": "outcome",
                    "outcome_id": uuid4().hex,
                    "run_id": run_id,
                    "observed_at": observed_at,
                    "org_name": org,
                    "advert_hash": app_hash,
                    "kind": "applied",
                    "date": app.date,
                    "source": "applications",
                    "role": app.role,
                    "path": str(app.path),
                    "notes": "",
                }
                f.write(json.dumps(applied, ensure_ascii=False, sort_keys=True) + "\n")
                if app.has_rejection:
                    rejection = dict(applied)
                    rejection["outcome_id"] = uuid4().hex
                    rejection["kind"] = "rejection"
                    rejection["date"] = app.rejection_date or app.date
                    rejection["notes"] = "rejection marker found in application case"
                    f.write(json.dumps(rejection, ensure_ascii=False, sort_keys=True) + "\n")

            for event in prior.gmail_events:
                if event.kind == "other":
                    continue
                row = {
                    "event_type": "outcome",
                    "outcome_id": uuid4().hex,
                    "run_id": run_id,
                    "observed_at": observed_at,
                    "org_name": org,
                    "advert_hash": "",
                    "kind": event.kind,
                    "date": event.date,
                    "source": "gmail",
                    "role": "",
                    "subject": event.subject,
                    "from": event.from_,
                    "snippet": event.snippet,
                }
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    logger.info(f"Passive outcome log appended: {path}")
    return path

"""
Cache snapshotting — archive jobs.csv and jobs.db before each crawl run.

Every crawl rewrites the registry. This module saves a timestamped copy
so we build a historical dataset. Use from pipeline.py and
incremental_crawl.py before the crawl starts:

    from snapshot import snapshot_cache
    snapshot_cache(CACHE_DIR)
"""

import hashlib
import jsonl_io
import json
import logging
import os
import re
import shutil
from datetime import date, datetime, timezone
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
    mode: str = "live",
    policy_version: str = POLICY_VERSION,
) -> Path:
    """Append one pipeline-run header to the dataset ledger.

    `mode` is "live" for a crawl, "replay" for a re-ingest of the local email
    archive (`--replay-archive`), and "consume" for ingesting another
    principal's crawled corpus (see `consume.py`).
    """
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "runs.jsonl"
    row = {
        "event_type": "run",
        "run_id": run_id,
        # Aware UTC: a naive stamp cannot be ordered against a cutoff
        # without assuming a zone, which is a silent lie (WP3b box 3).
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile_name": profile_name,
        "role": role,
        "sources": list(sources),
        "git_commit": _git_commit(),
        "profile_hash": _sha256_text(profile_text),
        "policy_version": policy_version,
        "model": model,
        "provider": provider,
        "mode": mode,
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
    written: list = []
    # Aware UTC, same reason as started_at above.
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                # The advert's own posting date (WP3a) belongs in the record:
                # without it `asof explain` can never say how old the advert was
                # when the decision was made, and every row would look like the
                # pre-WP3a ones (null).
                "posted_at": getattr(entry, "posted_at", None),
                "policy_version": policy_version,
            }
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            written.append(row)
    logger.info(f"Assessment log appended: {path}")
    # Feed the company-event cube (docs/37): one raw {date, principal, company,
    # event} row per decision, so tallies can decide which companies earn a
    # permanent place on a crawl list. Best-effort by contract — bookkeeping must
    # never break a crawl; the ledger above is already durable.
    try:
        import company_events

        company_events.append_events(written, profile_name, dataset_dir=dataset_dir)
    except Exception as exc:
        logger.warning(f"Company events not recorded: {exc}")
    return path


def _outcome_key(row: dict) -> str:
    """Stable per-event key, so re-scans do not inflate the ledger counts."""
    try:
        from outcome_link import make_outcome_key

        return make_outcome_key(
            row.get("source", ""),
            row.get("case_path") or row.get("path", ""),
            row.get("role") or row.get("title", ""),
            row.get("kind", ""),
            row.get("date", ""),
        )
    except Exception:  # pragma: no cover - outcome_link is always importable
        parts = "|".join(
            str(row.get(field, ""))
            for field in ("source", "path", "role", "kind", "date")
        )
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]


def _derive_outcome(
    base: dict,
    *,
    kind: str,
    date: str = "",
    notes: str = "",
    run_id: str = "",
    observed_at: str = "",
) -> dict:
    """Copy an outcome row into a different outcome kind with a fresh id."""
    row = dict(base)
    row["event_type"] = "outcome"
    row["outcome_id"] = uuid4().hex
    row["kind"] = kind
    row["date"] = date or row.get("date", "")
    if notes:
        row["notes"] = notes
    if run_id:
        row["run_id"] = run_id
    if observed_at:
        row["observed_at"] = observed_at
    row["outcome_key"] = _outcome_key(row)
    return row


def _application_outcome(app, *, org: str, run_id: str, observed_at: str) -> dict:
    """One application -> one outcome row that carries its advert identity.

    The hash is taken from the case dossier when the case has one
    (``history.scan_applications`` reads ``Advert identity:``), so the row
    joins the assessment that produced the recommendation at Tier 1
    (``outcome_link.resolve_outcome``). Without a dossier the hash is derived
    from the case directory, exactly as before this change, and the resolver
    falls back to a weaker, clearly labelled rung.
    """
    case_org = getattr(app, "company", "") or org
    advert_url = (getattr(app, "url", "") or "").strip()
    advert_hash = (getattr(app, "advert_hash", "") or "").strip()
    hash_source = "case_dossier" if advert_hash else "derived"
    if not advert_hash:
        advert_hash = _advert_hash(case_org, getattr(app, "role", "") or "", advert_url)
    return {
        "event_type": "outcome",
        "outcome_id": uuid4().hex,
        "outcome_key": _outcome_key({
            "source": "applications",
            "case_path": str(app.path),
            "role": getattr(app, "role", "") or "",
            "kind": "applied",
            "date": app.date,
        }),
        "run_id": run_id,
        "observed_at": observed_at,
        "org_name": org,
        "case_org": case_org,
        "advert_hash": advert_hash,
        "advert_hash_source": hash_source,
        "url": advert_url,
        "title": getattr(app, "title", "") or getattr(app, "role", "") or "",
        "decision": getattr(app, "decision", "") or "",
        "initial_score": getattr(app, "initial_score", None),
        "kind": "applied",
        "date": app.date,
        "source": "applications",
        "role": getattr(app, "role", "") or "",
        "path": str(app.path),
        "case_path": str(app.path),
        "notes": "",
    }


def append_decay_expirations(
    dataset_dir: Path,
    *,
    quiet_days: int = 30,
    half_life_days: int = 30,
    today: date | None = None,
) -> Path:
    """Close stale open applications by the decay policy (LJ, 2026-10-03).

    An application whose latest outcome is still ``applied``/
    ``application_sent`` after ``quiet_days`` (default 30) is treated as a
    NO by another channel that was never recorded: append a
    ``kind: decay_expiration`` outcome with a survival probability that
    halves every ``half_life_days`` since the application date. The
    original ``applied`` row is never edited — the expiration is a new
    evidence row with its own provenance, and a later real outcome
    (rejection, interview, offer) simply supersedes it downstream.

    Idempotent by (outcome_id = sha1 of org|role|date|decay_expiration),
    so re-running on any ledger state is free.
    """
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "outcomes.jsonl"
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = today or datetime.now(timezone.utc).date()

    latest: dict[tuple, dict] = {}
    existing_decay: set[str] = set()
    for line in (jsonl_io.lines(path)
                 if path.is_file() else []):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") == "decay_expiration" and row.get("org_name"):
            existing_decay.add(
                f"{row.get('org_name','').casefold()}|{row.get('role','')}")
            continue
        key = (row.get("org_name", ""), row.get("role", ""))
        when = str(row.get("date", ""))
        if key not in latest or when > latest[key].get("date", ""):
            latest[key] = row

    new_rows = []
    mem_pairs = []  # (org, narrative line) — the D22 mirror, note-if-exists
    for (org, role), row in sorted(latest.items()):
        if not org:
            continue
        if row.get("kind") not in ("applied", "application_sent"):
            continue                                   # already terminal
        try:
            applied = date.fromisoformat(str(row.get("date", ""))[:10])
        except ValueError:
            continue
        age = (today - applied).days
        if age < quiet_days:
            continue
        decay_key = f"{org.casefold()}|{role}"
        if decay_key in existing_decay:
            continue
        half_lives = max(0, (age - quiet_days) / half_life_days)
        p_success = 0.5 ** half_lives
        new_rows.append({
            "event_type": "outcome",
            "kind": "decay_expiration",
            "outcome_id": hashlib.sha1(
                f"lj|{org}|{role}|{applied.isoformat()}|decay".encode()
            ).hexdigest(),
            "org_name": org,
            "role": role,
            "date": today.isoformat(),
            "source": "decay_policy",
            "notes": (f"treated as NO by decay: no recorded outcome in "
                      f"{age}d; P(success) ≈ {p_success:.3f} "
                      f"(half-life {half_life_days}d from day {quiet_days})"),
            "observed_at": observed_at,
            "supersedes_kind": "applied",
        })
        mem_pairs.append((org, "case: decay-closed %s after %dd, P≈%.3f"
                          % (role, age, p_success)))

    if new_rows:
        with path.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            import mem as _mem
            _mem.note_case_lines(dataset_dir, mem_pairs)
        except Exception:
            pass  # ledger rows are the authority; the mirror is best-effort
    return path


def append_passive_outcomes(
    dataset_dir: Path,
    entries: Iterable,
    *,
    run_id: str = "",
    use_gmail: bool = True,
    history_kwargs: dict | None = None,
) -> Path:
    """Append passively discovered actions/outcomes from applications/ and Gmail.

    This intentionally does not require a user action. It reuses the existing
    history scanner and records outcome evidence per application case, with
    the advert identity the case carries. ``history_kwargs`` keeps a
    principal's application and Gmail scope aligned with its discovery run.

    Identity limits, stated rather than papered over: Gmail events have no
    advert attribution (no role, no hash) and resolve at best to their
    organisation; applications to organisations that this run did not surface
    are not scanned at all, because the scan is scoped to the crawled orgs.
    """
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / "outcomes.jsonl"
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
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

    mem_pairs: list[tuple[str, str]] = []  # D22 mirror, note-if-exists
    with path.open("a", encoding="utf-8") as f:
        for org in orgs:
            try:
                from history import scan_history

                prior = scan_history(org, use_gmail=use_gmail, **(history_kwargs or {}))
            except Exception as e:
                logger.debug(f"Passive outcome scan failed for {org}: {e}")
                continue

            for app in prior.applications:
                applied = _application_outcome(app, org=org, run_id=run_id, observed_at=observed_at)
                f.write(json.dumps(applied, ensure_ascii=False, sort_keys=True) + "\n")
                mem_pairs.append((org, "case: applied %s (%s)" % (
                    getattr(app, "role", "") or "role", app.date)))
                if app.has_rejection:
                    rejection = _derive_outcome(
                        applied,
                        kind="rejection",
                        date=app.rejection_date or app.date,
                        notes="rejection marker found in application case",
                    )
                    f.write(json.dumps(rejection, ensure_ascii=False, sort_keys=True) + "\n")
                    mem_pairs.append((org, "case: rejection %s (%s)" % (
                        getattr(app, "role", "") or "role",
                        app.rejection_date or app.date)))

                # Post-application events recorded on purpose in the case file
                # ("LoveWork outcome: interview 2026-09-17"). Without this the
                # ledger only ever learns applied/rejection, because Gmail
                # subject patterns do not reliably show an interview or an
                # offer. No marker, no row: nothing is inferred here.
                for kind, marker_date in getattr(app, "outcome_markers", []) or []:
                    if kind in {"applied"}:
                        continue
                    row = _derive_outcome(
                        applied,
                        kind=kind,
                        date=marker_date or app.date,
                        notes="explicit LoveWork outcome marker in the case file",
                    )
                    f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    mem_pairs.append((org, "case: %s %s (%s)" % (
                        kind, getattr(app, "role", "") or "role",
                        marker_date or app.date)))

            for event in prior.gmail_events:
                # `job_alert` is a board's digest, not an outcome: it is skipped
                # for the same reason `other` is (docs/52 §1.2 — 50 rows on lj's
                # ledger were alert emails counted as interviews and offers).
                if event.kind in ("other", "job_alert"):
                    continue
                row = _derive_outcome(
                    {
                        "org_name": org,
                        "role": "",
                        "path": "",
                        "source": "gmail",
                        "advert_hash": "",
                    },
                    kind=event.kind,
                    date=event.date,
                    run_id=run_id,
                    observed_at=observed_at,
                )
                row.update({
                    "subject": event.subject,
                    "from": event.from_,
                    "snippet": event.snippet,
                })
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                mem_pairs.append((org, "case: %s via gmail — %s (%s)" % (
                    event.kind, (event.subject or "")[:60], event.date)))

    logger.info(f"Passive outcome log appended: {path}")
    try:
        import mem as _mem
        _mem.note_case_lines(dataset_dir, mem_pairs)
    except Exception:
        pass  # ledger rows are the authority; the mirror is best-effort
    return path


# ── crawl-independent case ingest (docs/41 P1b) ──────────────────────────


def _case_dir_parts(name: str) -> tuple[str, str, str]:
    """`YYYYMMDD-Org-Role` → (date, org, role); the convention history.py parses."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(.+)", name or "")
    if not m:
        return "", "", ""
    yyyy, mm, dd, rest = m.groups()
    org, _, role = rest.partition("-")
    return f"{yyyy}-{mm}-{dd}", org, role.replace("_", " ")


def append_case_outcomes(
    dataset_dir: Path,
    principal: str,
    *,
    run_id: str = "",
    state_root: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Append outcome rows for a principal's case dossiers — no crawl needed.

    ``append_passive_outcomes`` is scoped to the orgs *that run crawled* (its own
    docstring: "applications to organisations that this run did not surface are
    not scanned at all"). A programme like a16z Speedrun, a recruiter, or any
    off-cycle application is never a registry advert, so no crawl will ever write
    its outcome — which is how the a16z SR008 submission stayed invisible on
    2026-10-06 (docs/41). This walks the principal's own case directories
    instead, so the ledger hears about a submission the day it happens.

    The authority is the deliberate ``LoveWork outcome: <kind> <date>`` marker
    (``outcome_link.CASE_OUTCOME_RE``) — never the mere existence of a directory:
    a pack that was prepared and never sent is not an application, and a captured
    page is not a fact. Rows are keyed by ``outcome_key``, so a re-run is a no-op:
    this is written to be safe to run daily.
    """
    if state_root is None:
        import config
        state_root = config.STATE_DIR
    from outcome_link import (load_jsonl, read_case_identity,
                              read_case_outcome_markers)

    cases_dir = Path(state_root) / principal / "applications"
    path = dataset_dir / "outcomes.jsonl"
    report = {"cases_scanned": 0, "markers_seen": 0, "appended": 0,
              "already_recorded": 0, "cases_absent": 0, "rows": []}
    if not cases_dir.is_dir():
        report["cases_absent"] = 1
        return report

    known = {row.get("outcome_key") for row in load_jsonl(path)}
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    mem_pairs: list[tuple[str, str]] = []

    for case in sorted(p for p in cases_dir.iterdir() if p.is_dir()):
        report["cases_scanned"] += 1
        dir_date, dir_org, dir_role = _case_dir_parts(case.name)
        identity = read_case_identity(case)
        org = (getattr(identity, "org_name", "") or dir_org).strip()
        # The same authority the memory layer uses, so the ledger row and the org
        # memory agree on the entity: a programme directory (a16z_Speedrun_SR008)
        # is one company with its programme named in the text, not a second firm.
        if org:
            try:
                from company_events import canonical_company
                org = canonical_company(org) or org
            except Exception:
                pass
        role = (getattr(identity, "title", "") or dir_role).strip()
        url = (getattr(identity, "url", "") or "").strip()
        advert_hash = (getattr(identity, "advert_hash", "") or "").strip()
        # A programme (a16z Speedrun) or a recruiter is never a registry advert,
        # so its pack carries no `Advert identity:` line. Rather than record a
        # blank, derive one exactly as _application_outcome does for legacy
        # cases — and label the source, so a reader can tell the two apart.
        hash_source = "case_dossier" if advert_hash else "derived"
        if not advert_hash:
            advert_hash = _advert_hash(org, role, url)

        for kind, marker_date in read_case_outcome_markers(case):
            report["markers_seen"] += 1
            when = marker_date or dir_date
            key = _outcome_key({
                "source": "applications", "case_path": str(case),
                "role": role, "kind": kind, "date": when,
            })
            if key in known:
                report["already_recorded"] += 1
                continue
            known.add(key)
            rows.append({
                "event_type": "outcome",
                "outcome_id": uuid4().hex,
                "outcome_key": key,
                "run_id": run_id,
                "observed_at": observed_at,
                "org_name": org,
                "case_org": org,
                "advert_hash": advert_hash,
                "advert_hash_source": hash_source,
                "url": url,
                "title": role,
                "kind": kind,
                "date": when,
                "source": "applications",
                "case_path": str(case),
                "notes": "read from the case dossier's LoveWork outcome marker",
            })
            mem_pairs.append((org, "case: %s %s (%s)" % (kind, role or "role", when)))

    report["appended"] = len(rows)
    report["rows"] = [{"org": r["org_name"], "kind": r["kind"], "date": r["date"]}
                      for r in rows]
    if dry_run or not rows:
        return report

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    logger.info("Case outcomes appended: %d row(s) → %s", len(rows), path)
    try:
        import mem as _mem
        # A deliberate marker means this really happened to this organisation, so
        # the org gets a memory if it has none — note-if-exists is the right rule
        # for bulk passes, not for one real submission. `memo init` is idempotent,
        # and this only runs when a *new* row was written.
        for org in sorted({o for o, _ in mem_pairs if o}):
            _mem.run_memo(_mem.memory_dir(principal, org=org), ["init"], quiet=True)
        _mem.note_case_lines(dataset_dir, mem_pairs)   # D22 mirror, note-if-exists
    except Exception:
        pass  # the ledger row is the authority; the memory mirror is best-effort
    return report


def main(argv: list[str] | None = None) -> int:
    """`snapshot.py --case-outcomes --profile lj [--dry-run]` (docs/41 P1b)."""
    import argparse

    ap = argparse.ArgumentParser(description="LoveWork ledger helpers")
    ap.add_argument("--case-outcomes", action="store_true",
                    help="append outcome rows from the case dossiers (no crawl needed)")
    ap.add_argument("--profile", default="lj")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.case_outcomes:
        ap.error("nothing to do — pass --case-outcomes")

    import config
    report = append_case_outcomes(config.STATE_DIR / args.profile / "dataset",
                                  args.profile, run_id=args.run_id,
                                  dry_run=args.dry_run)
    print("case ingest (%s)%s: %d dossier(s), %d marker(s) → %d new row(s)"
          % (args.profile, " DRY-RUN" if args.dry_run else "",
             report["cases_scanned"], report["markers_seen"], report["appended"]))
    for row in report["rows"]:
        print("  + %s %s (%s)" % (row["kind"], row["org"] or "?", row["date"]))
    if report["already_recorded"]:
        print("  = %d already recorded (idempotent)" % report["already_recorded"])
    if report["cases_absent"]:
        print("  (no case directory for this principal)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

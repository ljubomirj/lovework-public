#!/usr/bin/env python3
"""Ingest another principal's crawled corpus into this principal's state.

Cross-principal source ownership: a principal declares in
``profiles/<principal>/sources.yaml`` that some sources are crawled by another
principal (the *owner*)::

    consumes:
      - source: neolabs
        owner: lj

Those sources are excluded from this principal's own crawl
(``pipeline.sources_for_profile``) and ingested here from the owner's registry
plus its cached advert evidence. Fetching happens once, on the owner's
schedule; scoring stays per principal.

Boundaries:

* The owner's state is **read-only** — nothing in its registry, cache, wiki or
  ledger is written.
* Scoring, the registry, the wiki and the dataset ledger all stay with this
  principal.
* The owner keeps **lifecycle ownership**: adverts it still tracks are
  refreshed here, and adverts the owner has marked ``disappeared`` are mirrored
  as disappeared in this principal's registry.

Usage::

    ../venv/bin/python3 consume.py --profile kj --role general
    ../venv/bin/python3 consume.py --profile kj --role general --dry-run
    ../venv/bin/python3 consume.py --profile kj --role general --source neolabs --owner lj
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from job_registry import (
    STATUS_DISAPPEARED,
    STATUS_NOT_LISTED_IN_SCOPE,
    JobRecord,
    JobRegistry,
    _job_hash,
)

#: A qualified absence and an unqualified one are both absences for the
#: consumer's purposes: neither is a record to re-ingest.
_ABSENT_STATUSES = (STATUS_DISAPPEARED, STATUS_NOT_LISTED_IN_SCOPE)
from llm_client import LLMClient
from principal_runtime import PrincipalRuntime, resolve_principal_runtime
from contextlib import nullcontext
from writer_boundary import WriterBusy, standalone_writer_guard
from pipeline import consumed_sources
from reassess import reassess_records
from snapshot import append_assessments, append_run, new_run_id
from wiki_store import WikiEntry, WikiStore

logger = logging.getLogger("lovework-agent")

MARKER_FILENAME = "last_consume.json"


def _group_by_owner(
    profile_name: str,
    owner_override: Optional[str],
    source_override: Optional[str],
) -> Dict[str, List[str]]:
    """Return {owner: [sources]} for this principal's declared delegation."""
    if source_override:
        if not owner_override:
            raise SystemExit("--source requires --owner")
        if owner_override.lower() == profile_name.lower():
            raise SystemExit("--owner must be a different principal than --profile")
        sources = [s.strip() for s in source_override.split(",") if s.strip()]
        return {owner_override.lower(): sources}

    grouped: Dict[str, List[str]] = {}
    for source, owner in consumed_sources(profile_name).items():
        grouped.setdefault(owner, []).append(source)
    return {owner: sorted(sources) for owner, sources in grouped.items()}


@dataclass(frozen=True)
class OwnerCorpus:
    """The owner's corpus as *sealed* by one of its runs.

    Every path here lives inside a sealed bundle
    (`state/<owner>/strata/<run_id>/state/cache/…`), never in the owner's live
    tree: an ingest that reads live state can see bytes the owner's run never
    scored, and can race the owner's next crawl (docs/28 §WP1b box ⑤).
    """

    owner: str
    run_id: str
    bundle: Path
    registry_csv: Path
    enrichment_dir: Path
    manifest_digest: str


def resolve_owner_corpus(owner: str, *, state_root: Optional[Path] = None) -> Optional[OwnerCorpus]:
    """The owner's newest eligible sealed corpus, or None.

    None means "no eligible owner seal" — the caller must treat that as a
    blocked/deferred consume. It is deliberately **not** a licence to read the
    owner's live registry: the plan's rule is that missing coverage defers work,
    it never silently falls back.
    """
    strata = (state_root or (config.STATE_DIR / owner)) / "strata"
    if not strata.is_dir():
        return None

    def _candidate(run_id: str) -> Optional[OwnerCorpus]:
        bundle = strata / run_id
        manifest_path = bundle / "manifest.json"
        registry_csv = bundle / "state" / "cache" / "jobs.csv"
        if not manifest_path.is_file() or not registry_csv.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"[consume] unreadable owner manifest {manifest_path}: {e}")
            return None
        digest = str((manifest.get("integrity") or {}).get("file_inventory_digest") or "")
        return OwnerCorpus(
            owner=owner,
            run_id=str((manifest.get("run") or {}).get("run_id") or run_id),
            bundle=bundle,
            registry_csv=registry_csv,
            enrichment_dir=bundle / "state" / "cache" / "enrichment",
            manifest_digest=digest,
        )

    current = strata / "current"
    if current.is_symlink():
        sealed = _candidate(os.readlink(current))
        if sealed is not None:
            return sealed

    # No usable pointer (a fresh clone, a partial transfer, or a seal whose
    # manifest is gone): fall back to the newest bundle that is actually
    # complete, ordered by the manifest's own seal time — never by mtime.
    best: tuple[str, OwnerCorpus] | None = None
    for path in sorted(p for p in strata.iterdir() if p.is_dir() and not p.is_symlink()):
        if path.name.startswith(".") or path.name == "history-index":
            continue
        sealed = _candidate(path.name)
        if sealed is None:
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            sealed_at = str((manifest.get("clock") or {}).get("sealed_at") or "")
        except (OSError, ValueError):
            sealed_at = ""
        if best is None or sealed_at > best[0]:
            best = (sealed_at, sealed)
    return best[1] if best else None


def _owner_records(corpus: OwnerCorpus, sources: List[str]) -> List[JobRecord]:
    """The owner's sealed corpus for these sources.

    Adverts the owner has marked absent — `disappeared` or the stronger
    `not_listed_in_scope` — are deliberately excluded: they are not refreshed
    here, so the consumer's own `mark_run_complete` retires its copies too, and
    the owner keeps lifecycle ownership. The consumer never claims a scope
    completeness it did not verify itself, so its own retirement stays
    `disappeared`.
    """
    wanted = set(sources)
    registry = JobRegistry(corpus.registry_csv)
    return [
        record
        for record in registry.all_jobs()
        if (record.source or "") in wanted
        and record.status not in _ABSENT_STATUSES
    ]


def _owner_records_live(owner_runtime: PrincipalRuntime, sources: List[str]) -> List[JobRecord]:
    """The owner's *live* corpus — only with the documented env override.

    Kept for the transition while an owner has not yet produced a seal
    (`LOVEWORK_CONSUME_ALLOW_LIVE=1`); the default path never uses it.
    """
    wanted = set(sources)
    registry = JobRegistry(owner_runtime.cache_dir / "jobs.csv")
    return [
        record
        for record in registry.all_jobs()
        if (record.source or "") in wanted
        and record.status not in _ABSENT_STATUSES
    ]


def _marker_path(runtime: PrincipalRuntime) -> Path:
    return runtime.cache_dir / MARKER_FILENAME


def _read_marker(runtime: PrincipalRuntime) -> dict:
    path = _marker_path(runtime)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Unreadable consume marker {path}: {e}")
        return {}


def _write_marker(runtime: PrincipalRuntime, value: dict) -> None:
    path = _marker_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def allow_live_owner() -> bool:
    """Whether the transitional live-owner path is permitted.

    Off by default: an ingest reads the owner's *sealed* corpus or defers. The
    override exists only to bridge a period where an owner has not yet produced
    a seal — it is not a normal operating mode.
    """
    return os.getenv("LOVEWORK_CONSUME_ALLOW_LIVE", "").strip() in {"1", "true", "yes"}


def consume(
    profile_name: str,
    role: str,
    *,
    owner_run_id: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    runtime: Optional[PrincipalRuntime] = None,
    llm: Optional[LLMClient] = None,
    owner_override: Optional[str] = None,
    source_override: Optional[str] = None,
) -> dict:
    """Ingest every delegated source from its owner. Returns a summary dict.

    The corpus is the owner's newest **sealed** bundle; when the owner has no
    eligible seal the ingest is deferred (`status="blocked"`) rather than read
    from live state. `LOVEWORK_CONSUME_ALLOW_LIVE=1` bridges a transition and is
    the only way the live path is used.
    """
    runtime = runtime or resolve_principal_runtime(profile_name)
    plan = _group_by_owner(profile_name, owner_override, source_override)
    if not plan:
        return {"status": "nothing-declared", "entries": 0, "owners": {}, "counts": {}}

    # `reassess_records` may write content-addressed assessment-cache entries
    # (reassess.py documents that the caller owns this): a preview must leave no
    # trace, so the matching step gets a scratch cache. Caught by a real
    # `--dry-run` leaving two files in `state/kj/cache/assessments/` (2026-09-20).
    match_runtime = runtime
    if dry_run:
        match_runtime = replace(
            runtime, cache_dir=Path(tempfile.mkdtemp(prefix="lovework-consume-preview-"))
        )

    blocked: Dict[str, str] = {}
    owner_seals: Dict[str, str] = {}
    corpora: Dict[str, Optional[OwnerCorpus]] = {}
    live_owners: List[str] = []
    for owner in plan:
        owner_runtime = resolve_principal_runtime(owner)
        # The state root is derived, not stored: `cache_dir` already encodes it
        # (the same idiom as PrincipalRuntime.letters_dir).
        corpus = resolve_owner_corpus(owner, state_root=owner_runtime.cache_dir.parent)
        corpora[owner] = corpus
        if corpus is None:
            if allow_live_owner():
                live_owners.append(owner)
                logger.warning(
                    f"[consume] {owner} has no eligible seal; reading LIVE state "
                    f"because LOVEWORK_CONSUME_ALLOW_LIVE=1"
                )
            else:
                blocked[owner] = "no eligible owner seal"
                logger.warning(
                    f"[consume] {owner} has no eligible sealed corpus — deferring this "
                    f"ingest instead of reading live state (docs/28 §WP1b box ⑤). "
                    f"LOVEWORK_CONSUME_ALLOW_LIVE=1 exists only to bridge a transition."
                )
        else:
            owner_seals[owner] = corpus.manifest_digest

    # Nothing to ingest and nothing to say: an explicit deferral, no writes.
    if blocked and not any(corpora[owner] or owner in live_owners for owner in plan):
        return {
            "status": "blocked",
            "reason": "no eligible owner seal",
            "entries": 0,
            "owners": {},
            "counts": {},
            "blocked": blocked,
            "owner_seals": {},
        }

    marker_state = _read_marker(runtime)
    # Idempotence is bound to the owner's SEAL (its manifest digest) together
    # with the sources and this principal's profile/role — not to a run id alone,
    # because a run id can name a run whose bundle we never read
    # (docs/28 §WP1b box ⑤).
    seal_key = "|".join(
        [profile_name.lower(), role or ""]
        + [f"{owner}:{digest}" for owner, digest in sorted(owner_seals.items())]
    )
    if not force and not dry_run:
        if marker_state.get("seal_key") == seal_key:
            return {
                "status": "already-consumed",
                "entries": 0,
                "owners": {},
                "counts": {},
                "seal_key": seal_key,
                "owner_seals": owner_seals,
            }
        if owner_run_id and marker_state.get("owner_run_id") == owner_run_id:
            return {
                "status": "already-consumed",
                "entries": 0,
                "owners": {},
                "counts": {},
                "owner_run_id": owner_run_id,
                "owner_seals": owner_seals,
            }

    registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    wiki = WikiStore(root=runtime.wiki_root)
    marker = _read_marker(runtime)
    previously_seen: Dict[str, List[str]] = marker.get("seen") or {}
    now_seen: Dict[str, List[str]] = {}
    all_entries: List[WikiEntry] = []
    summary: Dict[str, dict] = {}
    empty_sources: List[str] = []
    profile_text = ""

    for owner, sources in plan.items():
        owner_runtime = resolve_principal_runtime(owner)
        corpus = corpora.get(owner)
        if corpus is None and owner not in live_owners:
            continue  # blocked above: deferred, never read live
        if corpus is None:
            records = _owner_records_live(owner_runtime, sources)
            evidence_dir = owner_runtime.cache_dir / "enrichment"
        else:
            records = _owner_records(corpus, sources)
            evidence_dir = corpus.enrichment_dir
            summary[owner] = {
                **(summary.get(owner) or {}),
                "seal": {"run_id": corpus.run_id, "digest": corpus.manifest_digest},
            }
        per_source = Counter((record.source or "") for record in records)
        summary[owner] = {
            **(summary.get(owner) or {}),
            "sources": sources,
            "records": len(records),
            "per_source": {source: per_source.get(source, 0) for source in sources},
        }
        # A misnamed source, or one the owner has never run, would otherwise
        # consume nothing at all — silently, every week. Say so loudly.
        for source in sources:
            if not per_source.get(source):
                empty_sources.append(source)
                logger.warning(
                    f"[consume] {owner} holds no records for {source!r} in its sealed corpus — "
                    "misnamed source, or the owner has never run it"
                )
        if not records:
            logger.warning(f"[consume] {owner} holds no records for {sources}; skipping")
            continue

        entries, coverage, profile_text, _ = reassess_records(
            profile_name,
            role,
            runtime=match_runtime,
            registry=registry,
            records=records,
            evidence_cache_dir=evidence_dir,
            # Evidence may be READ from the owner's seal; anything written
            # goes to the consumer's own cache, so a seal stays immutable.
            write_evidence_cache_dir=match_runtime.cache_dir / "enrichment",
            llm=llm,
        )
        summary[owner].update(coverage)

        if dry_run:
            all_entries.extend(entries)
            continue

        first_seen_by_hash = {record.hash: record.first_seen for record in records}
        # The owner is the authority on what it verified at the hiring party's
        # own publication, so its `last_primary_seen` is *carried*, not re-dated:
        # the consumer never observed the primary page itself (§WP3b box 1).
        primary_by_hash = {record.hash: record.last_primary_seen
                           for record in records if record.last_primary_seen}
        for entry in entries:
            record = registry.upsert(
                org=entry.org_name,
                title=entry.title,
                url=entry.url or "",
                source=entry.source or "",
                discovery_url=entry.discovery_url or "",
                discovery_date=entry.discovery_date or "",
                first_seen=first_seen_by_hash.get(
                    _job_hash(entry.org_name, entry.title, entry.url or ""), ""
                ),
                last_primary_seen=primary_by_hash.get(
                    _job_hash(entry.org_name, entry.title, entry.url or ""), ""
                ),
            )
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
            all_entries.append(entry)
            now_seen.setdefault(entry.source or "", []).append(record.hash)

        # Retire exactly the adverts that dropped out of the owner's live corpus
        # since the previous ingest. Adverts this principal discovered itself
        # were never in the marker, so they are never retired here.
        retired = 0
        for source in sources:
            gone = set(previously_seen.get(source) or []) - set(now_seen.get(source) or [])
            retired += registry.retire_hashes(gone)
        summary[owner]["retired"] = retired

    counts = {
        decision: sum(entry.decision == decision for entry in all_entries)
        for decision in ("GO", "MAYBE", "FLAG", "DROP")
    }
    result = {
        "status": "dry-run" if dry_run else "consumed",
        "entries": len(all_entries),
        "owners": summary,
        "counts": counts,
        "empty_sources": empty_sources,
        "owner_seals": owner_seals,
    }
    if blocked:
        result["blocked"] = blocked
    if dry_run or not all_entries:
        if not dry_run:
            # Nothing to ingest is still a *completed ingest of this seal*: bind
            # the marker so a rerun short-circuits instead of re-scoring every
            # advert. `seen` must preserve the previously-known corpus — writing
            # an empty set would tell the next ingest that every advert had
            # dropped out and mass-retire the consumer's copies.
            merged_seen = {
                source: sorted(set(previously_seen.get(source) or [])
                               | set(now_seen.get(source) or []))
                for source in set(previously_seen) | set(now_seen)
            }
            _write_marker(
                runtime,
                {
                    "owner_run_id": owner_run_id,
                    "seal_key": seal_key,
                    "owner_seals": owner_seals,
                    "consumed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "owners": plan,
                    "seen": merged_seen,
                    "note": "nothing-to-ingest",
                },
            )
            result["status"] = "nothing-to-ingest"
        return result

    for entry in all_entries:
        try:
            wiki.update_org_page(entry)
        except Exception as e:
            logger.warning(f"Wiki update failed for {entry.org_name}: {e}")

    report = wiki.save_report(
        all_entries,
        profile_name=f"{profile_name.upper()}-{role}",
        run_type="consume",
    )
    try:
        wiki.rebuild_index(all_entries)
    except Exception as e:
        logger.warning(f"Wiki index rebuild failed: {e}")
    result["report"] = str(report)

    sources_all = sorted({source for sources in plan.values() for source in sources})
    run_id = new_run_id()
    append_run(
        runtime.dataset_dir,
        run_id=run_id,
        profile_name=profile_name,
        role=role,
        sources=sources_all,
        profile_text=profile_text,
        mode="consume",
    )
    append_assessments(
        runtime.dataset_dir,
        all_entries,
        run_id=run_id,
        profile_name=profile_name,
        role=role,
        sources=sources_all,
    )

    _write_marker(
        runtime,
        {
            "owner_run_id": owner_run_id,
            # Idempotence is bound to the owner's seal digest(s), not just to a
            # run id: re-running against the same sealed corpus is free, and a
            # new seal re-opens the ingest (docs/28 §WP1b box ⑤).
            "seal_key": seal_key,
            "owner_seals": owner_seals,
            "consumed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "owners": plan,
            # Hash of every advert ingested, per source. The next ingest retires
            # exactly what has dropped out of this set — never anything else.
            "seen": now_seen,
        },
    )
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="Principal that consumes, e.g. kj")
    parser.add_argument("--role", required=True, help="Role criteria file for scoring")
    parser.add_argument("--owner", help="Override the owner principal (with --source)")
    parser.add_argument("--source", help="Comma-separated sources to ingest (requires --owner)")
    parser.add_argument(
        "--owner-run-id",
        help="Owner run that triggered this ingest; used as the idempotence key",
    )
    parser.add_argument("--dry-run", action="store_true", help="Score without writing anything")
    parser.add_argument("--force", action="store_true", help="Ignore the idempotence marker")
    args = parser.parse_args()

    # WP1b box ①: a direct invocation holds the consumer's writer lease so it
    # cannot race a seal or another crawl. `--dry-run` writes nothing and must
    # not contend for the real lease; an operation child carries the marker and
    # skips the acquire entirely.
    guard = nullcontext() if args.dry_run else standalone_writer_guard(
        args.profile, mode="direct",
        state_root=resolve_principal_runtime(args.profile).cache_dir.parent)
    try:
        with guard:
            result = consume(
                args.profile,
                args.role,
                owner_run_id=args.owner_run_id,
                dry_run=args.dry_run,
                force=args.force,
                owner_override=args.owner,
                source_override=args.source,
            )
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)

    counts = result.get("counts") or {}
    detail = ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "no leads"
    owners = ", ".join(
        f"{owner}:{info.get('records', 0)} records" for owner, info in (result.get("owners") or {}).items()
    )
    # One concise line for cron/Telegram delivery; empty stdout stays silent.
    print(f"[consume] {args.profile} status={result['status']} entries={result['entries']} ({detail}) {owners}")
    if result.get("empty_sources"):
        print(f"WARNING: no live records from the owner for {', '.join(result['empty_sources'])}")
    if result.get("report"):
        print(f"Report: {result['report']}")
        # The operation runner registers the produced artifact by parsing this
        # marker out of stdout (crawl_operation.py) — never "newest by mtime".
        print(f"[REPORT_PATH] {result['report']}", flush=True)
    if result.get("blocked"):
        for owner, reason in result["blocked"].items():
            print(f"[consume] deferred: owner {owner} — {reason} (no live fallback)")
    if args.dry_run:
        print("Preview only: registry, wiki, ledger and marker untouched.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""AsOf readers (WP2a) — sealed-level queries over strata bundles.

The read half of chapter 26 §6 against the WP1a engine. Everything here
is read-only, local, zero-LLM. **Sealed bundles are the truth**; the
history index is a disposable cache — readers detect a stale index and say
so rather than answering from it.

Honesty rules binding every command:

* A fact is answered only by a **seal** (bundle + manifest digest); every
  returned structure names its seal(s). No event-level knowledge is
  invented: at this layer we know what a seal captured, not what the world
  did between seals (that is WP3's typed observations).
* `state --at T` selects the latest seal with ``sealed_at ≤ T`` and shows
  the requested/effective cutoff and the lag. Before the first seal:
  ``unknown``, never a guess. Seals after T are never consulted.
* Registry transitions read from frozen ``jobs.csv`` copies are labelled
  ``legacy_registry_state_change`` — processing-date semantics, not
  qualified liveness (chapter 26 §5).
* Original outputs are retrieved as sealed bytes (``cat``); a derived view
  never rewrites them, and disagreement between a report's claims and the
  ledger is surfaced, not reconciled away.

Usage::

    ../venv/bin/python3 asof.py --profile lj runs [--before T] [--limit N]
    ../venv/bin/python3 asof.py --profile lj state --at 2026-09-14T12:00:00+00:00
    ../venv/bin/python3 asof.py --profile lj when --org dex
    ../venv/bin/python3 asof.py --profile lj when --path state/wiki/orgs/dex.md
    ../venv/bin/python3 asof.py --profile lj cat --run-id X --path outputs/report.md
    ../venv/bin/python3 asof.py --profile lj diff --from A --to B
    ../venv/bin/python3 asof.py --profile lj verify [--run-id X] [--deep]
    ../venv/bin/python3 asof.py --profile lj journal --from A --to B [--write]

Add ``--json`` to any command for machine output.
"""

from __future__ import annotations
import jsonl_io

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import strata_contract as sc
from strata import StrataEngine, index_row, parse_manifest

JOURNAL_REDUCER_VERSION = 1
REGISTRY_CSV = "state/cache/jobs.csv"
REGISTRY_MEANING = "legacy_registry_state_change"


def _event_summary(kind: str, row: dict) -> str:
    """One line a reader can act on, without knowing the ledger's schema."""
    if kind == "assessment":
        return (f"{row.get('decision') or '?'} "
                f"({row.get('combined_score')}) — "
                f"{(row.get('reasoning') or '')[:80]}")
    if kind == "outcome":
        return (f"{row.get('event_type') or row.get('kind') or 'outcome'}: "
                f"{row.get('notes') or row.get('path') or ''}"[:120])
    if kind == "run":
        return (f"{row.get('mode') or '?'} run "
                f"{row.get('execution') or ''}/{row.get('capture') or ''} "
                f"({', '.join(row.get('sources') or []) or 'no sources'})")
    return ""


def _qualify_time(value: str | None) -> dict:
    """Report a stored time with its precision and zone stated, not assumed.

    `2026-09-12` and `2026-09-12T14:03:11Z` are different claims; a naive
    timestamp carries no known zone. Unknown stays unknown (docs/28 §WP3b box 3).
    """
    if not value:
        return {"value": None, "precision": None, "timezone": None, "source": "absent"}
    text = str(value)
    if "T" not in text:
        return {"value": text, "precision": "date", "timezone": "unspecified",
                "source": "recorded"}
    zone = "UTC" if text.endswith(("Z", "+00:00")) else "unspecified"
    return {"value": text, "precision": "timestamp", "timezone": zone,
            "source": "recorded"}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ── the reader ───────────────────────────────────────────────────────────


@dataclass
class SealView:
    run_id: str
    sealed_at: str
    digest: str
    manifest: sc.Manifest


class Asof:
    """Read-only query surface over one principal's strata."""

    def __init__(self, engine: StrataEngine):
        self.engine = engine

    # -- seal discovery -------------------------------------------------

    def seals(self) -> list[SealView]:
        """All sealed bundles, oldest first. Truth comes from bundles; the
        index is cross-checked and a mismatch becomes a warning."""
        views = []
        for run_id in self.engine.sealed_run_ids():
            manifest = self.engine.read_manifest(run_id)
            if manifest is not None:
                views.append(SealView(run_id, manifest.clock.sealed_at,
                                      manifest.digest(), manifest))
        views.sort(key=lambda v: (v.sealed_at, v.run_id))
        return views

    def stale_index_warnings(self) -> list[str]:
        bundle_ids = {v.run_id for v in self.seals()}
        if not self.engine.index_path.is_file():
            return ["stale_index: index missing (run index-rebuild)"] \
                if bundle_ids else []
        indexed = {json.loads(l)["run_id"]
                   for l in jsonl_io.lines(self.engine.index_path)}
        warnings = []
        if bundle_ids - indexed:
            warnings.append(
                f"stale_index: {len(bundle_ids - indexed)} sealed bundle(s) "
                f"missing from the index; answering from bundles "
                f"(run index-rebuild)")
        if indexed - bundle_ids:
            warnings.append(
                f"stale_index: index names unknown runs "
                f"{sorted(indexed - bundle_ids)}")
        return warnings

    def _seal_at(self, cutoff: str) -> tuple[SealView | None, dict]:
        """Latest seal with sealed_at ≤ cutoff, plus the cutoff metadata
        every caller must show."""
        cutoff_dt = _parse_ts(cutoff)
        eligible = [v for v in self.seals() if _parse_ts(v.sealed_at) <= cutoff_dt]
        meta = {"requested_cutoff": cutoff, "effective_seal": None,
                "lag_seconds": None, "history_starts_at":
                    self.seals()[0].sealed_at if self.seals() else None}
        if not eligible:
            return None, meta
        chosen = eligible[-1]
        meta["effective_seal"] = chosen.run_id
        meta["lag_seconds"] = round(
            (cutoff_dt - _parse_ts(chosen.sealed_at)).total_seconds(), 3)
        return chosen, meta

    # -- commands ---------------------------------------------------------

    def runs(self, *, before: str | None = None, limit: int = 20) -> dict:
        seals = self.seals()
        if before:
            cutoff_dt = _parse_ts(before)
            seals = [v for v in seals if _parse_ts(v.sealed_at) <= cutoff_dt]
        rows = [{
            "run_id": v.run_id, "sealed_at": v.sealed_at,
            "mode": v.manifest.run.mode,
            "operation_kind": v.manifest.run.operation_kind,
            "execution": v.manifest.status.execution,
            "capture": v.manifest.status.capture,
            "digest": v.digest,
        } for v in seals]
        out = {"principal": self._principal(), "runs": rows[-limit:],
               "total": len(rows),
               "warnings": self.stale_index_warnings()}
        if before:
            out["cutoff"] = before
        return out

    def state_at(self, cutoff: str) -> dict:
        chosen, meta = self._seal_at(cutoff)
        out = {"principal": self._principal(), **meta,
               "answer": "unknown" if chosen is None else "sealed_snapshot",
               "warnings": self.stale_index_warnings()}
        if chosen is None:
            if meta["history_starts_at"]:
                out["reason"] = (
                    f"no seal at or before {cutoff}; history starts at "
                    f"{meta['history_starts_at']}")
            else:
                out["reason"] = "no sealed history yet (WP1b wires sealing)"
            return out
        m = chosen.manifest
        out["seal_digest"] = chosen.digest
        out["status"] = m.status.to_dict()
        out["coverage"] = [{"source": c.source, "scope": c.scope,
                            "outcome": c.outcome} for c in m.coverage]
        out["inputs"] = [{"role": i.role, "sha256": i.sha256}
                         for i in m.inputs]
        out["artifacts"] = {
            "state_file_count": m.state.file_count if m.state else 0,
            "ledger_rows": {l.path.split("/")[-1]: l.rows
                            for l in m.ledger_copies},
            "original_outputs": [o.path for o in m.outputs if o.original],
        }
        out["capabilities"] = m.capabilities.to_dict()
        out["gaps"] = [g.reason for g in m.gaps]
        out["not_answerable_here"] = [
            "event-level knowledge between seals — see `events`, which reads "
            "the write-ahead ledgers and says so",
            "qualified liveness — registry transitions are legacy "
            "processing dates",
            "counterfactuals (WP5 replay)",
        ]
        return out

    def when(self, *, path: str | None = None, org: str | None = None,
             at: str | None = None) -> dict:
        """Seal-level presence timeline for a bundle path (or any path
        containing an org slug). Honest scope: files, not world facts.

        With `at`, only seals that existed by that cutoff contribute — and the
        result carries the **same cutoff metadata shape as `state --at`**
        (`requested_cutoff`, `effective_seal`, `lag_seconds`,
        `history_starts_at`), so an operator can diff the two readers without
        translating between them.
        """
        if bool(path) == bool(org):
            raise ValueError("give exactly one of --path / --org")
        needle = path or f"{org.lower()}"
        seals = self.seals()
        cutoff_meta = None
        if at is not None:
            # Same machinery, same semantics: `_seal_at` also computes the lag
            # against the seal that actually answered.
            _, cutoff_meta = self._seal_at(at)
            cutoff_dt = _parse_ts(at)
            seals = [v for v in seals
                     if _parse_ts(v.sealed_at) <= cutoff_dt]
        timeline = []
        for v in seals:
            inv = self._inventory(v.run_id)
            matches = sorted(p for p in inv
                             if (path and p == path)
                             or (not path and needle in p.lower()))
            for p in matches:
                timeline.append({
                    "path": p, "sealed_at": v.sealed_at, "run_id": v.run_id,
                    "sha256": inv[p]["sha256"],
                    "change": "present",
                })
        # collapse per path: mark first sighting and digest changes
        events: list[dict] = []
        last_digest: dict[str, str] = {}
        first_seen: dict[str, dict] = {}
        for row in timeline:
            p = row["path"]
            if p not in first_seen:
                first_seen[p] = row
                events.append({**row, "event": "first_seen"})
                last_digest[p] = row["sha256"]
            elif row["sha256"] != last_digest[p]:
                events.append({**row, "event": "content_changed"})
                last_digest[p] = row["sha256"]
        out = {"principal": self._principal(), "needle": needle,
               "events": events,
               "scope": "seal-level file presence/digests; not world facts",
               "warnings": self.stale_index_warnings()}
        if cutoff_meta is not None:
            out["cutoff"] = cutoff_meta
        return out

    def events(self, *, at: str | None = None, kind: str | None = None,
               org: str | None = None, limit: int = 50) -> dict:
        """Typed observation events up to a cutoff, from the ledgers.

        This is the event-level counterpart to `state --at`: the seal reader can
        only see state *at seal times*, while the ledgers record what happened
        between them (a run starting, an assessment scored, an outcome observed).

        **Honest limit, stated in the result:** these are write-ahead ledger
        rows, not sealed evidence. They can describe work that left no bundle,
        and their timestamps are what the writer recorded — possibly naive
        (`_qualify_time` says so per event). Sealed truth remains the bundles.
        """
        dataset = self.engine.state_root / "dataset"
        sources = [("run", dataset / "runs.jsonl", "started_at"),
                   ("assessment", dataset / "assessments.jsonl", "observed_at"),
                   ("outcome", dataset / "outcomes.jsonl", "observed_at")]
        cutoff_meta = None
        cutoff_dt = None
        if at is not None:
            _, cutoff_meta = self._seal_at(at)      # same shape as state --at
            cutoff_dt = _parse_ts(at)
        collected: list[dict] = []
        ledgers: dict[str, int] = {}
        for event_kind, path, time_key in sources:
            if kind and kind != event_kind:
                continue
            if not path.is_file():
                continue
            rows = [json.loads(l) for l in jsonl_io.lines(path) if l.strip()]
            ledgers[event_kind] = len(rows)
            for row in rows:
                when = _qualify_time(row.get(time_key))
                if cutoff_dt is not None:
                    if when["value"] is None:
                        continue          # undated events cannot be placed in time
                    try:
                        event_dt = _parse_ts(when["value"])
                    except ValueError:
                        continue
                    if event_dt.tzinfo is None:
                        # Rows written before the writer emitted aware stamps.
                        # They are ordered by *assuming* UTC and the assumption
                        # is recorded on the event, because a naive stamp could
                        # be off by the writer's local offset.
                        event_dt = event_dt.replace(tzinfo=cutoff_dt.tzinfo)
                        when = {**when, "assumed_timezone": "UTC"}
                    if event_dt > cutoff_dt:
                        continue
                if org and org.lower() not in (row.get("org_name") or "").lower():
                    continue
                collected.append({
                    "kind": event_kind,
                    "observed_at": when,
                    "run_id": row.get("run_id"),
                    "org_name": row.get("org_name"),
                    "title": row.get("title"),
                    "summary": _event_summary(event_kind, row),
                    "source": row.get("source"),
                })
        collected.sort(key=lambda e: (e["observed_at"]["value"] or "",
                                      e["kind"], e["run_id"] or ""))
        out = {
            "principal": self._principal(),
            "events": collected[-limit:] if limit else collected,
            "total": len(collected),
            "ledger_rows": ledgers,
            "evidence": "write-ahead ledgers (not sealed bundles)",
            "scope": ("event-level recording facts; an event's time is when the "
                      "writer recorded it, not when the world changed"),
            "warnings": self.stale_index_warnings() + [
                "ledger_only: these events are not independently verifiable "
                "from a seal; sealed bundles remain the truth"
            ] + ([
                "timezone_assumed: some events predate aware timestamps; they "
                "were ordered as if UTC (see assumed_timezone per event)"
            ] if any(e["observed_at"].get("assumed_timezone")
                     for e in collected) else []),
        }
        if cutoff_meta is not None:
            out["cutoff"] = cutoff_meta
        return out

    def cat(self, run_id: str, path: str) -> dict:
        """Frozen bytes from a bundle. The path must be part of that seal's
        inventory — no live fallback, no unsealed files."""
        bundle = self.engine.bundle_path(run_id)
        if run_id not in self.engine.sealed_run_ids():
            return {"error": f"unknown_run: {run_id} is not a sealed bundle"}
        if not sc.confined(path):
            return {"error": f"unconfined_path: {path}"}
        inv = self._inventory(run_id)
        if path not in inv:
            return {"error": (
                f"not_sealed_evidence: {path} is not in {run_id}'s "
                f"inventory (no live fallback is permitted)")}
        full = bundle / path
        if not full.is_file():
            return {"error": f"missing_file: {path} (verify reported this)"}
        return {"run_id": run_id, "path": path,
                "sha256": inv[path]["sha256"], "bytes": full.read_bytes()}

    def diff(self, from_run: str, to_run: str) -> dict:
        rows = self.journal(from_run, to_run)
        return {"principal": self._principal(), "from": from_run,
                "to": to_run, "journal": rows}

    def journal(self, from_run: str, to_run: str,
                *, write: bool = False) -> list[dict]:
        """Deterministic pairwise journal between two seals (ch26 §5 shape).

        File rows come from the two inventories (digests are identity;
        same inode is only a reuse hint). Registry rows read the frozen
        jobs.csv copies and are labelled legacy processing-date changes.
        Coverage/input rows come from the manifests. No gap rows are
        invented from file diffs — gaps exist only when a manifest recorded
        them (WP3 adds typed observation events).
        """
        a, b = self._require_seal(from_run), self._require_seal(to_run)
        inv_a, inv_b = self._inventory(a.run_id), self._inventory(b.run_id)

        rows: list[dict] = [{
            "kind": "journal_header",
            "from_run_id": a.run_id, "to_run_id": b.run_id,
            "from_digest": a.digest, "to_digest": b.digest,
            "reducer_version": JOURNAL_REDUCER_VERSION,
        }]

        for path in sorted(set(inv_a) | set(inv_b)):
            ea, eb = inv_a.get(path), inv_b.get(path)
            if ea and eb:
                if ea["sha256"] != eb["sha256"]:
                    rows.append({
                        "kind": "file_change", "path": path,
                        "change": "modified",
                        "same_inode": ea["ino"] == eb["ino"],
                        "sha256_from": ea["sha256"],
                        "sha256_to": eb["sha256"],
                        "bytes_delta": eb["bytes"] - ea["bytes"],
                    })
            elif ea:
                rows.append({"kind": "file_change", "path": path,
                             "change": "removed",
                             "sha256_from": ea["sha256"], "same_inode": False})
            else:
                rows.append({"kind": "file_change", "path": path,
                             "change": "added",
                             "sha256_to": eb["sha256"], "same_inode": False})

        rows += self._registry_transitions(a, b)
        rows += self._coverage_changes(a, b)
        rows += self._input_changes(a, b)

        if write:
            out_dir = self.engine.strata_root / "history-index" / "journal"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{a.run_id}--{b.run_id}.jsonl"
            tmp = target.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(
                sc.canonical_json(r) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, target)
        return rows

    # -- journal pieces -----------------------------------------------------

    def _registry_transitions(self, a: SealView, b: SealView) -> list[dict]:
        path_a = self.engine.bundle_path(a.run_id) / REGISTRY_CSV
        path_b = self.engine.bundle_path(b.run_id) / REGISTRY_CSV
        if not (path_a.is_file() and path_b.is_file()):
            return []
        def _rows(path: Path) -> dict[str, dict]:
            with path.open(newline="", encoding="utf-8") as fh:
                return {r["id"]: r for r in csv.DictReader(fh)}
        ra, rb = _rows(path_a), _rows(path_b)
        out = []
        for job_id in sorted(set(ra) | set(rb)):
            fa, fb = ra.get(job_id), rb.get(job_id)
            if fa and not fb:
                out.append({"kind": "registry_transition", "id": job_id,
                            "org": fa.get("org"), "title": fa.get("title"),
                            "from": fa.get("status"), "to": "absent",
                            "meaning": REGISTRY_MEANING,
                            "evidence_refs": [
                                f"{a.run_id}/{REGISTRY_CSV}",
                                f"{b.run_id}/{REGISTRY_CSV}"]})
            elif fb and not fa:
                out.append({"kind": "registry_transition", "id": job_id,
                            "org": fb.get("org"), "title": fb.get("title"),
                            "from": "absent", "to": fb.get("status"),
                            "meaning": REGISTRY_MEANING,
                            "evidence_refs": [
                                f"{a.run_id}/{REGISTRY_CSV}",
                                f"{b.run_id}/{REGISTRY_CSV}"]})
            elif fa and fb and (fa.get("status") != fb.get("status")
                                or fa.get("last_seen") != fb.get("last_seen")):
                out.append({"kind": "registry_transition", "id": job_id,
                            "org": fb.get("org"), "title": fb.get("title"),
                            "from": fa.get("status"), "to": fb.get("status"),
                            "meaning": REGISTRY_MEANING,
                            "evidence_refs": [
                                f"{a.run_id}/{REGISTRY_CSV}",
                                f"{b.run_id}/{REGISTRY_CSV}"]})
        return out

    def _coverage_changes(self, a: SealView, b: SealView) -> list[dict]:
        def _key(c):
            return (c.source, c.scope)
        ca = {_key(c): c for c in a.manifest.coverage}
        cb = {_key(c): c for c in b.manifest.coverage}
        out = []
        for key in sorted(set(ca) | set(cb)):
            fa, fb = ca.get(key), cb.get(key)
            if fa and fb and fa.outcome != fb.outcome:
                out.append({"kind": "coverage_change", "source": key[0],
                            "scope": key[1], "from_outcome": fa.outcome,
                            "to_outcome": fb.outcome})
            elif fa and not fb:
                out.append({"kind": "coverage_change", "source": key[0],
                            "scope": key[1], "from_outcome": fa.outcome,
                            "to_outcome": "not_attempted"})
            elif fb and not fa:
                out.append({"kind": "coverage_change", "source": key[0],
                            "scope": key[1], "from_outcome": "not_attempted",
                            "to_outcome": fb.outcome})
        return out

    def _input_changes(self, a: SealView, b: SealView) -> list[dict]:
        ia = {i.role: i for i in a.manifest.inputs}
        ib = {i.role: i for i in b.manifest.inputs}
        out = []
        for role in sorted(set(ia) | set(ib)):
            fa, fb = ia.get(role), ib.get(role)
            if fa and fb and fa.sha256 != fb.sha256:
                out.append({"kind": "input_change", "role": role,
                            "sha256_from": fa.sha256, "sha256_to": fb.sha256})
            elif fa and not fb:
                out.append({"kind": "input_change", "role": role,
                            "sha256_from": fa.sha256, "to": "unpinned"})
            elif fb and not fa:
                out.append({"kind": "input_change", "role": role,
                            "from": "unpinned", "sha256_to": fb.sha256})
        return out

    # -- assessment explanation ------------------------------------------

    def explain(self, *, assessment_id: str | None = None,
                run_id: str | None = None, org: str | None = None,
                title: str | None = None) -> dict:
        """Explain one assessment: the decision, its evidence, its policy, and
        **when each of those facts came into being**.

        Time is qualified explicitly rather than collapsed into one date:

        - `observed_at` — when the run recorded the assessment (the run's clock);
        - `primary_fetched_at` — when the advert's own content was fetched;
        - `sealed_at` — when that run's bundle sealed.

        An assessment whose run never sealed is **ledger-only evidence** and is
        reported as such: the ledger is a write-ahead record, so its rows can
        describe work that left no sealed bundle (docs/26 §evidence limits).
        """
        ledger = self.engine.state_root / "dataset" / "assessments.jsonl"
        if assessment_id is None and run_id is None:
            # A vague `not_found` after scanning the whole ledger would hide a
            # caller mistake; say what is missing instead.
            return {"status": "no_selector",
                    "detail": "give --assessment-id, or --run-id with --org "
                              "and/or --title"}
        if not ledger.is_file():
            return {"status": "no_ledger", "ledger": str(ledger),
                    "detail": "no assessments ledger for this principal"}
        rows = [json.loads(l) for l in jsonl_io.lines(ledger) if l.strip()]
        matches = [r for r in rows
                   if (assessment_id and r.get("assessment_id") == assessment_id)
                   or (not assessment_id and run_id and r.get("run_id") == run_id
                       and (org is None or (r.get("org_name") or "").lower()
                            == org.lower())
                       and (title is None or (r.get("title") or "").lower()
                            == title.lower()))]
        if not matches:
            return {"status": "not_found", "ledger": str(ledger),
                    "detail": "no assessment matches that selector"}
        row = matches[-1]          # the newest match, deterministically
        sealed = None
        try:
            sealed = self._require_seal(str(row.get("run_id") or ""))
        except ValueError:
            pass
        return {
            "status": "ok",
            "assessment_id": row.get("assessment_id"),
            "run_id": row.get("run_id"),
            "org_name": row.get("org_name"),
            "title": row.get("title"),
            "decision": {
                "decision": row.get("decision"),
                "recommended_action": row.get("recommended_action"),
                "combined_score": row.get("combined_score"),
                "score": row.get("score"),
                "fit_score": row.get("fit_score"),
                "reach_score": row.get("reach_score"),
                "flourish_score": row.get("flourish_score"),
                "reasoning": row.get("reasoning"),
                "assessment_status": row.get("assessment_status"),
                "lifecycle_status": row.get("lifecycle_status"),
            },
            # When each fact came into being, with its precision stated rather
            # than assumed: an ISO date and an ISO timestamp are different
            # claims, and a naive timestamp has no known zone.
            "time_basis": {
                "observed_at": _qualify_time(row.get("observed_at")),
                "primary_fetched_at": _qualify_time(row.get("primary_fetched_at")),
                "sealed_at": ({"value": sealed.sealed_at, "precision": "timestamp",
                               "timezone": "UTC", "source": "seal"}
                              if sealed is not None else
                              {"value": None, "precision": None, "timezone": None,
                               "source": "no_seal"}),
            },
            "evidence": {
                "advert_hash": row.get("advert_hash"),
                "primary_content_hash": row.get("primary_content_hash"),
                "primary_fetch_method": row.get("primary_fetch_method"),
                "url": row.get("url"),
                "discovery_url": row.get("discovery_url"),
                "source": row.get("source"),
                "advert_observed": {
                    "posted_at": row.get("posted_at"),
                    "first_seen": row.get("first_seen"),
                    "discovery_date": row.get("discovery_date"),
                },
            },
            "policy": {
                "profile_name": row.get("profile_name"),
                "role": row.get("role"),
                "policy_version": row.get("policy_version"),
            },
            "location": {
                "ledger": str(ledger),
                "sealed_bundle": (str(self.engine.bundle_path(sealed.run_id))
                                  if sealed is not None else None),
            },
            "caveats": ([] if sealed is not None else [
                "ledger_only: this run left no sealed bundle, so the assessment "
                "is a write-ahead record — its inputs are not independently "
                "verifiable from a seal"
            ]),
        }

    # -- helpers ---------------------------------------------------------

    def _principal(self) -> str:
        return self.engine.state_root.name

    def _inventory(self, run_id: str) -> dict[str, dict]:
        path = self.engine.bundle_path(run_id) / "inventory.jsonl"
        return {json.loads(l)["path"]: json.loads(l)
                for l in jsonl_io.lines(path)}

    def _require_seal(self, run_id: str) -> SealView:
        for v in self.seals():
            if v.run_id == run_id:
                return v
        raise ValueError(f"unknown_run: {run_id} is not a sealed bundle")


# ── CLI ──────────────────────────────────────────────────────────────────


def _render_human(result: dict) -> str:
    import stability
    if "runs" in result:
        lines = [f"{r['sealed_at']}  {r['run_id']}  {r['mode']:<12}"
                 f"  {r['execution']}/{r['capture']}" for r in result["runs"]]
        lines.append(f"({result['total']} total)")
        return "\n".join(lines)
    if result.get("answer") in ("unknown", "sealed_snapshot") and \
            "status" in result:
        lines = [f"principal: {result['principal']}",
                 f"requested: {result['requested_cutoff']}",
                 f"effective seal: {result['effective_seal']} "
                 f"(lag {result['lag_seconds']}s)"]
        if result["answer"] == "unknown":
            lines.append(f"UNKNOWN: {result.get('reason')}")
        else:
            lines.append(f"seal digest: {result['seal_digest']}")
            lines.append(f"status: {result['status']}")
            for c in result["coverage"]:
                lines.append(f"  {c['source']} [{c['scope']}]: {c['outcome']}")
            lines.append(f"ledger rows: {result['artifacts']['ledger_rows']}")
        for w in result.get("warnings", []):
            lines.append(f"⚠ {w}")
        return "\n".join(lines)
    if "comparable" in result:
        lines = [f"{result['from']['run_id']} → {result['to']['run_id']}",
                 f"  verdict: {result['verdict']}"]
        for bucket, label in ((stability.SCOPE, "coverage"),
                              (stability.POLICY, "lens"),
                              (stability.WORLD, "world")):
            items = result["attributed"].get(bucket) or []
            if items:
                lines.append(f"  {label}: {len(items)}")
                for c in items[:5]:
                    lines.append(f"    {c['kind']:<15} {c['org_name']} — "
                                 f"{c['title'][:40]}")
        if result["scope_or_capping"]:
            for c in result["scope_or_capping"]:
                lines.append(f"  coverage change: {c['source']} "
                             f"{c['from']} → {c['to']}")
        for c in result.get("caveats", []):
            lines.append(f"⚠ {c}")
        return "\n".join(lines)
    if "episodes" in result and "seals" in result:
        lines = [f"principal: {result.get('principal', '?')}"
                 f"  ({result.get('counts', {}).get('episodes', 0)} episode(s) "
                 f"from {result['seals']} seal(s))"]
        if result.get("status") == "no_seals":
            lines.append(f"  {result['detail']}")
        for e in result["episodes"][:20]:
            kind = f"{e['state']}/{e['censoring']}"
            span = f"{e['start'][:10]} → {(e['end'] or 'now')[:10]}"
            lines.append(f"  {kind:<16} {span}  {e['org_name']} — "
                         f"{e['title'][:48]}")
        s = result.get("survival")
        if s:
            median = s["median_lower_bound_days"]
            lines.append(f"  survival: n={s['n']} closed={s['n_closed']} "
                         f"open={s['n_open']} median_lower_bound="
                         f"{'—' if median is None else f'{median}d'}")
            for c in s["caveats"]:
                lines.append(f"⚠ {c}")
        return "\n".join(lines)
    if "events" in result and "ledger_rows" in result:
        lines = [f"principal: {result['principal']}  "
                 f"({result['total']} event(s); {result['evidence']})"]
        if result.get("cutoff"):
            c = result["cutoff"]
            lines.append(f"cutoff: {c['requested_cutoff']} → seal "
                         f"{c['effective_seal']} (lag {c['lag_seconds']}s)")
        for e in result["events"]:
            q = e["observed_at"]
            stamp = q["value"] or "undated"
            zone = "" if q["timezone"] in (None, "UTC") else f" [{q['timezone']}]"
            who = e["org_name"] or e["run_id"] or ""
            lines.append(f"  {stamp}{zone}  {e['kind']:<10} {who:<22} "
                         f"{e['summary'][:70]}")
        for w in result.get("warnings", []):
            lines.append(f"⚠ {w}")
        return "\n".join(lines)
    if "events" in result:
        return "\n".join(
            f"{e['sealed_at']}  {e['event']:<15} {e['path']}  {e['sha256'][:19]}"
            for e in result["events"]) or "(no events)"
    if "time_basis" in result:
        d, t, e = result["decision"], result["time_basis"], result["evidence"]
        lines = [f"{result['org_name']} — {result['title']}",
                 f"  decision: {d['decision']} "
                 f"(combined {d['combined_score']}, {d['recommended_action']})",
                 f"  status:   {d['assessment_status']} · lifecycle "
                 f"{d['lifecycle_status']}",
                 f"  policy:   {result['policy']['policy_version']} · "
                 f"{result['policy']['profile_name']}/{result['policy']['role']}"]
        if d.get("reasoning"):
            lines.append(f"  why:      {d['reasoning'][:160]}")
        lines.append("  when:")
        for name in ("observed_at", "primary_fetched_at", "sealed_at"):
            q = t[name]
            if q["value"] is None:
                lines.append(f"    {name:<18} {q['source']}")
            else:
                lines.append(f"    {name:<18} {q['value']} "
                             f"[{q['precision']}, {q['timezone']}]")
        lines.append(f"  evidence: advert {e['advert_hash']} · primary "
                     f"{str(e['primary_content_hash'])[:16]} "
                     f"({e['primary_fetch_method']})")
        lines.append(f"  run:      {result['run_id']}")
        for c in result.get("caveats", []):
            lines.append(f"⚠ {c}")
        return "\n".join(lines)
    if "journal" in result:
        counts: dict[str, int] = {}
        for r in result["journal"]:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        lines = [f"{result['from']} → {result['to']}: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
        for r in result["journal"]:
            if r["kind"] == "registry_transition":
                lines.append(f"  {r['org']} — {r['title']}: "
                             f"{r['from']} → {r['to']} ({r['meaning']})")
        return "\n".join(lines)
    return json.dumps(result, indent=2, sort_keys=True, default=repr)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AsOf readers (WP2a)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("runs"); p.add_argument("--before"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("state"); p.add_argument("--at", required=True)
    p = sub.add_parser("when"); p.add_argument("--path"); p.add_argument("--org")
    p.add_argument("--at", help="only seals that existed by this cutoff")

    p = sub.add_parser("stability")
    p.add_argument("--from", dest="from_run", required=True)
    p.add_argument("--to", dest="to_run", required=True)
    p = sub.add_parser("episodes")
    p.add_argument("--org", help="only this org")
    p.add_argument("--survival", action="store_true",
                   help="add the censoring-aware survival projection")
    p = sub.add_parser("events")
    p.add_argument("--at", help="only events recorded by this cutoff")
    p.add_argument("--kind", choices=["run", "assessment", "outcome"])
    p.add_argument("--org")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("explain")
    p.add_argument("--assessment-id")
    p.add_argument("--run-id")
    p.add_argument("--org")
    p.add_argument("--title")
    p = sub.add_parser("cat"); p.add_argument("--run-id", required=True); p.add_argument("--path", required=True)
    p = sub.add_parser("diff"); p.add_argument("--from", dest="from_run", required=True); p.add_argument("--to", dest="to_run", required=True)
    p = sub.add_parser("verify"); p.add_argument("--run-id"); p.add_argument("--deep", action="store_true")
    p = sub.add_parser("journal"); p.add_argument("--from", dest="from_run", required=True); p.add_argument("--to", dest="to_run", required=True); p.add_argument("--write", action="store_true")

    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config

    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66
    asof = Asof(StrataEngine(state_root))

    try:
        if args.command == "runs":
            result: dict | list = asof.runs(before=args.before, limit=args.limit)
        elif args.command == "state":
            result = asof.state_at(args.at)
        elif args.command == "when":
            result = asof.when(path=args.path, org=args.org, at=args.at)
        elif args.command == "stability":
            import stability

            result = stability.compare(asof.engine, args.from_run, args.to_run)
        elif args.command == "episodes":
            import availability

            result = availability.episodes(asof.engine, org=args.org)
            if args.survival:
                result["survival"] = availability.survival(result)
        elif args.command == "events":
            result = asof.events(at=args.at, kind=args.kind, org=args.org,
                                 limit=args.limit)
        elif args.command == "explain":
            result = asof.explain(assessment_id=args.assessment_id,
                                  run_id=args.run_id, org=args.org,
                                  title=args.title)
        elif args.command == "cat":
            result = asof.cat(args.run_id, args.path)
            if "error" in result:
                print(result["error"], file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(result, default=repr))
            else:
                sys.stdout.buffer.write(result["bytes"])
            return 0
        elif args.command == "diff":
            result = asof.diff(args.from_run, args.to_run)
        elif args.command == "journal":
            rows = asof.journal(args.from_run, args.to_run, write=args.write)
            result = {"from": args.from_run, "to": args.to_run,
                      "journal": rows}
        else:  # verify
            ok = True
            reports = []
            run_ids = [args.run_id] if args.run_id else asof.engine.sealed_run_ids()
            for rid in run_ids:
                r = asof.engine.verify(rid, deep=args.deep)
                ok = ok and r.ok
                reports.append(r.to_dict())
            result = {"ok": ok, "reports": reports}
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=repr))
    else:
        print(_render_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

#!/usr/bin/env python3
"""Synthetic strata fixture — the frozen test corpus for WP1a/WP2a.

Builds a miniature, fully-valid history under a temp root:

* a fake principal live tree (the shapes the inventory grades),
* three sealed bundles:
  - run A ``…-live-0001``  — full run, succeeded; one source complete, one
    source FAILED (dead credential) with an explicit gap,
  - run B ``…-incr-0002``  — incremental run; storage lineage → A,
  - event C ``…-notify-0003`` — the LATE RECEIPT: a child operation for
    A's notification, sealed after B. Its storage lineage is B (dedup
    chain) while its causal parent is A — demonstrating that the two
    lineages are distinct (contract rule 6),
* ``history-index/strata.jsonl`` with one row per sealed manifest.

The builder assembles manifests through ``strata_contract`` types and
validates each one — the fixture is schema-valid by construction, and
tests mutate it to prove validation catches damage. It deliberately does
NOT implement sealing (no rsync, no locking): WP1a's engine must pass its
own gate against this corpus.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import strata_contract as sc

PRINCIPAL = "lj"


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode()
    path.write_bytes(payload)
    return len(payload), _sha256_bytes(payload)


@dataclass
class FixtureResult:
    root: Path
    state_root: Path
    strata_root: Path
    index_path: Path
    manifests: dict[str, sc.Manifest] = field(default_factory=dict)

    def index_rows(self) -> list[dict]:
        return [json.loads(line)
                for line in self.index_path.read_text().strip().splitlines()]


def _fake_live_tree(state_root: Path) -> None:
    _write(state_root / "cache" / "jobs.csv", "id,org\n1,Dex\n")
    _write(state_root / "cache" / "page_dex.md", "# Dex careers\n")
    _write(state_root / "cache" / "last_run_context.json", '{"mode":"live"}')
    _write(state_root / "wiki" / "index.md", "# wiki\n")
    _write(state_root / "wiki" / "orgs" / "dex.md", "# Dex\n")
    _write(state_root / "dataset" / "runs.jsonl", '{"run_id":"a"}\n')
    _write(state_root / "dataset" / "assessments.jsonl", '{"assessment_id":"a1"}\n')
    _write(state_root / "sources" / "gmail" / "LJ-jobs" / "m1.json",
           '{"msg_id":"m1"}')
    _write(state_root / "logs" / "full.log", "run\n")
    # allowlist-rejected shapes (must never enter a bundle)
    _write(state_root / "cache" / "crawl.lock", "pid=1")
    _write(state_root / "cache" / "archive" / "jobs-2026-07.csv", "old")


def build_synthetic_fixture(root: Path) -> FixtureResult:
    state_root = root / "state" / PRINCIPAL
    strata_root = state_root / "strata"
    index_path = strata_root / "history-index" / "strata.jsonl"
    _fake_live_tree(state_root)

    result = FixtureResult(root=root, state_root=state_root,
                           strata_root=strata_root, index_path=index_path)

    # ── Run A: full, succeeded, one complete source, one FAILED source ──
    a_dir = strata_root / "run-lj-live-0001"
    soul_bytes, soul_sha = _write(a_dir / "inputs" / "profile" / "soul.md",
                                  "# soul\nwants: agency\n")
    poss_bytes, poss_sha = _write(a_dir / "inputs" / "profile" / "possibilities.md",
                                  "# possibilities\n(a) agentic systems\n")
    report_bytes, report_sha = _write(
        a_dir / "outputs" / "2026-09-14T1000-lj-full-report.md",
        "# FULL report\n1 GO\n")
    # miniature state copy honoring the allowlist
    _write(a_dir / "state" / "cache" / "jobs.csv", "id,org\n1,Dex\n")
    _write(a_dir / "state" / "dataset" / "assessments.jsonl",
           '{"assessment_id":"a1"}\n')

    manifest_a = sc.Manifest(
        run=sc.RunIdentity(
            run_id="run-lj-live-0001",
            aliases={"wrapper_run_id": "full-lj-20260914-100001-4242",
                     "pipeline_run_id": "aaa111aaa111aaa111aaa111aaa111aa"},
            host="fixture-host", principal=PRINCIPAL, role="general",
            mode="live"),
        clock=sc.ClockWindow(
            started_at="2026-09-14T10:00:01+00:00",
            finished_at="2026-09-14T11:00:00+00:00",
            sealed_at="2026-09-14T11:00:05+00:00",
            timezone="Europe/London",
            observation_from="2026-09-14T10:00:10+00:00",
            observation_to="2026-09-14T10:59:30+00:00",
            input_cutoff="2026-09-14T10:00:01+00:00"),
        status=sc.StatusSet(execution="succeeded", capture="partial",
                            qa="passed", delivery="pending"),
        inputs=[
            sc.InputRef(role="profile:soul", path="inputs/profile/soul.md",
                        origin="profiles/example/soul.md", sha256=soul_sha,
                        bytes=soul_bytes),
            sc.InputRef(role="profile:possibilities",
                        path="inputs/profile/possibilities.md",
                        origin="profiles/example/possibilities.md",
                        sha256=poss_sha, bytes=poss_bytes),
        ],
        coverage=[
            sc.CoverageAttempt(
                source="research_orgs", scope="19 fixed labs",
                attempt_id="att-a1", outcome="complete",
                started_at="2026-09-14T10:00:10+00:00",
                ended_at="2026-09-14T10:59:30+00:00", cache_use="mixed",
                parser_version="orglist-v2"),
            sc.CoverageAttempt(
                source="gmail_lj_jobs", scope="label:LJ-jobs",
                attempt_id="att-a2", outcome="failed",
                started_at="2026-09-14T10:00:11+00:00",
                ended_at="2026-09-14T10:00:12+00:00", cache_use="unknown",
                notes="TOKEN_REVOKED: invalid_grant"),
        ],
        gaps=[sc.Gap(kind="coverage",
                     reason="gmail_lj_jobs credential dead; zero leads this "
                            "run are NOT market evidence",
                     evidence_ref="coverage[1]")],
        state=sc.StateArtifact(
            path="state", file_count=2, logical_bytes=64, copied_bytes=64,
            linked_files=0,
            excluded=[
                {"path": "cache/archive", "reason": "legacy recursive snapshot"},
                {"path": "cache/runs", "reason": "mutable wrapper records"},
            ]),
        outputs=[sc.OutputRef(role="report",
                              path="outputs/2026-09-14T1000-lj-full-report.md",
                              sha256=report_sha, bytes=report_bytes)],
        ledger_copies=[sc.LedgerCopy(
            path="state/dataset/assessments.jsonl",
            sha256=_sha256_bytes(b'{"assessment_id":"a1"}\n'), rows=1)],
        capabilities=sc.Capabilities(state_snapshot=True, recorded_output=True,
                                     observation_history=False,
                                     input_complete=False, reconstructable=False),
    )

    # ── Run B: incremental; storage lineage → A ─────────────────────────
    b_dir = strata_root / "run-lj-incr-0002"
    b_bytes, b_sha = _write(b_dir / "outputs" / "2026-09-14T1500-lj-incr.md",
                            "# INCR report\n")
    manifest_b = sc.Manifest(
        run=sc.RunIdentity(run_id="run-lj-incr-0002", host="fixture-host",
                           principal=PRINCIPAL, role="general",
                           mode="incremental"),
        clock=sc.ClockWindow(
            started_at="2026-09-14T15:00:00+00:00",
            finished_at="2026-09-14T15:12:00+00:00",
            sealed_at="2026-09-14T15:12:01+00:00"),
        status=sc.StatusSet(execution="succeeded", capture="complete",
                            delivery="sent"),
        coverage=[sc.CoverageAttempt(
            source="neolabs", scope="62 orgs", attempt_id="att-b1",
            outcome="complete", cache_use="fresh")],
        state=sc.StateArtifact(path="state", file_count=1, logical_bytes=32,
                               copied_bytes=0, linked_files=1),
        outputs=[sc.OutputRef(role="report",
                              path="outputs/2026-09-14T1500-lj-incr.md",
                              sha256=b_sha, bytes=b_bytes)],
        capabilities=sc.Capabilities(state_snapshot=True, recorded_output=True),
        previous_manifest=sc.SealedRef(run_id=manifest_a.run.run_id,
                                       digest=manifest_a.digest()),
    )

    # ── Event C: the LATE RECEIPT for A's notification (child op) ───────
    # Sealed AFTER B: storage lineage → B, causal parent → A.
    c_dir = strata_root / "evt-lj-notify-0003"
    r_bytes, r_sha = _write(c_dir / "outputs" / "receipt.json",
                            '{"message_id":"1a09fix", "status":"sent"}')
    manifest_c = sc.Manifest(
        run=sc.RunIdentity(
            run_id="evt-lj-notify-0003",
            parent_run_id=manifest_a.run.run_id,
            operation_kind="child", child_of=manifest_a.run.run_id,
            host="fixture-host", principal=PRINCIPAL, role="general",
            mode="attempt"),
        clock=sc.ClockWindow(
            started_at="2026-09-14T15:30:00+00:00",
            finished_at="2026-09-14T15:30:02+00:00",
            sealed_at="2026-09-14T15:30:03+00:00"),
        status=sc.StatusSet(execution="succeeded", capture="complete",
                            qa="not_applicable", delivery="sent"),
        outputs=[sc.OutputRef(role="notification_receipt",
                              path="outputs/receipt.json", sha256=r_sha,
                              bytes=r_bytes)],
        capabilities=sc.Capabilities(recorded_output=True),
        previous_manifest=sc.SealedRef(run_id=manifest_b.run.run_id,
                                       digest=manifest_b.digest()),
    )

    # ── Seal: validate every manifest before writing anything ───────────
    manifests = {"run-lj-live-0001": manifest_a,
                 "run-lj-incr-0002": manifest_b,
                 "evt-lj-notify-0003": manifest_c}
    for run_id, manifest in manifests.items():
        problems = manifest.validate()
        if problems:
            raise ValueError(f"fixture manifest {run_id} invalid: {problems}")

    for run_id, manifest in manifests.items():
        manifest_path = strata_root / run_id / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        sc.canonical_json({
            "run_id": run_id,
            "sealed_at": manifest.clock.sealed_at,
            "mode": manifest.run.mode,
            "operation_kind": manifest.run.operation_kind,
            "status": manifest.status.to_dict(),
            "previous_run_id": (manifest.previous_manifest.run_id
                                if manifest.previous_manifest else None),
            "digest": manifest.digest(),
        }) + "\n"
        for run_id, manifest in manifests.items()
    )
    index_path.write_text(rows)

    # `current` points at the newest sealed generation (C, the last seal).
    current = strata_root / "current"
    tmp = strata_root / "current.tmp"
    os.symlink("evt-lj-notify-0003", tmp)
    os.replace(tmp, current)

    result.manifests = manifests
    return result

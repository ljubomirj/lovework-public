#!/usr/bin/env python3
"""Strata seal engine (WP1a) — begin / capture / seal / verify / recover.

Implements the storage half of chapter 26's sealing protocol against the
contracts frozen in ``strata_contract`` (manifest v1, allowlist,
RunContext). Library milestone only: nothing here is wired into the crawl
wrapper or pipeline — that is WP1b, the writer-boundary integration.

Mechanism (the wdbackup lineage, in-process):

* Each seal writes a **fresh** staging dir ``strata/.partial-<run>-<attempt>/``
  and only copied bytes or links against previous *sealed* bundles ever
  enter it — the live tree is never hard-linked into a bundle.
* Every capturable regular file is digested exactly once. SQLite databases
  are first captured through the backup API and that consistent snapshot is
  then digested. When the resulting digest matches the same path in the
  immediately previous sealed bundle, the staging file is replaced with an
  ``os.link`` to that sealed copy (same filesystem). Different bytes are
  always retained as copies. Linking is therefore content-identified: a
  same-size, mtime-preserving edit can never produce a false link (unlike
  size+mtime quick-checks), and WAL-only changes cannot be missed.
* Per-bundle ``inventory.jsonl`` (path, sha256, bytes, mtime, dev, ino) is
  the integrity substrate: the manifest's ``integrity.file_inventory_digest``
  derives from it and ``verify`` re-checks it. A shared inode means reuse;
  an in-place edit of any linked file corrupts both generations' digests —
  verify says so, because immutability is a writer contract enforced by
  digests, not a property of hard links.
* Publish order: durable inventory+manifest → atomic rename of the whole
  staging dir → append index row → flip ``current``. A bundle is
  discoverable the moment it is renamed; the index is disposable.

Deliberate deviation from chapter 26's rsync wording: this engine shells
out to nothing. macOS ships openrsync with an uncertain flag matrix; the
*shape* of the wdbackup pattern (fresh destination, link-dest from sealed
generations only, content comparison) is preserved and the digests are
needed for verify regardless.

SQLite files (``*.db``, ``*.sqlite*``) are copied through sqlite3's backup
API or skipped with a gap — a raw copy of an active DB/WAL pair is not a
transactional snapshot. WAL/SHM sidecars are never captured.

Library usage::

    from strata import StrataEngine
    engine = StrataEngine(state_root)
    ctx = engine.begin(RunIdentity(run_id=..., principal="lj", mode="live", ...))
    ctx.pin_input("profile:soul", path)
    ctx.record_attempt(source=..., scope=..., outcome=...)
    result = engine.seal(ctx, execution="succeeded", capture="complete")

CLI (thin adapters)::

    ../venv/bin/python3 strata.py --profile lj status
    ../venv/bin/python3 strata.py --profile lj verify [--run-id X] [--deep]
    ../venv/bin/python3 strata.py --profile lj index-rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import strata_contract as sc

INVENTORY_NAME = "inventory.jsonl"
ATTEMPT_NAME = "attempt.json"
INDEX_NAME = "strata.jsonl"

SQLITE_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── helpers ──────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def is_sqlite_db(rel_path: str) -> bool:
    return rel_path.endswith(SQLITE_DB_SUFFIXES)


def is_sqlite_sidecar(rel_path: str) -> bool:
    return rel_path.endswith(SQLITE_SIDECAR_SUFFIXES)


def inventory_digest(entries: list[dict]) -> str:
    """sha256 over the canonical [[path, hex], …] array (plain hex, sorted).

    Matches the definition frozen in strata_manifest.schema.json."""
    pairs = sorted([e["path"], e["sha256"].split(":", 1)[1]] for e in entries)
    canonical = json.dumps(pairs, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


# ── result types ─────────────────────────────────────────────────────────


@dataclass
class SealResult:
    run_id: str
    manifest: sc.Manifest
    bundle_path: Path
    manifest_path: Path
    published: bool
    copied_files: int = 0
    linked_files: int = 0
    copied_bytes: int = 0
    logical_bytes: int = 0
    link_fallbacks: int = 0
    duration_s: float = 0.0


@dataclass
class VerifyReport:
    run_id: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked_files: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "ok": self.ok,
                "problems": self.problems,
                "checked_files": self.checked_files,
                "duration_s": round(self.duration_s, 3)}


# ── the engine ───────────────────────────────────────────────────────────


class StrataEngine:
    """Seals bundles for ONE principal's state root. No locking here: the
    shared writer protocol (WP1b) wraps begin/seal; the engine itself is
    single-operation by construction."""

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root)
        self.strata_root = self.state_root / "strata"
        self.index_dir = self.strata_root / "history-index"
        self.index_path = self.index_dir / INDEX_NAME

    # -- discovery -----------------------------------------------------

    def bundle_path(self, run_id: str) -> Path:
        return self.strata_root / run_id

    def sealed_run_ids(self) -> list[str]:
        """Every sealed bundle on disk, **including operation children**.

        A child (a late receipt, a link recheck) is a sealed bundle with its own
        manifest and clock, so `asof cat` / `verify` / replay legitimately read
        it. It is *not* a result, and it seals after the run it reports on — so a
        reader that asks "what did we last produce?" or "what is the newest
        seal?" must use `result_run_ids()`, not this. The 2026-10-07 defect was
        exactly that mistake in `status.py`: the cockpit described the email
        receipt instead of the crawl.
        """
        return self._bundle_ids()

    def _bundle_ids(self) -> list[str]:
        if not self.strata_root.is_dir():
            return []
        return sorted(
            p.name for p in self.strata_root.iterdir()
            if p.is_dir() and not p.is_symlink()      # `current` is a symlink
            and not p.name.startswith(".")
            and p.name != "history-index"
            and (p / "manifest.json").is_file()
        )

    def result_run_ids(self) -> list[str]:
        """Sealed bundles that are results — child operations excluded."""
        return [r for r in self._bundle_ids()
                if not sc.is_child_manifest(self.read_manifest(r))]

    def child_run_ids(self) -> list[str]:
        """Sealed bundles that are operation children (bookkeeping)."""
        return [r for r in self._bundle_ids()
                if sc.is_child_manifest(self.read_manifest(r))]

    def current_run_id(self) -> str | None:
        current = self.strata_root / "current"
        if not current.is_symlink():
            return None
        run_id = os.readlink(current)
        return run_id if (self.bundle_path(run_id) / "manifest.json").is_file() \
            else None

    def read_manifest(self, run_id: str) -> sc.Manifest | None:
        path = self.bundle_path(run_id) / "manifest.json"
        if not path.is_file():
            return None
        return parse_manifest(json.loads(path.read_text()))

    def previous_generation(self) -> tuple[str, Path, dict[str, dict]] | None:
        """(run_id, bundle_path, {rel_path: inventory_row}) of the latest
        sealed generation — the only legal link source."""
        run_id = self.current_run_id() or (
            self.sealed_run_ids()[-1] if self.sealed_run_ids() else None)
        if not run_id:
            return None
        bundle = self.bundle_path(run_id)
        inv: dict[str, dict] = {}
        inv_path = bundle / INVENTORY_NAME
        if inv_path.is_file():
            for line in inv_path.read_text().splitlines():
                row = json.loads(line)
                inv[row["path"][len("state/"):]] = row
        return run_id, bundle, inv

    # -- lifecycle -----------------------------------------------------

    def staging_dir(self, run_id: str, attempt: str = "a1") -> Path:
        return self.strata_root / f".partial-{run_id}-{attempt}"

    def begin(self, run: sc.RunIdentity, *, attempt: str = "a1",
              started_at: str | None = None, clock=None):
        """Create staging + the durable attempt record. ``clock`` (returning
        UTC ISO strings) is injectable for deterministic tests. Import here
        keeps the contract/engine split import-clean."""
        from run_context import RunContext

        if self.bundle_path(run.run_id).exists():
            raise ValueError(f"duplicate run_id already sealed: {run.run_id}")
        staging = self.staging_dir(run.run_id, attempt)
        if staging.exists():
            raise ValueError(f"staging already exists: {staging}")
        staging.mkdir(parents=True)
        started = started_at or (clock() if clock else utc_now_iso())
        _write_durable(staging / ATTEMPT_NAME, json.dumps({
            "run": run.to_dict(),
            "started_at": started,
            "attempt": attempt,
            "status": "running",
        }, indent=2, sort_keys=True) + "\n")
        return RunContext(self, run, staging, started_at=started,
                          clock=clock)

    # -- capture -------------------------------------------------------

    def capture_state(
        self, staging: Path,
        link_source: tuple[str, Path, dict[str, dict]] | None,
    ) -> tuple[sc.StateArtifact, list[dict], list[sc.Gap], dict]:
        """Allowlisted copy of live state into ``staging/state/``.

        Returns (state artifact, inventory entries, capture gaps, stats).
        """
        gaps: list[sc.Gap] = []
        entries: list[dict] = []
        excluded_counts: dict[str, int] = {}
        copied = linked = copied_bytes = link_fallbacks = 0
        t0 = time.monotonic()
        prev_run, prev_bundle, prev_inv = link_source or (None, None, {})

        def _entry(rel_staging: str, src_or_dst: Path, *, is_link: bool,
                   digest: str | None = None) -> None:
            st = src_or_dst.stat()
            entries.append({
                "path": rel_staging,
                "sha256": digest or sha256_file(src_or_dst),
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                "dev": st.st_dev, "ino": st.st_ino,
                "linked": is_link,
            })

        for tree in sc.INCLUDE_TREES:
            src_tree = self.state_root / tree
            if not src_tree.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(src_tree):
                rel_dir = Path(dirpath).relative_to(self.state_root)
                kept: list[str] = []
                for d in sorted(dirnames):
                    capturable, reason = sc.is_capturable(
                        (rel_dir / d).as_posix())
                    if capturable:
                        kept.append(d)
                    else:
                        # pruned at the directory level; count the files it
                        # shields so the manifest's exclusions stay honest
                        n = sum(1 for p in (Path(dirpath) / d).rglob("*")
                                if p.is_file() and not p.is_symlink())
                        excluded_counts[reason] = \
                            excluded_counts.get(reason, 0) + n
                dirnames[:] = kept
                for name in sorted(filenames):
                    rel = (rel_dir / name).as_posix()
                    capturable, reason = sc.is_capturable(rel)
                    if not capturable:
                        excluded_counts[reason] = \
                            excluded_counts.get(reason, 0) + 1
                        continue
                    src = Path(dirpath) / name
                    if src.is_symlink() or not src.is_file():
                        continue
                    if is_sqlite_sidecar(rel):
                        continue
                    dst = staging / "state" / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)

                    made_link = False
                    if is_sqlite_db(rel):
                        # The live main file is not a database snapshot when
                        # WAL pages are pending. Capture first, then identify
                        # the captured bytes. Comparing the live-file digest
                        # here can both record the wrong inventory digest and
                        # falsely reuse a prior generation after a WAL-only
                        # logical change.
                        if not self._copy_sqlite_consistent(src, dst):
                            gaps.append(sc.Gap(
                                kind="capture",
                                reason=f"active sqlite not consistently "
                                       f"backupable: {rel}"))
                            continue
                        digest = sha256_file(dst)
                        if prev_inv.get(rel, {}).get("sha256") == digest:
                            link_candidate = dst.with_name(
                                f".{dst.name}.link-candidate")
                            try:
                                os.link(prev_bundle / "state" / rel,
                                        link_candidate)
                                os.replace(link_candidate, dst)
                                made_link = True
                                linked += 1
                            except OSError:
                                link_fallbacks += 1
                                try:
                                    link_candidate.unlink(missing_ok=True)
                                except OSError:
                                    pass
                    else:
                        digest = sha256_file(src)
                        if prev_inv.get(rel, {}).get("sha256") == digest:
                            try:
                                os.link(prev_bundle / "state" / rel, dst)
                                made_link = True
                                linked += 1
                            except OSError:
                                link_fallbacks += 1
                        if not made_link:
                            shutil.copy2(src, dst)
                    if not made_link:
                        copied += 1
                        copied_bytes += dst.stat().st_size
                    _entry(f"state/{rel}", dst, is_link=made_link,
                           digest=digest)

        # pinned inputs and any pre-registered artifacts join the inventory
        for sub in ("inputs", "outputs", "events"):
            sub_path = staging / sub
            if not sub_path.is_dir():
                continue
            for p in sorted(sub_path.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    _entry(p.relative_to(staging).as_posix(), p, is_link=False)

        excluded = [
            {"path": _rule_path_for(reason),
             "reason": f"{reason} ({count} files)"}
            for reason, count in sorted(excluded_counts.items())
        ] or [{"path": "(none)", "reason": "(none excluded)"}]

        state_files = [e for e in entries if e["path"].startswith("state/")]
        artifact = sc.StateArtifact(
            path="state",
            file_count=len(state_files),
            logical_bytes=sum(e["bytes"] for e in entries),
            copied_bytes=copied_bytes,
            linked_files=linked,
            excluded=excluded,
        )
        stats = {"copied_files": copied, "linked_files": linked,
                 "copied_bytes": copied_bytes,
                 "link_fallbacks": link_fallbacks,
                 "duration_s": time.monotonic() - t0,
                 "previous_run_id": prev_run}
        return artifact, entries, gaps, stats

    def _copy_sqlite_consistent(self, src: Path, dst: Path) -> bool:
        """sqlite3 backup API: src → dst. (Connection.backup(target) copies
        *self* into target — the direction matters; getting it backwards
        would overwrite the live database.)"""
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src_conn = sqlite3.connect(str(src))
            out = sqlite3.connect(str(dst))
            try:
                src_conn.backup(out)
            finally:
                out.close()
                src_conn.close()
            return True
        except sqlite3.Error:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # -- seal ----------------------------------------------------------

    def seal(
        self, context, *, execution: str, capture: str,
        qa: str = "unknown", delivery: str = "not_applicable",
        previous: sc.SealedRef | None = None,
        owner_manifests: list[sc.SealedRef] | None = None,
        input_complete: bool = False,
    ) -> SealResult:
        """Capture, seal and atomically publish. Called exactly once per
        context (by the outer operation)."""
        if getattr(context, "_sealed", False):
            raise RuntimeError("context already sealed")
        t0 = time.monotonic()
        # A child operation (late receipt, recheck, heal) captures its own
        # artifacts only — the contract says so, and the state it would copy is
        # the *parent's*, already sealed and not to be reopened
        # (strata_contract §"child operation captures its own artifacts only").
        is_child = getattr(context.run, "operation_kind", "outer") == "child"
        if is_child:
            artifact, entries, capture_gaps = None, [], []
            stats = {"copied_files": 0, "linked_files": 0, "copied_bytes": 0,
                     "link_fallbacks": 0}
            link_source = None
        else:
            link_source = self.previous_generation()
            artifact, entries, capture_gaps, stats = self.capture_state(
                context.staging, link_source)

        ledger_copies = self._ledger_copies(context.staging, entries)
        manifest = sc.Manifest(
            run=context.run,
            clock=sc.ClockWindow(
                started_at=context.started_at,
                finished_at=context.now(),
                sealed_at=context.now(),
                timezone=getattr(context, "timezone", "UTC"),
                observation_from=getattr(context, "observation_from", None),
                observation_to=getattr(context, "observation_to", None),
                input_cutoff=getattr(context, "input_cutoff", None),
            ),
            status=sc.StatusSet(execution=execution, capture=capture,
                                qa=qa, delivery=delivery),
            previous_manifest=previous or (
                sc.SealedRef(run_id=link_source[0],
                             digest=self.read_manifest(link_source[0]).digest())
                if link_source else None),
            owner_manifests=owner_manifests or [],
            inputs=list(context.inputs),
            coverage=list(context.coverage),
            gaps=list(context.gaps) + capture_gaps,
            state=artifact,
            outputs=list(context.outputs),
            events=list(context.events),
            ledger_copies=ledger_copies,
            capabilities=sc.Capabilities(
                state_snapshot=not is_child,
                recorded_output=any(o.original for o in context.outputs),
                observation_history=False,
                input_complete=input_complete,
                reconstructable=False,
            ),
            file_inventory_count=len(entries),
            file_inventory_digest=inventory_digest(entries),
        )
        problems = manifest.validate()
        if problems:
            raise ValueError(f"manifest invalid before publish: {problems}")

        bundle = self._publish(context.staging, context.run.run_id,
                               manifest, entries)
        context._sealed = True
        return SealResult(
            run_id=context.run.run_id, manifest=manifest,
            bundle_path=bundle, manifest_path=bundle / "manifest.json",
            published=True,
            copied_files=stats["copied_files"],
            linked_files=stats["linked_files"],
            copied_bytes=stats["copied_bytes"],
            logical_bytes=(artifact.logical_bytes if artifact else 0),
            link_fallbacks=stats["link_fallbacks"],
            duration_s=time.monotonic() - t0,
        )

    def _ledger_copies(self, staging: Path,
                       entries: list[dict]) -> list[sc.LedgerCopy]:
        copies = []
        by_path = {e["path"]: e for e in entries}
        for name in ("runs.jsonl", "assessments.jsonl", "outcomes.jsonl"):
            rel = f"state/dataset/{name}"
            entry = by_path.get(rel)
            if not entry:
                continue
            rows = sum(1 for _ in (staging / rel).open("rb"))
            copies.append(sc.LedgerCopy(path=rel, sha256=entry["sha256"],
                                        rows=rows))
        return copies

    def _publish(self, staging: Path, run_id: str, manifest: sc.Manifest,
                 entries: list[dict]) -> Path:
        """Durable inventory+manifest → atomic rename → index → current."""
        staging.mkdir(parents=True, exist_ok=True)
        inv_text = "".join(
            json.dumps(e, sort_keys=True) + "\n" for e in entries)
        _write_durable(staging / INVENTORY_NAME, inv_text)
        _write_durable(staging / "manifest.json",
                       json.dumps(manifest.to_dict(), indent=2,
                                  sort_keys=True) + "\n")
        _fsync_dir(staging)

        bundle = self.bundle_path(run_id)
        if bundle.exists():
            raise ValueError(f"duplicate run_id already sealed: {run_id}")
        os.rename(staging, bundle)
        _fsync_dir(self.strata_root)

        self._append_index(index_row(manifest))
        self._flip_current(run_id)
        return bundle

    def _append_index(self, row: dict) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as fh:
            fh.write(sc.canonical_json(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _flip_current(self, run_id: str) -> None:
        tmp = self.strata_root / "current.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(run_id, tmp)
        os.replace(tmp, self.strata_root / "current")

    def rebuild_index(self) -> int:
        """The index is disposable: rebuild from sealed bundles, ordered by
        seal time (publication order), run_id as the tiebreaker."""
        rows = []
        for run_id in self.sealed_run_ids():
            manifest = self.read_manifest(run_id)
            if manifest is not None:
                rows.append(index_row(manifest))
        rows.sort(key=lambda r: (r["sealed_at"], r["run_id"]))
        self.index_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".jsonl.tmp")
        _write_durable(tmp, "".join(
            sc.canonical_json(r) + "\n" for r in rows))
        os.replace(tmp, self.index_path)
        return len(rows)

    # -- verify ----------------------------------------------------------

    def verify(self, run_id: str, *, deep: bool = False) -> VerifyReport:
        t0 = time.monotonic()
        problems: list[str] = []
        checked = 0
        bundle = self.bundle_path(run_id)

        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file():
            return VerifyReport(run_id, False,
                                ["manifest.json missing (not a sealed bundle)"])
        doc = json.loads(manifest_path.read_text())
        problems += sc.jsonschema_problems(doc)
        if doc.get("schema_version") != sc.SCHEMA_VERSION:
            problems.append(
                f"unsupported schema_version: {doc.get('schema_version')} "
                f"(reader supports {sc.SCHEMA_VERSION})")
        try:
            manifest = parse_manifest(doc)
            problems += manifest.validate()
        except (TypeError, KeyError) as exc:
            return VerifyReport(run_id, False,
                                [f"unparseable manifest: {exc}"],
                                duration_s=time.monotonic() - t0)

        # index cross-check
        if self.index_path.is_file():
            rows = [json.loads(line)
                    for line in self.index_path.read_text().splitlines()]
            row = next((r for r in rows if r["run_id"] == run_id), None)
            if row is None:
                problems.append("missing index entry (run index-rebuild)")
            elif row["digest"] != manifest.digest():
                problems.append("index digest mismatch: manifest changed "
                                "after publication?")
        else:
            problems.append("index missing (run index-rebuild)")

        # inventory cross-check
        inv_path = bundle / INVENTORY_NAME
        if not inv_path.is_file():
            problems.append(f"{INVENTORY_NAME} missing")
            entries = []
        else:
            entries = [json.loads(line)
                       for line in inv_path.read_text().splitlines()]
        if len(entries) != manifest.file_inventory_count:
            problems.append(
                f"inventory count {len(entries)} != manifest "
                f"{manifest.file_inventory_count}")
        if entries and inventory_digest(entries) != manifest.file_inventory_digest:
            problems.append("inventory digest mismatch: files changed after "
                            "seal, or inventory edited")

        if deep:
            for entry in entries:
                rel = entry["path"]
                if not sc.confined(rel):
                    problems.append(f"unconfined inventory path: {rel}")
                    continue
                p = bundle / rel
                if not p.is_file():
                    problems.append(f"missing file: {rel}")
                    continue
                checked += 1
                if sha256_file(p) != entry["sha256"]:
                    problems.append(f"digest mismatch: {rel}")

        return VerifyReport(run_id, not problems, problems, checked,
                            time.monotonic() - t0)

    # -- recover ---------------------------------------------------------

    def recover(self, staging: Path, *, note: str | None = None) -> SealResult:
        """Seal an interrupted operation's already-durable staging into a
        recovered bundle. Original recording times are retained; the seal
        time is the recovery time."""
        staging = Path(staging)
        attempt_path = staging / ATTEMPT_NAME
        if not attempt_path.is_file():
            raise ValueError(f"no attempt record in {staging}")
        record = json.loads(attempt_path.read_text())
        run = sc.RunIdentity(**record["run"])
        t0 = time.monotonic()

        manifest = self.read_recovered_manifest(staging) or self._build_recovered_manifest(
            staging, run, record, note)
        problems = manifest.validate()
        if problems:
            raise ValueError(f"recovered manifest invalid: {problems}")

        entries = self._recover_inventory(staging)
        bundle = self._publish(staging, run.run_id, manifest, entries)
        return SealResult(
            run_id=run.run_id, manifest=manifest, bundle_path=bundle,
            manifest_path=bundle / "manifest.json", published=True,
            logical_bytes=manifest.state.logical_bytes if manifest.state
            else 0,
            duration_s=time.monotonic() - t0,
        )

    def read_recovered_manifest(self, staging: Path) -> sc.Manifest | None:
        path = staging / "manifest.json"
        if not path.is_file():
            return None
        try:
            manifest = parse_manifest(json.loads(path.read_text()))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        manifest.status.execution = "recovered"
        manifest.clock.sealed_at = utc_now_iso()
        return manifest

    def _build_recovered_manifest(self, staging: Path, run: sc.RunIdentity,
                                  record: dict,
                                  note: str | None) -> sc.Manifest:
        gaps = [sc.Gap(kind="capture",
                       reason=note or "operation interrupted before seal; "
                                      "bundle contains durable staging only")]
        state = None
        if (staging / "state").is_dir():
            state = sc.StateArtifact(
                path="state", file_count=0, logical_bytes=0,
                copied_bytes=0, linked_files=0,
                excluded=[{"path": "(unknown)",
                           "reason": "recovered before accounting"}])
        return sc.Manifest(
            run=run,
            clock=sc.ClockWindow(
                started_at=record["started_at"], sealed_at=utc_now_iso(),
                finished_at=None),
            # capture honesty: no state/ tree captured means capture="none"
            status=sc.StatusSet(execution="recovered",
                                capture="partial" if state else "none"),
            gaps=gaps,
            state=state,
            capabilities=sc.Capabilities(
                state_snapshot=state is not None),
        )

    def _recover_inventory(self, staging: Path) -> list[dict]:
        inv_path = staging / INVENTORY_NAME
        if inv_path.is_file():
            return [json.loads(line)
                    for line in inv_path.read_text().splitlines()]
        entries = []
        for p in sorted(staging.rglob("*")):
            if p.is_file() and not p.is_symlink() \
                    and p.name not in (ATTEMPT_NAME, "manifest.json"):
                st = p.stat()
                entries.append({
                    "path": p.relative_to(staging).as_posix(),
                    "sha256": sha256_file(p),
                    "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                    "dev": st.st_dev, "ino": st.st_ino, "linked": False,
                })
        return entries


# ── manifest parse + index row ───────────────────────────────────────────


def parse_manifest(doc: dict) -> sc.Manifest:
    """Inverse of Manifest.to_dict() for persisted manifests."""
    clock_doc = doc["clock"]
    lineage = doc["lineage"]
    artifacts = doc["artifacts"]
    return sc.Manifest(
        run=sc.RunIdentity(**doc["run"]),
        clock=sc.ClockWindow(
            started_at=clock_doc["started_at"],
            finished_at=clock_doc["finished_at"],
            sealed_at=clock_doc["sealed_at"],
            timezone=clock_doc["timezone"],
            observation_from=clock_doc["observation_window"]["from"],
            observation_to=clock_doc["observation_window"]["to"],
            input_cutoff=clock_doc["input_cutoff"],
        ),
        status=sc.StatusSet(**doc["status"]),
        previous_manifest=(sc.SealedRef(**lineage["previous_manifest"])
                           if lineage["previous_manifest"] else None),
        owner_manifests=[sc.SealedRef(**o) for o in lineage["owner_manifests"]],
        inputs=[sc.InputRef(**i) for i in doc["inputs"]],
        coverage=[sc.CoverageAttempt(**c) for c in doc["coverage"]],
        gaps=[sc.Gap(**g) for g in doc["gaps"]],
        state=sc.StateArtifact(**artifacts["state"]),
        outputs=[sc.OutputRef(**o) for o in artifacts["outputs"]],
        events=[dict(e) for e in artifacts["events"]],
        ledger_copies=[sc.LedgerCopy(**l) for l in artifacts["ledger_copies"]],
        capabilities=sc.Capabilities(**doc["capabilities"]),
        file_inventory_count=doc["integrity"]["file_inventory_count"],
        file_inventory_digest=doc["integrity"]["file_inventory_digest"],
    )


def index_row(manifest: sc.Manifest) -> dict:
    return {
        "run_id": manifest.run.run_id,
        "sealed_at": manifest.clock.sealed_at,
        "mode": manifest.run.mode,
        "operation_kind": manifest.run.operation_kind,
        "status": manifest.status.to_dict(),
        "previous_run_id": (manifest.previous_manifest.run_id
                            if manifest.previous_manifest else None),
        "digest": manifest.digest(),
    }


_EXCLUDED_RULE_PATHS: dict[str, str] = {
    "legacy recursive snapshot; inventoried separately as paleo-strata":
        "cache/archive",
    "transient process state": "cache/crawl.lock",
    "mutable wrapper records; their facts enter the manifest instead":
        "cache/runs",
    "platform noise": ".DS_Store",
    "build artifact": "__pycache__ / *.pyc",
    "staging": "*.partial / *.tmp",
    "credential-like path": "credential-like paths",
    "outside include trees": "(outside include trees)",
}


def _age_minutes(path: Path) -> str:
    """Human age of a path's mtime, coarse on purpose (this is a status line)."""
    try:
        seconds = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return "?"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _attempt_rows(strata_root: Path) -> list[dict]:
    """Boundary attempt records, newest first. Unreadable ones still count."""
    rows: list[dict] = []
    attempts_dir = strata_root / "attempts"
    if not attempts_dir.is_dir():
        return rows
    paths = [p for p in attempts_dir.glob("*.json") if p.is_file()]
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rows.append({"status": "unreadable", "attempt_id": path.stem})
    return rows


def _print_status(engine: StrataEngine, *, rows_shown: int = 5) -> int:
    """Report sealed bundles **and the negative space around them**.

    This used to print one line per sealed run and *nothing at all* when there
    were none — which is exactly the shape of a live crawl: an attempt record,
    staging, and no bundle yet. Silence read as "no history" when the truth was
    "operation in flight". Negative space is data (D21 P5): staging and attempt
    rows are reported, with ages, so one command answers "what is happening".
    """
    sealed = engine.sealed_run_ids()
    if sealed:
        current = engine.current_run_id()
        print(f"sealed: {len(sealed)}  current -> {current or '(none)'}")
        for rid in sealed:
            m = engine.read_manifest(rid)
            print(f"  {rid}  {m.clock.sealed_at}  {m.run.mode:<12}  "
                  f"{m.status.execution}/{m.status.capture}")
    else:
        print("sealed: 0  (no bundles yet)")

    strata_root = engine.strata_root
    staging: list[Path] = []
    if strata_root.is_dir():
        staging = sorted(
            (p for p in strata_root.glob(".partial-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True)
    if staging:
        print(f"staging: {len(staging)}  (unpublished — interrupted or in flight)")
        for path in staging[:rows_shown]:
            inner = path / ATTEMPT_NAME
            note = ""
            if inner.is_file():
                try:
                    note = str(json.loads(inner.read_text(encoding="utf-8"))
                               .get("status", ""))
                except (OSError, ValueError):
                    note = "unreadable"
            print(f"  {path.name}  age={_age_minutes(path)}  {note}".rstrip())
    else:
        print("staging: 0")

    rows = _attempt_rows(strata_root)
    if rows:
        print(f"attempts: {len(rows)}  (newest first)")
        for row in rows[:rows_shown]:
            run = row.get("run") or {}
            label = run.get("run_id") or row.get("attempt_id", "?")
            when = row.get("started_at") or row.get("acquired_at") or "?"
            print(f"  {str(row.get('status', '?')):<12} {label}  {when}")
    else:
        print("attempts: 0")
    return 0


def _rule_path_for(reason: str) -> str:
    return _EXCLUDED_RULE_PATHS.get(reason, "(rule)")


# ── CLI (thin adapters) ──────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strata engine CLI (WP1a)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("command",
                        choices=["status", "verify", "index-rebuild", "recover"])
    parser.add_argument("--run-id")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--staging", help="staging dir to recover (command: recover)")
    parser.add_argument("--note", help="why this attempt is being recovered")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config

    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66
    engine = StrataEngine(state_root)

    if args.command == "status":
        return _print_status(engine)

    if args.command == "verify":
        run_ids = [args.run_id] if args.run_id else engine.sealed_run_ids()
        ok = True
        for rid in run_ids:
            report = engine.verify(rid, deep=args.deep)
            ok = ok and report.ok
            print(json.dumps(report.to_dict(), sort_keys=True))
        return 0 if ok else 1

    if args.command == "recover":
        # The engine could always do this; the shell could not reach it, so an
        # interrupted operation's durable staging had no resolution but manual
        # deletion — which throws away evidence instead of sealing it.
        if not args.staging:
            print("recover needs --staging <dir>", file=sys.stderr)
            return 64
        try:
            result = engine.recover(Path(args.staging), note=args.note)
        except ValueError as e:
            print(f"cannot recover: {e}", file=sys.stderr)
            return 65
        print(json.dumps({"run_id": result.run_id, "published": result.published,
                          "bundle": str(result.bundle_path),
                          "logical_bytes": result.logical_bytes}, sort_keys=True))
        return 0

    if args.command == "index-rebuild":
        count = engine.rebuild_index()
        print(f"index rebuilt: {count} rows -> {engine.index_path}")
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(_main())

#!/usr/bin/env python3
"""WP0 — provenance-graded inventory of a principal's state (chapter 28).

Read-only walk of ``state/<principal>/{cache,wiki,dataset,sources,logs}``
that emits one JSONL row per file with a *provenance grade*:

- ``live-mutable``  — the running system may overwrite/edit it in place.
- ``append-only``   — new rows/files are added; existing ones are never
                      rewritten (dataset ledgers, Gmail raw archive).
- ``frozen-legacy`` — historical snapshot copies no writer touches anymore
                      (``cache/archive/`` paleo-strata).
- ``unknown-time``  — no grading rule matched. This is a *finding*: the
                      path should gain a rule or be excluded deliberately.

The inventory is the P0 boundary artifact (docs/28-asof-implementation-plan
§WP0): it fixes what evidence exists *today*, with digests, before the WP1
seal engine starts. It never fetches, moves or modifies walked files.

Output (atomic tmp+rename):

- ``state/<p>/strata/history-index/inventory.jsonl``  — one row per file
- ``state/<p>/strata/history-index/inventory-summary.json`` — counts/bytes

Rows are path-sorted for stable diffs between invocations. ``dev``/``ino``
are recorded as the WP1 linkage baseline (same ``(dev, ino)`` across strata
means hard-link reuse). Row ``mtime`` is UTC ISO from the filesystem; a
``time_anomaly`` flag marks implausible mtimes (future-dated, pre-2000).

Usage::

    ../venv/bin/python3 history_inventory.py --profile lj
    ../venv/bin/python3 history_inventory.py --profile all --no-digests
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
# v2: added consume_state / dataset_doc / linkedin_seeds / linkedin_auth_log
# rules (the four unknown-time gaps found by the first full run, 2026-09-14).
# v3: added time_basis (what a file's mtime can honestly claim), mid-walk
# change detection (digest + re-stat + one retry) and unreadable counting —
# the WP0 provenance-fields remainder.
GRADER_VERSION = 3

# The walked trees. Everything else at state/<p>/ is reported as unwalked
# (applications → external symlink root; strata → the history layer itself).
WALKED_TREES = ("cache", "wiki", "dataset", "sources", "logs")

# Never inventoried: process state, platform noise, editor droppings.
EXCLUDED_NAMES = {"crawl.lock", ".DS_Store", "__pycache__"}
EXCLUDED_SUFFIXES = (".lock", ".pyc", ".partial", ".swp", "~")

# mtime sanity window for time_anomaly (files cannot be from the future or
# predate the project's era by decades).
MTIME_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE_SKEW_SECONDS = 300

DIGEST_CHUNK = 1 << 20
DEFAULT_MAX_DIGEST_BYTES = 256 * 1024 * 1024

# macOS file-provider eviction: a stub keeps its size but has no data blocks
# on disk (st_blocks == 0). Reading it can block indefinitely while the OS
# waits for the cloud provider. 2026-09-14 finding: 5,605 of 18,342 files in
# the host-b state copy were such stubs — never read them blindly.
def _is_evicted_stub(st: os.stat_result) -> bool:
    return st.st_size > 0 and st.st_blocks == 0

# (kind, grade) by relative path. First match wins; rules are ordered so the
# most specific paths grade before directory-wide fallbacks.
RULES: tuple[tuple[str, tuple[str, str]], ...] = (
    # frozen legacy — paleo-strata and archived reports
    ("cache/archive/", ("legacy_snapshot", "frozen-legacy")),
    ("wiki/reports/archive/", ("legacy_report", "frozen-legacy")),
    # append-only — dataset ledgers and the Gmail raw archive
    ("dataset/runs.jsonl", ("ledger_runs", "append-only")),
    ("dataset/assessments.jsonl", ("ledger_assessments", "append-only")),
    ("dataset/outcomes.jsonl", ("ledger_outcomes", "append-only")),
    ("dataset/profiles/", ("profile_snapshot", "append-only")),
    ("sources/gmail/", ("gmail_archive", "append-only")),
    # live-mutable — working state
    ("cache/jobs.csv", ("registry_csv", "live-mutable")),
    ("cache/last_run_context.json", ("run_context", "live-mutable")),
    ("cache/_sandbox_vars.json", ("sandbox_vars", "live-mutable")),
    ("cache/assessments.jsonl", ("assessment_cache_flat", "live-mutable")),
    ("cache/assessments/", ("assessment_cache", "live-mutable")),
    ("cache/enrichment/", ("enrichment", "live-mutable")),
    ("cache/link_checks/", ("link_check", "live-mutable")),
    ("cache/incidents/", ("incident", "live-mutable")),
    ("cache/jackandjill/", ("jnj_state", "live-mutable")),
    ("cache/search/", ("search_state", "live-mutable")),
    ("cache/last_consume.json", ("consume_state", "live-mutable")),
    ("cache/runs/", ("run_record", "live-mutable")),
    ("dataset/README.md", ("dataset_doc", "live-mutable")),
    # The manual-case index (`manual_projects.write_index`): a cache rebuilt by
    # `--write`, never a ledger. Graded, so it is not reported as an
    # unknown-time gap the day after it first appears.
    ("dataset/manual_index.json", ("manual_case_index", "live-mutable")),
    ("sources/linkedin_seeds.md", ("linkedin_seeds", "live-mutable")),
    ("sources/linkedin_needs_auth.md", ("linkedin_auth_log", "live-mutable")),
    ("wiki/reports/", ("report", "live-mutable")),
    ("wiki/orgs/", ("wiki_org", "live-mutable")),
    ("wiki/index.md", ("wiki_index", "live-mutable")),
    ("wiki/sources.md", ("wiki_sources", "live-mutable")),
    ("logs/", ("log", "live-mutable")),
)

_PAGE_PREFIX = "page_"


def classify(rel_path: str) -> tuple[str, str]:
    """Return ``(kind, grade)`` for a path relative to ``state/<p>/``.

    Unmatched paths grade ``unknown-time`` — an explicit inventory gap,
    not a guess.
    """
    posix = rel_path.replace(os.sep, "/")
    name = posix.rsplit("/", 1)[-1]
    if posix.startswith("cache/") and name.startswith(_PAGE_PREFIX):
        return "page_cache", "live-mutable"
    for prefix, kind_grade in RULES:
        if posix.startswith(prefix):
            return kind_grade
    return "other", "unknown-time"


# What an mtime can honestly claim per grade (WP0 provenance contract):
# live/append trees — the last write to this file; legacy copies — rsync -a
# may have preserved a source mtime, so the timestamp is not trustworthy as
# "when this copy was made"; unknown — no claim at all.
TIME_BASIS_BY_GRADE = {
    "live-mutable": "last_write_mtime",
    "append-only": "last_write_mtime",
    "frozen-legacy": "possibly_preserved_mtime",
    "unknown-time": "unknown",
}


def sha256_file(path: Path, max_bytes: int | None = None) -> tuple[str | None, bool]:
    """Digest a file; ``(None, skipped)`` when oversize."""
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        return None, True
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(DIGEST_CHUNK):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", False


def _mtime_is_anomalous(mtime: float, now: float) -> bool:
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt < MTIME_FLOOR or mtime > now + FUTURE_SKEW_SECONDS


def _digest_stable(
    path: Path, max_bytes: int | None = DEFAULT_MAX_DIGEST_BYTES
) -> tuple[str | None, bool, bool]:
    """Digest with mid-walk change detection.

    Returns ``(digest, changed_during_inventory, unreadable)``. The file is
    stat'd before and after digesting; a change triggers exactly one retry.
    A digest is only returned for bytes that were stable across the read —
    the inventory never claims a stable digest of a moving file.
    """
    try:
        def _fingerprint() -> tuple[int, int]:
            st = path.stat()
            return st.st_size, st.st_mtime_ns

        before = _fingerprint()
        digest, oversize = sha256_file(path, max_bytes)
        mid = _fingerprint()
        if before == mid and not oversize:
            return digest, False, False
        if before == mid and oversize:
            return None, False, False
        # changed between stat and read end: retry once
        digest, oversize = sha256_file(path, max_bytes)
        after = _fingerprint()
        if mid != after:
            return None, True, False          # still moving — no digest claimed
        if oversize:
            return None, True, False
        return digest, True, False
    except OSError:
        return None, False, True


def iter_rows(
    state_root: Path,
    *,
    digests: bool = True,
    max_digest_bytes: int | None = DEFAULT_MAX_DIGEST_BYTES,
    now: float | None = None,
) -> Iterator[dict]:
    """Yield one inventory row per walked file, path-sorted per tree."""
    now = now if now is not None else time.time()
    for tree in WALKED_TREES:
        tree_root = state_root / tree
        if not tree_root.is_dir():
            continue
        entries = sorted(
            (p for p in tree_root.rglob("*")
             if p.is_file() and not p.is_symlink()),
            key=lambda p: p.as_posix(),
        )
        for path in entries:
            name = path.name
            if name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES):
                continue
            rel = path.relative_to(state_root).as_posix()
            kind, grade = classify(rel)
            st = path.stat()
            digest, skip_reason = None, None
            changed = unreadable = False
            if _is_evicted_stub(st):
                skip_reason = "evicted"
            elif digests:
                digest, changed, unreadable = _digest_stable(
                    path, max_digest_bytes)
                if unreadable:
                    skip_reason = "unreadable"
                elif digest is None and changed:
                    skip_reason = "changed"
                elif digest is None:
                    skip_reason = "oversize"
            yield {
                "schema_version": SCHEMA_VERSION,
                "path": rel,
                "kind": kind,
                "grade": grade,
                "time_basis": TIME_BASIS_BY_GRADE[grade],
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                "time_anomaly": _mtime_is_anomalous(st.st_mtime, now),
                "sha256": digest,
                "digest_skip_reason": skip_reason,
                "data_evicted": skip_reason == "evicted",
                "changed_during_inventory": changed,
                "unreadable": unreadable,
                "dev": st.st_dev,
                "ino": st.st_ino,
                "grader_version": GRADER_VERSION,
            }


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def build_summary(
    principal: str,
    state_root: Path,
    rows: list[dict],
    skipped_symlinks: int,
    unwalked: list[str],
    generated_at: str,
    root: Path,
) -> dict:
    by_grade: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for row in rows:
        by_grade[row["grade"]] = by_grade.get(row["grade"], 0) + 1
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    anomalies = sum(1 for r in rows if r["time_anomaly"])
    evicted = sum(1 for r in rows if r["data_evicted"])
    changed = sum(1 for r in rows if r["changed_during_inventory"])
    unreadable = sum(1 for r in rows if r["unreadable"])
    return {
        "schema_version": SCHEMA_VERSION,
        "grader_version": GRADER_VERSION,
        "principal": principal,
        "state_root": str(state_root),
        "generated_at": generated_at,
        "host": os.uname().nodename,
        "git_commit": _git_commit(root),
        "walked_trees": list(WALKED_TREES),
        "files": len(rows),
        "bytes": sum(r["bytes"] for r in rows),
        "digested": sum(1 for r in rows if r["sha256"]),
        "time_anomalies": anomalies,
        "data_evicted": evicted,
        "changed_during_inventory": changed,
        "unreadable": unreadable,
        "skipped_symlinks": skipped_symlinks,
        "unwalked_top_level": unwalked,
        "by_grade": dict(sorted(by_grade.items())),
        "by_kind": dict(sorted(by_kind.items())),
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def inventory_principal(
    principal: str,
    state_root: Path,
    *,
    digests: bool = True,
    max_digest_bytes: int | None = DEFAULT_MAX_DIGEST_BYTES,
    out_dir: Path | None = None,
    root: Path | None = None,
) -> dict:
    """Walk one principal's state and write inventory + summary. Read-only
    for everything except ``strata/history-index/`` outputs."""
    root = root if root is not None else state_root.parent.parent
    out_dir = out_dir if out_dir is not None else (
        state_root / "strata" / "history-index"
    )

    unwalked = sorted(
        p.name for p in state_root.iterdir()
        if p.name not in WALKED_TREES
    )
    skipped_symlinks = sum(
        1 for tree in WALKED_TREES
        if (state_root / tree).is_dir()
        for p in (state_root / tree).rglob("*")
        if p.is_symlink()
    )

    rows = list(iter_rows(
        state_root, digests=digests, max_digest_bytes=max_digest_bytes,
    ))
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    _atomic_write(out_dir / "inventory.jsonl", lines)

    summary = build_summary(
        principal, state_root, rows, skipped_symlinks, unwalked,
        generated_at, root,
    )
    _atomic_write(
        out_dir / "inventory-summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    return summary


def _print_summary(summary: dict) -> None:
    print(
        f"{summary['principal']}: {summary['files']} files, "
        f"{summary['bytes']:,} bytes, {summary['digested']} digested, "
        f"{summary['time_anomalies']} time anomalies"
    )
    print(f"  grades: {summary['by_grade']}")
    if summary["data_evicted"]:
        print(
            f"  ⚠ {summary['data_evicted']} files are eviction stubs "
            f"(data not local; not digested — not evidence in hand)"
        )
    if summary["changed_during_inventory"]:
        print(
            f"  ⚠ {summary['changed_during_inventory']} files changed "
            f"during the walk (digest withdrawn or re-read; rerun when idle)"
        )
    if summary["unreadable"]:
        print(
            f"  ⚠ {summary['unreadable']} files unreadable "
            f"(permissions/I/O — counted, never guessed)"
        )
    unknown = summary["by_grade"].get("unknown-time", 0)
    if unknown:
        print(f"  ⚠ {unknown} files graded unknown-time (add rules or exclude)")
    if summary["unwalked_top_level"]:
        print(f"  unwalked: {summary['unwalked_top_level']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="all",
                        help="principal name or 'all' (default)")
    parser.add_argument("--no-digests", action="store_true",
                        help="skip sha256 (fast structural pass)")
    parser.add_argument("--max-digest-bytes", type=int,
                        default=DEFAULT_MAX_DIGEST_BYTES)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # noqa: E402  (needs the lovework-agent dir on sys.path)

    principals = (config.principal_names() if args.profile == "all"
                  else [args.profile])
    for principal in principals:
        state_root = config.STATE_DIR / principal
        if not state_root.is_dir():
            print(f"{principal}: no state dir at {state_root}", file=sys.stderr)
            return 66
        summary = inventory_principal(
            principal, state_root,
            digests=not args.no_digests,
            max_digest_bytes=args.max_digest_bytes,
            root=config.LOVEWORK_ROOT,
        )
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

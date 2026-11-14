#!/usr/bin/env python3
"""Revision context for a LoveWork run — which code produced this result.

Why this exists (2026-09-19): a Saturday sweep ran on host-a while host-b's
checkout carried WP1b wiring that host-a's `main` did not. The missing `[SEAL]`
markers and the absent `strata/attempts/` were the only clues that the run used
the older revision — the log named the date, principal, role and sources, but
never the code. LoveWork is one work tree with three git dirs (`.git` →
`.git-private`, plus `.git-public`, `.git-lovework-public`) and every host runs
its own checkout, so a run is attributable only if the revision travels with it.

`describe()` is deliberately best-effort: every git call is time-boxed, and a
missing binary, a non-repository tree or an unreadable git dir is reported in
`error` instead of raising. Revision evidence must never break a crawl.

CLI (what the wrapper logs):

    ../venv/bin/python3 git_context.py [--repo PATH] [--json] [--recent N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_RECENT = 5
MAX_DIRTY_PATHS = 8
SUBJECT_CHARS = 60
_GIT_TIMEOUT_SECONDS = 5.0


def repo_root() -> Path:
    """The LoveWork repo root — this module lives in the engine beside it."""
    return Path(__file__).resolve().parent.parent


_OCTAL_DIGITS = frozenset("01234567")
_ESCAPED_BYTES = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}


def _unquote_path(raw: str) -> str:
    """Decode Git's C-style path quoting.

    `git status --porcelain` wraps a path containing non-ASCII or special
    characters in double quotes and escapes it octally (`"\\303\\274n..."`), so
    the raw bytes have to be reconstituted before display. A rename arrives as
    one entry holding both names (`old -> new`) — readable as-is, and the dirty
    count is unaffected either way.
    """
    raw = raw.strip()
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8", "surrogateescape"))
            i += 1
            continue
        octal = body[i + 1 : i + 4]
        if len(octal) == 3 and all(digit in _OCTAL_DIGITS for digit in octal):
            out.append(int(octal, 8) & 0xFF)
            i += 4
            continue
        following = body[i + 1] if i + 1 < len(body) else ""
        out.append(_ESCAPED_BYTES.get(following, ord(following) if following else 92) & 0xFF)
        i += 2
    return out.decode("utf-8", "replace")


def _git(root: Path, *args: str) -> str | None:
    """Run one git command; return its stdout, or None on any failure.

    Only the trailing newline is stripped: `git status --porcelain` starts each
    line with the two-character XY code, so the *first* line's leading space is
    data (` M README.LJ`), and `.strip()` would shift every path by one
    character while leaving later lines intact.
    """
    try:
        result = subprocess.run(
            # `-c safe.directory=*`: the checkout may be owned by a different
            # user than the one running LoveWork (2026-10-26: the scheduled
            # runtime moved to `lovework` while `user` still works in the
            # same tree), and git's dubious-ownership guard then refuses every
            # read — the run record loses its revision silently. Revision
            # evidence must not depend on who owns the checkout, nor on a
            # global git config that a stripped environment may not carry.
            ["git", "-C", str(root), "-c", "safe.directory=*", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def describe(
    root: Path | str | None = None, *, recent: int = DEFAULT_RECENT
) -> dict[str, Any]:
    """Describe the revision of the checkout at *root* (default: this repo).

    Never raises. On failure the returned dict has ``error`` set and no
    revision fields; callers log whatever they got.
    """
    tree = Path(root) if root is not None else repo_root()
    info: dict[str, Any] = {
        "root": str(tree),
        "branch": None,
        "head": None,
        "head_short": None,
        "subject": None,
        "dirty": None,
        "dirty_paths": [],
        "recent": [],
        "error": None,
    }

    head = _git(tree, "rev-parse", "HEAD")
    if head is None:
        info["error"] = "no git revision (missing git, not a repository, or unreadable)"
        return info

    info["head"] = head
    info["head_short"] = head[:8]
    info["branch"] = _git(tree, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    info["subject"] = _git(tree, "log", "-1", "--format=%s")

    porcelain = _git(tree, "status", "--porcelain")
    if porcelain is not None:
        # XY status occupies the first two columns; the path follows after one space.
        paths = [_unquote_path(line[2:]) for line in porcelain.splitlines() if line[2:].strip()]
        info["dirty"] = len(paths)
        info["dirty_paths"] = paths[:MAX_DIRTY_PATHS]

    if recent > 0:
        oneline = _git(tree, "log", f"-{recent}", "--format=%h %s")
        if oneline:
            info["recent"] = [
                {"sha": line.split(" ", 1)[0], "subject": line.split(" ", 1)[1] if " " in line else ""}
                for line in oneline.splitlines()
                if line.strip()
            ]
    return info


def render_line(info: dict[str, Any], *, recent: int = 3) -> str:
    """One log-friendly line: branch, head, subject, dirty state, recent commits."""
    if info.get("error"):
        return f"unavailable: {info['error']}"
    parts = [
        f"branch={info.get('branch') or '?'}",
        f"head={info.get('head_short') or '?'}",
    ]
    subject = (info.get("subject") or "").strip()
    if subject:
        parts.append(f'"{subject[:SUBJECT_CHARS]}"')
    dirty = info.get("dirty")
    if dirty is None:
        parts.append("dirty=unknown")
    elif dirty == 0:
        parts.append("dirty=0 (clean)")
    else:
        paths = ", ".join(info.get("dirty_paths") or [])
        extra = f"; +{dirty - len(info.get('dirty_paths') or [])} more" if dirty > len(info.get("dirty_paths") or []) else ""
        parts.append(f"dirty={dirty} [{paths}{extra}]")
    commits = info.get("recent") or []
    if commits:
        shown = " | ".join(f"{c['sha']} {c['subject'][:SUBJECT_CHARS]}" for c in commits[:recent])
        parts.append(f"recent: {shown}")
    return " ".join(parts)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Print this checkout's revision context")
    parser.add_argument("--repo", type=Path, default=None, help="checkout to describe (default: this repo)")
    parser.add_argument("--recent", type=int, default=DEFAULT_RECENT, help="commits to list (0 to skip)")
    parser.add_argument("--json", action="store_true", help="emit the full structure instead of one line")
    args = parser.parse_args()

    info = describe(args.repo, recent=args.recent)
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        print(render_line(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

#!/usr/bin/env python3
"""Memory coarsening — keep every OptMem tree settled (the watch's daily step).

OptMem's LOG is append-only and the TREE is its compression. `memo nap` performs
one compression and, with no arguments, *hands the agent the prompt* — the block's
records, the size budget, and the exact command to run:

    Compress memories #4-7 into one line of at most 280 bytes.
    Keep what has lasting effect, drop what does not. Invent nothing.

So coarsening is an agent task by design, and this is that agent: it takes the
tool's own prompt, asks the model for the one line, and runs the tool's own
command. Nothing is invented, nothing is deleted — the LOG keeps every record and
the seals keep the LOG (docs/26), so a nap is recoverable by replay, and `forget`
can drop a bad summary for `nap` to rebuild.

Report-only unless `--apply` (the house rule for anything that writes):

    python3 consolidate.py --profile lj                  # what would be done
    python3 consolidate.py --profile lj --max 2 --apply   # do at most 2

Wired into the watch run (`~/.hermes-host-a/.../lovework-<p>-watch.sh`)
so memory is coarsened a little every day, before the brief reads it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import mem as memmod
from llm_client import LLMClient

#: Memories are walked in this order; `_attic` is skipped everywhere (it holds
#: folded-away sources and unreadable-name residue on purpose).
KINDS = ("principal", "orgs", "people", "agents")


def iter_memories(profile: str) -> list[Path]:
    """Every live memory of one principal, deterministic order."""
    root = config.STATE_DIR / profile / "dataset" / "memory"
    out: list[Path] = []
    for kind in KINDS:
        d = root / kind
        if not d.is_dir():
            continue
        if kind == "principal":
            out.append(d)
            continue
        out.extend(sorted(p for p in d.iterdir()
                          if p.is_dir() and not p.name.startswith("_")))
    return out


def nap_prompt(memdir: Path) -> str:
    """`memo nap` with no arguments: this memory's next pending compression.

    Returns "" when there is nothing to compress (or the memory is unreadable).
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(memmod.MEMO), "nap"],
            env=dict(os.environ, MEMORY_DIR=str(memdir)),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip()


def parse_range(prompt: str) -> tuple[int, int] | None:
    """`Compress memories #4-7 into one line…` → (4, 7)."""
    m = re.search(r"Compress memories #(\d+)-(\d+)", prompt)
    return (int(m.group(1)), int(m.group(2))) if m else None


def one_line(client: LLMClient, prompt: str) -> str:
    """Ask the model for the single summary line the prompt asks for.

    `deepseek-v4.1-flash` is a reasoning model on the relay: it spends output
    tokens thinking, and DeepSeek counts that CoT *inside* `max_tokens`, so a
    small budget returns `finish_reason=length` with no visible content at all
    (800 and 2,000 both did). The budget is therefore left to
    `config.LLM_MAX_TOKENS`, which is deliberately generous — a cap is a ceiling,
    not a target: at 131,072 the model still stopped at ~1.8K tokens and wrote
    the line.

    Measured 2026-10-05 (one call, same prompt): ~1,800-3,800 completion tokens
    for a ~33-token visible line, so thinking runs ~30-60x the visible output.
    `reasoning_effort` is the lever if that ever needs cutting — `"low"` gave
    1,028 tokens and `"none"` gave 38, both with the same line ("minimal" is
    accepted and ignored). It is left at full thinking here on purpose: a memory
    summary is judgement, and two calls a day cost nothing.
    """
    line = client.chat([{"role": "user", "content": prompt}],
                       context="mem-consolidate").strip()
    line = " ".join(line.split())
    line = memmod.fit_one_line(line)
    return line


def consolidate_memory(memdir: Path, client: LLMClient, *, apply: bool) -> dict:
    """One memory: at most one compression. Returns a report row."""
    prompt = nap_prompt(memdir)
    row = {"memory": str(memdir.name), "pending": False, "applied": False,
           "range": "", "line": "", "note": ""}
    if not prompt or "Nothing left to compress." in prompt:
        return row
    row["pending"] = True
    rng = parse_range(prompt)
    if rng is None:
        row["note"] = "unrecognised nap prompt"
        return row
    row["range"] = "%d-%d" % rng
    line = one_line(client, prompt)
    if not line:
        row["note"] = "model returned nothing"
        return row
    row["line"] = line
    if not apply:
        return row
    code = memmod.run_memo(memdir, ["nap", row["range"], line], quiet=True)
    row["applied"] = code == 0
    if code != 0:
        row["note"] = "nap refused (exit %d)" % code
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Coarsen OptMem trees (nap pending compressions)")
    ap.add_argument("--profile", default="all")
    ap.add_argument("--max", type=int, default=2,
                    help="compressions per principal per run (default 2)")
    ap.add_argument("--apply", action="store_true", help="perform (default: report only)")
    args = ap.parse_args(argv)
    config.set_active_profile(args.profile)

    from writer_boundary import current_writer

    lease = current_writer()
    if lease is not None:
        # A crawl (or another sealed writer) holds the principal. Napping under a
        # writer would interleave with its notes for no benefit; the next daily
        # run picks the work up.
        print("[consolidate] deferred — a writer holds %s" % getattr(lease, "principal", "?"))
        return 0

    profiles = ([args.profile] if args.profile != "all"
                else sorted(p.name for p in config.STATE_DIR.iterdir()
                            if (p / "dataset" / "memory").is_dir()))
    client = LLMClient()
    total_applied = 0
    for principal in profiles:
        memories = iter_memories(principal)
        if not memories:
            continue
        done = 0
        for memdir in memories:
            if done >= args.max:
                break
            row = consolidate_memory(memdir, client, apply=args.apply)
            if not row["pending"]:
                continue
            done += 1
            mark = "✓" if row["applied"] else ("·" if not args.apply else "✗")
            print("  %s %s/%s  #%s  %s" % (mark, principal, row["memory"], row["range"],
                                           row["line"][:110] or row["note"]))
            if row["applied"]:
                total_applied += 1
        print("== %s: %d of %d memories need coarsening (cap %d)%s"
              % (principal, done, len(memories), args.max,
                 "" if args.apply else " — report only"))
    if args.apply:
        print("coarsened: %d compression(s)" % total_applied)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded operational status for one principal — the cockpit's source (WP4a).

Three rules shape this module:

1. **Pure readers compute; nothing here writes on read.** `status()` only reads.
2. **Bounded output.** Recent runs, counts rather than listings, and no
   cross-principal data: the cockpit is a projection, not an export.
3. **Honest capability labels.** What this deployment *cannot* answer is named
   as unavailable with the reason, never rendered as an empty result — an empty
   list and an unanswerable question look identical to a reader, and they mean
   opposite things.

`STATE.md` is persisted only by an explicit `refresh()`, atomically, and a
failed refresh leaves the previous rendering in place with a dated error beside
it (the plan's requirement: a dated prior rendering, never a blank cockpit).
"""
from __future__ import annotations
import jsonl_io

import csv
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from selection import ACQUISITION_OUTCOMES, NON_OBSERVATION_PREFIXES

logger = logging.getLogger(__name__)

RECENT_RUNS = 5          # bounded: the cockpit shows a window, not the archive
STATUS_FILENAME = "STATE.md"


def _read_jsonl(path: Path, limit: Optional[int] = None) -> list[dict]:
    if not path.is_file():
        return []
    rows = [json.loads(l) for l in jsonl_io.lines(path)
            if l.strip()]
    return rows[-limit:] if limit else rows


def _lifecycle_counts(registry_csv: Path) -> dict:
    if not registry_csv.is_file():
        return {}
    counts: dict[str, int] = {}
    with registry_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status = (row.get("status") or "unknown").strip()
            counts[status] = counts.get(status, 0) + 1
    return counts


def _coverage(manifest) -> list[dict]:
    """Per-source health as the seal recorded it — attempts, not guesses."""
    out = []
    for attempt in getattr(manifest, "coverage", []) or []:
        out.append({
            "source": attempt.source,
            "scope": attempt.scope,
            "outcome": attempt.outcome,
            "cache_use": getattr(attempt, "cache_use", "unknown"),
            # The distinction WP4b exists to keep: an unread scope is not an
            # empty one, so "did this run actually see the world?" is explicit.
            # It also has to be *about* the world: a coverage entry for a
            # preflight or a token check is bookkeeping about the run, and
            # claiming it read the market is a claim the seal does not support
            # (visible once the receipt child stopped being the newest seal).
            "read_the_world": (not str(attempt.source).startswith(NON_OBSERVATION_PREFIXES)
                               and attempt.outcome in ACQUISITION_OUTCOMES
                               and getattr(attempt, "cache_use", "") != "cached_only"),
        })
    return out


def status(runtime, engine, *, recent_runs: int = RECENT_RUNS) -> dict:
    """The bounded status projection. Reads only; never writes."""
    runs = _read_jsonl(runtime.dataset_dir / "runs.jsonl", limit=recent_runs)
    # Bundles on disk, split into results and operation children. A late
    # receipt child (notification, link recheck) seals *after* the sweep it
    # reports on, so "the newest bundle" is routinely the email receipt:
    # reading it as the newest seal made the cockpit describe `receipt:email`
    # instead of the crawl, and inflated `count`. The engine answers the
    # distinction (`strata_contract.is_child_manifest`) — asked, not re-implemented.
    result_seals = engine.result_run_ids()
    child_seals = engine.child_run_ids()
    bundles = result_seals + child_seals

    newest = None
    newest_manifest = None
    for run_id in result_seals:
        manifest = engine.read_manifest(run_id)
        if manifest is None:
            continue
        if newest is None or manifest.clock.sealed_at > newest.clock.sealed_at:
            newest, newest_manifest = manifest, manifest
    latest_run = runs[-1] if runs else None
    latest_sealed = (latest_run is not None
                     and any(r == latest_run.get("run_id") for r in result_seals))

    coverage = _coverage(newest_manifest) if newest_manifest else []
    gaps = [{"kind": g.kind, "reason": g.reason}
            for g in (getattr(newest_manifest, "gaps", []) or [])] \
        if newest_manifest else []

    episodes = None
    survival = None
    if result_seals:
        import availability
        ep = availability.episodes(engine)
        episodes = {"counts": ep.get("counts", {}),
                    "identity_shifts": len(ep.get("identity_shifts", [])),
                    "seals": ep.get("seals", 0)}
        survival = availability.survival(ep)

    counts = _lifecycle_counts(runtime.cache_dir / "jobs.csv")
    # Only a coverage attempt that names a *source* can qualify that source's
    # absence. A seal that records the crawl (`crawl:full`) has not told us that
    # any individual source read its whole scope, so it qualifies nothing — and
    # the cockpit says so rather than presenting a crawl as a source.
    known_sources = _registry_sources(runtime.cache_dir / "jobs.csv")
    complete_sources = sorted({c["source"] for c in coverage
                               if c["read_the_world"] and c["source"] in known_sources})
    coverage_names_sources = any(c["source"] in known_sources for c in coverage)

    return {
        "principal": runtime.profile_name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_run": ({
            "run_id": latest_run.get("run_id"),
            "mode": latest_run.get("mode"),
            "started_at": latest_run.get("started_at"),
            "sources": latest_run.get("sources") or [],
            "sealed": latest_sealed,
        } if latest_run else None),
        "recent_runs": [{"run_id": r.get("run_id"), "mode": r.get("mode"),
                         "started_at": r.get("started_at"),
                         "sources": len(r.get("sources") or [])}
                        for r in runs],
        "seals": {
            "count": len(result_seals),
            "children": len(child_seals),
            "bundles": len(bundles),
            "newest": newest.clock.sealed_at if newest else None,
            "newest_run_id": newest.run.run_id if newest else None,
            "newest_digest": newest.digest() if newest else None,
        },
        "coverage": coverage,
        "gaps": gaps,
        "lifecycle": counts,
        "episodes": episodes,
        "capabilities": _capabilities(result_seals, complete_sources, episodes,
                                      coverage_names_sources),
        "llm_usage": _llm_usage(runtime),
        "pointers": {
            "state_root": str(engine.state_root),
            "registry": str(runtime.cache_dir / "jobs.csv"),
            "run_ledger": str(runtime.dataset_dir / "runs.jsonl"),
            "assessment_ledger": str(runtime.dataset_dir / "assessments.jsonl"),
            "model_calls_ledger": str(runtime.dataset_dir / "model_calls.jsonl"),
            "wiki_index": str(runtime.wiki_root / "index.md"),
            "reports": str(runtime.wiki_root / "reports"),
            "state_md": str(engine.state_root / STATUS_FILENAME),
        },
        "unavailable": _unavailable(result_seals, complete_sources,
                                    coverage_names_sources),
    }


def _llm_usage(runtime) -> dict:
    """Per-provider/per-model LLM totals from the model-calls ledger.

    Reads only; a missing or unreadable ledger is an empty summary, never
    an error — the cockpit must stay available when accounting has nothing
    to say. Totals count only rows that report each field (see
    `model_transcripts.summarize_usage`); unknown is unknown, not zero.
    """
    try:
        import model_transcripts as mt

        return mt.summarize_usage(runtime.dataset_dir)
    except Exception as e:  # the cockpit never breaks over accounting
        logger.debug("LLM usage summary skipped: %s", e)
        return {"ledger": str(runtime.dataset_dir / "model_calls.jsonl"),
                "rows": 0, "skipped_rows": 0, "groups": 0, "by_model": []}


def _registry_sources(registry_csv: Path) -> set[str]:
    if not registry_csv.is_file():
        return set()
    with registry_csv.open(newline="", encoding="utf-8") as f:
        return {(row.get("source") or "").strip() for row in csv.DictReader(f)} - {""}


def _capabilities(seals: list[str], complete_sources: list[str],
                  episodes: Optional[dict],
                  coverage_names_sources: bool = True) -> dict:
    """What this deployment can answer, and on what basis."""
    return {
        "asof": (f"available: {len(seals)} seal(s) to read from"
                 if seals else "unavailable: no seals yet — nothing to read at a "
                               "cutoff"),
        "episodes": ("available: availability episodes from sealed snapshots"
                     if seals else "unavailable: needs seals"),
        "survival": ("available, right-censored only: no source has declared a "
                     "complete scope, so no closure can be inferred"
                     if seals and not complete_sources else
                     ("available: closures may be inferred where a source read "
                      "its whole scope" if seals else "unavailable: needs seals")),
        "qualified_absence": (
            f"available for: {', '.join(complete_sources)}" if complete_sources
            else ("unavailable: the newest seal's coverage records the crawl, not "
                  "per-source scopes, so no source can be qualified — every "
                  "absence stays unobserved"
                  if seals and not coverage_names_sources else
                  "unavailable: no source declared a whole-scope enumeration in "
                  "the newest seal")),
        "state_md": "written only by an explicit refresh (status.py --write)",
    }


def _unavailable(seals: list[str], complete_sources: list[str],
                 coverage_names_sources: bool = True) -> list[str]:
    out = []
    if not seals:
        out.append("no sealed snapshots: point-in-time reads, episodes and "
                   "survival cannot be answered from this deployment yet")
    if seals and not complete_sources:
        if not coverage_names_sources:
            out.append("per-source coverage is not recorded in seals yet (the "
                       "newest seal names the crawl, not each source's scope), so "
                       "absence can never be qualified and every advert is "
                       "right-censored — no closure is inferred, which is the "
                       "conservative reading, not a working one")
        else:
            out.append("no whole-scope enumeration declared: absence cannot be "
                       "read as closure, so every advert is right-censored")
    return out


def _render_llm_usage(usage: dict) -> list[str]:
    """One line per (provider, model): counts, latency, tokens, tok/s, spend.

    Missing data reads as unknown, never as zero — a provider that reports
    no usage object still has its requests counted (latency is measured
    locally), while tokens and spend say how many rows knew them.
    """
    groups = usage.get("by_model") or []
    if not groups:
        return ["- no recorded calls"]
    lines = []
    for g in groups:
        lat = ("%.1fs" % g["mean_latency_s"]
               if g.get("mean_latency_s") is not None else "unknown")
        toks: list[str] = []
        for field, short in (("prompt_tokens", "in"), ("completion_tokens", "out"),
                             ("total_tokens", "total")):
            if g.get(f"{field}_known"):
                toks.append(f"{g[field]} {short} ({g[f'{field}_known']}/{g['requests']})")
            else:
                toks.append(f"{short}: unknown")
        tps = ("%.0f tok/s" % g["mean_tokens_per_second"]
               if g.get("mean_tokens_per_second") is not None else "tok/s: unknown")
        spend = ("$%.4f (%d/%d)" % (g["cost_usd"], g["cost_usd_known"], g["requests"])
                 if g.get("cost_usd_known") else "spend: unknown")
        lines.append(
            f"- {g['provider']} / {g['model']}: "
            f"{g['requests']} request(s), mean latency {lat}, "
            f"{', '.join(toks)}, {tps}, {spend}")
    return lines


# ── STATE.md ─────────────────────────────────────────────────────────────

def render_state_md(st: dict) -> str:
    lines = [f"# STATE — {st['principal']}", "",
             f"_Generated {st['generated_at']} by `status.py`. Bounded projection: "
             f"recent runs and counts, not the archive._", ""]
    run = st.get("latest_run")
    if run:
        lines += ["## Latest run", "",
                  f"- `{run['run_id']}` — {run['mode']}, started {run['started_at']}",
                  f"- sources: {', '.join(run['sources']) or '—'}",
                  f"- sealed: {'yes' if run['sealed'] else 'no'}"]
    else:
        lines += ["## Latest run", "", "- none recorded"]
    # Result bundles are what a reader means by "a seal"; operation children
    # (receipts, link rechecks) are named separately so the number is honest
    # without hiding them (2026-10-07: the count mixed the two).
    children = st["seals"].get("children", 0)
    lines += ["", "## Seals", "",
              f"- count: {st['seals']['count']} result bundle(s)"
              + (f"; {children} receipt child bundle(s) also on disk" if children else ""),
              f"- newest: {st['seals']['newest'] or '—'}",
              f"- digest: {st['seals']['newest_digest'] or '—'}"]

    if st["coverage"]:
        lines += ["", "## Source health (newest seal)", "",
                  "| source | scope | outcome | cache | read the world |",
                  "|---|---|---|---|---|"]
        for c in st["coverage"]:
            lines.append(f"| {c['source']} | {c['scope']} | {c['outcome']} | "
                         f"{c['cache_use']} | {'yes' if c['read_the_world'] else 'no'} |")
    if st["gaps"]:
        lines += ["", "## Capture gaps", ""]
        lines += [f"- {g['kind']}: {g['reason']}" for g in st["gaps"]]

    if st["lifecycle"]:
        lines += ["", "## Registry lifecycle", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(st["lifecycle"].items())]

    if st.get("episodes"):
        s = st.get("survival") or {}
        lines += ["", "## Availability", "",
                  f"- episodes: {st['episodes']['counts'].get('episodes', 0)} "
                  f"({st['episodes']['counts'].get('open', 0)} open, "
                  f"{st['episodes']['counts'].get('closed', 0)} closed)",
                  f"- identity shifts reported: {st['episodes']['identity_shifts']}",
                  f"- median lower-bound duration: "
                  f"{'—' if s.get('median_lower_bound_days') is None else str(s['median_lower_bound_days']) + 'd'}"]

    lines += ["", "## LLM usage (per provider/model)", ""]
    lines += _render_llm_usage(st.get("llm_usage") or {})

    lines += ["", "## What this deployment can answer", ""]
    if st["unavailable"]:
        lines += ["", "## Not answerable here", ""]
        lines += [f"- {u}" for u in st["unavailable"]]
    lines += ["", "## Pointers", ""]
    lines += [f"- {k}: `{v}`" for k, v in st["pointers"].items()]
    return "\n".join(lines) + "\n"


def refresh(st: dict, state_root: Path) -> dict:
    """Persist STATE.md atomically. A failure leaves the prior rendering.

    Returns a small result; on failure it also writes a dated error beside the
    cockpit, because a silently stale STATE.md is worse than none at all.
    """
    path = state_root / STATUS_FILENAME
    text = render_state_md(st)
    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="STATE-", dir=str(state_root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        err = state_root / (STATUS_FILENAME + ".error")
        err.write_text(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: "
            f"refresh failed ({type(e).__name__}: {e}); the previous rendering "
            f"is left in place\n", encoding="utf-8")
        logger.warning("STATE.md refresh failed: %s", e)
        return {"ok": False, "path": str(path), "error": str(e),
                "prior_rendering_kept": path.is_file()}
    return {"ok": True, "path": str(path), "bytes": len(text)}


# ── CLI ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Bounded operational status for one principal (WP4a)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--write", action="store_true",
                        help="atomically persist STATE.md (reads never write)")
    parser.add_argument("--json", action="store_true", help="raw projection")
    parser.add_argument("--llm-usage", action="store_true",
                        help="print per-provider/per-model LLM totals and exit")
    args = parser.parse_args(argv)

    from principal_runtime import resolve_principal_runtime
    from strata import StrataEngine

    runtime = resolve_principal_runtime(args.profile)
    if not runtime.state_root.is_dir():
        print(f"no state dir: {runtime.state_root}", file=sys.stderr)
        return 66
    if args.llm_usage:
        import model_transcripts as mt

        print(mt.render_usage_text(mt.summarize_usage(runtime.dataset_dir)), end="")
        return 0
    st = status(runtime, StrataEngine(runtime.state_root))
    if args.json:
        print(json.dumps(st, indent=2, sort_keys=True, default=str))
    else:
        print(render_state_md(st), end="")
    if args.write:
        result = refresh(st, runtime.state_root)
        print(f"[STATE.md] {'written' if result['ok'] else 'FAILED'}: "
              f"{result['path']}", file=sys.stderr)
        if not result["ok"]:
            print(f"[STATE.md] {result['error']}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

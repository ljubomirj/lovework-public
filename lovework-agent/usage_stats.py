#!/usr/bin/env python3
"""usage_stats.py — what the models cost us, per provider and model.

LJ, 2026-10-13: *"We started collecting tracking the tokens stats per
provider/model (including local). Can we see some stats about that on the C&C
web page?"*

Three ledgers carry model usage, and this is the one place that reads all of
them:

- `state/<p>/dataset/model_calls.jsonl` — the crawl's and the watch's calls
  (the relay, `opencode-go`);
- `state/<p>/loop/transcripts/model_calls.jsonl` — the agentic loop's calls (the
  local endpoint, `openai-compatible`);
- `~/.hermes*/profiles/*/cron/usage_audit.jsonl` — the Hermes *agent* jobs
  (the meta-investigators and the pack session). Host-local and outside the repo,
  so it is read when it is there and reported as absent when it is not.

Nothing here writes: it is a read over ledgers that already exist, so it can be
served by the dashboard and asked from a shell without side effects.

    ../venv/bin/python3 usage_stats.py --days 7
    ../venv/bin/python3 usage_stats.py --days 30 --json
    ../venv/bin/python3 usage_stats.py --days 7 --by-day
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import jsonl_io

#: The ledgers, by the name a reader knows them by. Order is the report's order:
#: the crawl first, because it is the cost the plan was built on.
LEDGERS = (
    ("crawl", "dataset/model_calls.jsonl"),
    ("loop", "loop/transcripts/model_calls.jsonl"),
)

#: Token fields, in the order the columns read. A row without them is *unmetered*
#: — the older crawl rows predate the accounting (measured 2026-10-13: all of
#: vj's and kj's crawl rows), and counting them as zero would understate the
#: total while dropping them would overstate the coverage.
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _rows(path: Path):
    """Yield (row, unreadable_reason) for a JSONL file, never raising."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line), None
            except json.JSONDecodeError as exc:
                yield None, f"{path.name} line {number}: {exc}"


def _day(row: dict) -> str:
    stamp = str(row.get("at") or row.get("ts") or "")
    return stamp[:10] if len(stamp) >= 10 else "unknown"


def hermes_usage_paths() -> list[Path]:
    """The Hermes agent-job usage audits on this host, if any.

    Host-local and outside the repo by design (ch15: credentials and runtime stay
    on the host), so this is a *read of what is here*: a machine without the
    gateway reports the source as absent rather than inventing a zero.
    """
    pattern = os.path.expanduser("~/.hermes*/profiles/*/cron/usage_audit.jsonl")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def aggregate(*, days: int = 7, state_dir: Path | None = None,
              principals: tuple[str, ...] | None = None,
              include_hermes: bool = True,
              today: date | None = None) -> dict:
    """Calls, tokens and cost per (source, provider, model), plus per day.

    `days` is a window on the row's own date: a ledger holds history, and a cost
    answer that silently mixes a year of crawls is not an answer.
    """
    state_dir = state_dir or config.STATE_DIR
    today = today or date.today()
    cutoff = (today - timedelta(days=max(0, days - 1))).isoformat()
    if principals is None:
        principals = tuple(sorted(p.name for p in state_dir.iterdir()
                                  if p.is_dir() and (p / "dataset").is_dir())) \
            if state_dir.is_dir() else ()

    totals: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "unmetered": 0, "cost_usd": 0.0,
                 "latency_s": 0.0, "latency_calls": 0})
    by_day: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "total_tokens": 0, "unmetered": 0})
    unreadable: list[str] = []
    seen_files: list[str] = []

    def add(source, principal, row, reason):
        if reason:
            unreadable.append(reason)
            return
        if row is None:
            return
        day = _day(row)
        if day != "unknown" and day < cutoff:
            return
        provider = str(row.get("provider") or "unknown")
        model = str(row.get("model") or "unknown")
        key = (source, provider, model)
        bucket = totals[key]
        bucket["calls"] += 1
        if all(row.get(f) is None for f in TOKEN_FIELDS):
            bucket["unmetered"] += 1
        else:
            for field in TOKEN_FIELDS:
                value = row.get(field)
                if isinstance(value, (int, float)):
                    bucket[field] += int(value)
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)):
            bucket["cost_usd"] += float(cost)
        latency = row.get("latency_s")
        if isinstance(latency, (int, float)):
            bucket["latency_s"] += float(latency)
            bucket["latency_calls"] += 1
        day_bucket = by_day[(day, principal, source, provider, model)]
        day_bucket["calls"] += 1
        if all(row.get(f) is None for f in TOKEN_FIELDS):
            day_bucket["unmetered"] += 1
        elif isinstance(row.get("total_tokens"), (int, float)):
            day_bucket["total_tokens"] += int(row["total_tokens"])

    for principal in principals:
        root = state_dir / principal
        for source, relative in LEDGERS:
            path = root / relative
            if path.is_file():
                seen_files.append(str(path))
            for row, reason in _rows(path):
                add(source, principal, row, reason)

    if include_hermes:
        for path in hermes_usage_paths():
            seen_files.append(str(path))
            for row, reason in _rows(path):
                if reason:
                    unreadable.append(reason)
                    continue
                if row is None:
                    continue
                day = _day(row)
                if day != "unknown" and day < cutoff:
                    continue
                key = ("hermes-agent", "hermes-cron",
                       str(row.get("model") or "unknown"))
                bucket = totals[key]
                bucket["calls"] += 1
                for field in TOKEN_FIELDS:
                    value = row.get(field)
                    if isinstance(value, (int, float)):
                        bucket[field] += int(value)
                duration = row.get("duration_ms")
                if isinstance(duration, (int, float)):
                    bucket["latency_s"] += float(duration) / 1000.0
                    bucket["latency_calls"] += 1
                day_bucket = by_day[(day, "all", "hermes-agent",
                                     "hermes-cron",
                                     str(row.get("model") or "unknown"))]
                day_bucket["calls"] += 1
                if isinstance(row.get("total_tokens"), (int, float)):
                    day_bucket["total_tokens"] += int(row["total_tokens"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "since": cutoff,
        "principals": list(principals),
        "sources": [p for p in seen_files],
        "unreadable": unreadable,
        "rows": [
            {"source": key[0], "provider": key[1], "model": key[2], **value,
             "mean_latency_s": (round(value["latency_s"] / value["latency_calls"], 2)
                                if value["latency_calls"] else None)}
            for key, value in sorted(totals.items(),
                                     key=lambda kv: -kv[1]["total_tokens"])
        ],
        "by_day": [
            {"day": key[0], "principal": key[1], "source": key[2],
             "provider": key[3], "model": key[4], **value}
            for key, value in sorted(by_day.items())
        ],
    }


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def _metric(value, *, suffix: str = "", spec: str = "") -> str:
    """An optional engine metric, or an honest marker when it was not recorded.

    The engine's log is an untimestamped tail that rotates, so whole classes of
    line disappear: a restart onto a fresh log tail leaves no `MiB of VRAM free`
    line at all, and `engine_health` then reports that metric as None. Formatting
    None with `:,` raised TypeError and killed the whole 06:00 report of
    2026-10-24; printing `None%` (what the median fields did) is worse than
    saying the number was not recorded. Never invent one.
    """
    if value is None:
        return "n/a"
    return format(value, spec) + suffix


def render_text(stats: dict, *, by_day: bool = False) -> str:
    lines = [f"model usage — last {stats['window_days']} days "
             f"(since {stats['since']}) · principals {', '.join(stats['principals'])}"]
    header = ("source", "provider", "model", "calls", "prompt", "completion",
              "total", "unmetered", "cost $", "mean s")
    widths = (11, 17, 30, 6, 12, 12, 12, 10, 8, 7)
    lines.append("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    for row in stats["rows"]:
        cells = (row["source"], row["provider"], row["model"][:30],
                 _fmt(row["calls"]), _fmt(row["prompt_tokens"]),
                 _fmt(row["completion_tokens"]), _fmt(row["total_tokens"]),
                 _fmt(row["unmetered"]), _fmt(round(row["cost_usd"], 4)),
                 _fmt(row["mean_latency_s"]))
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(cells, widths)))
    if not stats["rows"]:
        lines.append("(no calls in the window)")
    if by_day:
        lines.append("")
        lines.append("by day:")
        for row in stats["by_day"]:
            lines.append(f"  {row['day']}  {row['principal']:4s} {row['source']:12s} "
                         f"{row['model'][:30]:30s} calls {row['calls']:4d} "
                         f"tokens {_fmt(row['total_tokens']):>12s}"
                         + (f"  ({row['unmetered']} unmetered)"
                            if row["unmetered"] else ""))
    if stats["unreadable"]:
        lines.append("")
        lines.append(f"unreadable lines: {len(stats['unreadable'])} "
                     f"(first: {stats['unreadable'][0]})")
    return "\n".join(lines)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--by-day", action="store_true")
    parser.add_argument("--no-hermes", action="store_true",
                        help="skip the host-local Hermes agent-job audit")
    parser.add_argument("--report", action="store_true",
                        help="write one inference report per principal (logs/)")
    parser.add_argument("--engine", action="store_true",
                        help="show the local engine's own numbers only")
    args = parser.parse_args()
    if args.engine:
        engine = engine_health()
        print(json.dumps(engine, indent=2) if args.json else _engine_text(engine))
        return 0
    if args.report:
        engine = engine_health()
        written = write_report(days=args.days, engine=engine)
        for path in written:
            print(f"wrote {path}")
        return 0
    stats = aggregate(days=args.days, include_hermes=not args.no_hermes)
    print(json.dumps(stats, indent=2) if args.json
          else render_text(stats, by_day=args.by_day))
    return 0


def _engine_text(engine: dict) -> str:
    if not engine.get("available"):
        return f"engine: unavailable — {engine.get('reason')}"
    if not engine.get("requests"):
        # The second shape of this dict: the log was read, but its tail carried
        # no request lines. Say that instead of KeyError-ing on the metrics.
        return (f"engine: no request lines in the log's tail "
                f"— {engine.get('reason') or 'nothing measured'}")
    return (f"engine: {engine['requests']} request(s), {engine['generated_tokens']:,} tokens in "
            f"{engine['generation_seconds']:,.0f} s · decode "
            f"{_metric(engine.get('decode_tps_median'))} tok/s median "
            f"(min {_metric(engine.get('decode_tps_min'))}) · drafts "
            f"{_metric(engine.get('draft_acceptance_median'), suffix='%')} · expert cache "
            f"{_metric(engine.get('expert_cache_median'), suffix='%')} · VRAM free "
            f"{_metric(engine.get('vram_free_mib_min'), spec=',')} MiB · longest "
            f"{engine['longest']['generated']:,} tok in {engine['longest']['seconds']} s")



#: Self-contained styling: the dashboard imports this module, so this module must
#: not import the dashboard (a cycle for a font stack is not a trade worth making).
_PAGE_CSS = """
:root { --bg:#FAFAF7; --fg:#1A1A1A; --dim:#6B6B66; --line:#D4D4D0; --live-bg:#F4F4F0;
        --accent:#1D4ED8; --warn:#B45309; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:26px 20px 60px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:16px; margin:26px 0 8px; }
a { color:var(--accent); }
.dim { color:var(--dim); }
table { border-collapse:collapse; width:100%; margin-top:8px; font-variant-numeric:tabular-nums; }
th, td { border-bottom:1px solid var(--line); padding:6px 8px; text-align:right; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2),
th:nth-child(3), td:nth-child(3) { text-align:left; }
th { background:var(--live-bg); font-weight:600; }
tr.total td { font-weight:600; background:var(--live-bg); }
.warn { color:var(--warn); }
code { background:var(--live-bg); padding:1px 4px; border-radius:3px; }
"""


def _pct(part: int, whole: int, *, decimals: int = 0) -> str:
    return f"{100.0 * part / whole:.{decimals}f}%" if whole else "—"


def render_html(stats: dict, *, by_day: bool = False) -> str:
    """The C&C page: what the models cost us, per provider and model."""
    rows = stats["rows"]
    calls = sum(r["calls"] for r in rows)
    total = sum(r["total_tokens"] for r in rows)
    unmetered = sum(r["unmetered"] for r in rows)
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{row['source']}</td><td>{row['provider']}</td>"
            f"<td><code>{row['model']}</code></td>"
            f"<td>{_fmt(row['calls'])}</td>"
            f"<td>{_fmt(row['prompt_tokens'])}</td>"
            f"<td>{_fmt(row['completion_tokens'])}</td>"
            f"<td>{_fmt(row['total_tokens'])}</td>"
            f"<td>{_fmt(row['unmetered'])}"
            + (f" <span class='dim'>({_pct(row['unmetered'], row['calls'])} = "
               f"{_fmt(row['unmetered'])} of {_fmt(row['calls'])} calls)</span>"
               if row["unmetered"] else "")
            + "</td>"
            # A mean is a division: show what was divided (LJ, 2026-10-13).
            f"<td>{_fmt(row['mean_latency_s'])}"
            + (f" <span class='dim'>(={_fmt(round(row['latency_s']))} s/"
               f"{row['latency_calls']})</span>" if row["mean_latency_s"] is not None else "")
            + "</td>"
            "</tr>")
    body.append(
        f"<tr class='total'><td colspan='3'>total</td><td>{_fmt(calls)}</td>"
        f"<td></td><td></td><td>{_fmt(total)}</td>"
        f"<td>{_fmt(unmetered)} <span class='dim'>({_pct(unmetered, calls)})</span></td>"
        "<td></td></tr>")

    day_html = ""
    if by_day and stats["by_day"]:
        rows_html = "".join(
            f"<tr><td>{r['day']}</td><td>{r['principal']}</td><td>{r['source']}</td>"
            f"<td><code>{r['model']}</code></td><td>{_fmt(r['calls'])}</td>"
            f"<td>{_fmt(r['total_tokens'])}</td>"
            f"<td>{_fmt(r['unmetered'])}</td></tr>"
            for r in stats["by_day"])
        day_html = (
            "<h2>By day</h2><table><thead><tr><th>day</th><th>principal</th>"
            "<th>source</th><th>model</th><th>calls</th><th>total tokens</th>"
            f"<th>unmetered</th></tr></thead><tbody>{rows_html}</tbody></table>")

    sources = "".join(f"<li><code>{s}</code></li>" for s in stats["sources"])
    unreadable = ""
    if stats["unreadable"]:
        unreadable = (f"<p class='warn'>{len(stats['unreadable'])} unreadable "
                      f"line(s): <code>{stats['unreadable'][0]}</code></p>")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LoveWork — model usage</title><style>{_PAGE_CSS}</style></head>
<body><main>
<h1>◆ Model usage</h1>
<div class="dim">last {stats['window_days']} days (since {stats['since']}) ·
principals {', '.join(stats['principals'])} · generated {stats['generated_at']}</div>
<p><a href="/">← LAN index</a> · <a href="/dashboard/">dashboard</a>
 · <a href="/usage/?days=1">today</a> · <a href="/usage/?days=30">30 days</a>
 · <a href="/usage/?days={stats['window_days']}&amp;by_day=1">by day</a></p>
<table><thead><tr><th>source</th><th>provider</th><th>model</th><th>calls</th>
<th>prompt tokens</th><th>completion tokens</th><th>total tokens</th>
<th>unmetered</th><th>mean s</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
{day_html}
{unreadable}
<h2>Where this comes from</h2>
<ul>{sources or '<li class="dim">no ledger found</li>'}</ul>
<p class="dim">An <em>unmetered</em> call is a row with no token counts. The
crawl's older rows predate the accounting, so they count as calls but not as
tokens — the totals are a floor, not the whole truth. The local model
(<code>openai-compatible</code>) has no price, and the relay's
<code>cost_usd</code> is not filled in by the provider, so the cost column is
deliberately absent rather than invented.</p>
</main></body></html>
"""


# ── the local engine's own view (its log) ────────────────────────────────

#: Where Strata writes. Host-local and outside the repo, like every other
#: runtime path (ch15): the report says "absent" rather than inventing numbers.
STRATA_LOG_ENV = "LOVEWORK_STRATA_LOG"

_REQUEST_RE = None


def strata_log_path() -> Path | None:
    """The newest Strata log on this host, or None."""
    explicit = os.environ.get(STRATA_LOG_ENV)
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    candidates = [Path(p) for p in glob.glob(os.path.expanduser(
        "~/LJ-amdgpu-7900xtx/Strata/*.log"))]
    # Newest first, and a `*.smoke.log` only if it is all there is: the smoke
    # logs are one-request probes, and reporting *them* as the engine's health
    # would describe a test, not the box (found 2026-10-13 — the glob's
    # alphabetical order picked the smoke log).
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True)
    real = [p for p in candidates if ".smoke." not in p.name]
    chosen = real or candidates
    return chosen[0] if chosen else None


def engine_health(path: Path | None = None, *, tail: int = 4000) -> dict:
    """What the inference engine says about itself: throughput and headroom.

    Parsed from its own log, because the two halves of the picture live in
    different places: LoveWork's ledgers know *what we asked for*, the engine's
    log knows *what it cost to serve* — decode tok/s, speculative-draft
    acceptance, expert-cache hit rate, KV residency and free VRAM. A report that
    shows only one of them cannot say whether a slow action was our prompt or
    the box.
    """
    import re
    import statistics

    path = path or strata_log_path()
    if path is None or not path.is_file():
        return {"available": False, "reason": "no Strata log found"}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")[-tail:]
    except OSError as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    request = re.compile(
        r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms "
        r"\(([\d.]+) tok/s\), (\d+) generated in (\d+) ms \(([\d.]+) tok/s\), "
        r"drafts accepted (\d+) of (\d+)")
    # Every ratio this report prints carries its numerator and denominator
    # (LJ, 2026-10-13: "where we measure 2 things, and then we report the ratio …
    # report the numbers in the numerator and denominator in parenthesis"). So the
    # parse keeps the *totals* as well as the per-request values, and the sums are
    # the parenthetical: a median and an aggregate ratio are different facts and
    # showing both is the honest version.
    cache = re.compile(r"decode expert cache hit rate: ([\d.]+)% \((\d+) hits / (\d+) lookups\)")
    vram = re.compile(r"([\d,]+) MiB of VRAM free")
    kv = re.compile(r"KV streaming: ([\d.]+)% of (\d+) block reads hit VRAM")
    drafts_line = re.compile(r"suffix drafts: \d+ windows, (\d+) of (\d+) drafts accepted")
    loads = sum(1 for line in lines if "GiB at" in line and "loaded" in line)

    rows, cache_hits, vram_free, kv_hits = [], [], [], []
    sums = {"prompt_tokens": 0, "prefill_ms": 0, "gen_tokens": 0, "gen_ms": 0,
            "draft_ok": 0, "draft_n": 0, "cache_hits": 0, "cache_lookups": 0,
            "kv_blocks": 0, "kv_hits": 0}
    for line in lines:
        m = request.search(line)
        if m:
            rows.append({"prompt": int(m.group(1)), "reused": int(m.group(2)),
                         "gen": int(m.group(6)), "gen_ms": int(m.group(7)),
                         "gen_tps": float(m.group(8)), "prefill_tps": float(m.group(5)),
                         "draft_ok": int(m.group(9)), "draft_n": int(m.group(10))})
            sums["prompt_tokens"] += int(m.group(1))
            sums["prefill_ms"] += int(m.group(4))
            sums["gen_tokens"] += int(m.group(6))
            sums["gen_ms"] += int(m.group(7))
            sums["draft_ok"] += int(m.group(9))
            sums["draft_n"] += int(m.group(10))
        m = cache.search(line)
        if m:
            cache_hits.append(float(m.group(1)))
            sums["cache_hits"] += int(m.group(2))
            sums["cache_lookups"] += int(m.group(3))
        m = vram.search(line)
        if m:
            vram_free.append(int(m.group(1).replace(",", "")))
        m = kv.search(line)
        if m:
            kv_hits.append(float(m.group(1)))
            sums["kv_blocks"] += int(m.group(2))
            sums["kv_hits"] += int(round(float(m.group(1)) / 100 * int(m.group(2))))
        m = drafts_line.search(line)
        if m:
            # The per-request `drafts accepted A of B` and the per-window summary
            # are the same measurement; the summary is only used when no request
            # line carried it, so the total is never double-counted.
            pass
    if not rows:
        return {"available": True, "log": str(path), "requests": 0,
                "reason": "no request lines in the tail"}
    acceptance = [r["draft_ok"] / r["draft_n"] for r in rows if r["draft_n"]]
    longest = max(rows, key=lambda r: r["gen_ms"])
    return {
        "available": True, "log": str(path), "requests": len(rows),
        "generated_tokens": sum(r["gen"] for r in rows),
        "generation_seconds": round(sums["gen_ms"] / 1000, 1),
        # The totals behind every ratio the report prints, so each one can show
        # its own numerator and denominator (LJ's rule, 2026-10-13).
        "totals": sums,
        "prefill_seconds": round(sums["prefill_ms"] / 1000, 1),
        "aggregate_decode_tps": (round(sums["gen_tokens"] / (sums["gen_ms"] / 1000), 1)
                                 if sums["gen_ms"] else None),
        "aggregate_prefill_tps": (round(sums["prompt_tokens"] / (sums["prefill_ms"] / 1000))
                                  if sums["prefill_ms"] else None),
        "decode_tps_median": round(statistics.median(r["gen_tps"] for r in rows), 1),
        "decode_tps_min": round(min(r["gen_tps"] for r in rows), 1),
        "prefill_tps_median": round(statistics.median(r["prefill_tps"] for r in rows)),
        "draft_acceptance_median": (round(100 * statistics.median(acceptance))
                                    if acceptance else None),
        "expert_cache_median": (round(statistics.median(cache_hits), 1)
                                if cache_hits else None),
        "expert_cache_min": round(min(cache_hits), 1) if cache_hits else None,
        "kv_vram_median": (round(statistics.median(kv_hits), 1) if kv_hits else None),
        "vram_free_mib_min": min(vram_free) if vram_free else None,
        "reused_requests": sum(1 for r in rows if r["reused"]),
        "longest": {"prompt": longest["prompt"], "generated": longest["gen"],
                    "seconds": round(longest["gen_ms"] / 1000, 1),
                    "tps": longest["gen_tps"]},
        "biggest_prompt": max(r["prompt"] for r in rows),
        "model_loads_in_log": loads,
    }


def _loop_cost(state_dir: Path, principal: str, since: str) -> dict:
    """The loop's *own* tokens and GPU seconds in the window.

    Not the report's totals: those include the Hermes agent jobs' 34 M tokens,
    and dividing that by the loop's packs produced "2,639,025 tokens per pack" —
    a true division of the wrong numerator (measured 2026-10-13). Efficiency is
    only meaningful within one workload.
    """
    path = state_dir / principal / "loop" / "transcripts" / "model_calls.jsonl"
    tokens, seconds, calls = 0, 0.0, 0
    if not path.is_file():
        return {"tokens": 0, "seconds": 0.0, "calls": 0}
    for row, reason in _rows(path):
        if reason or row is None:
            continue
        if str(row.get("at", ""))[:10] < since:
            continue
        calls += 1
        value = row.get("total_tokens")
        if isinstance(value, (int, float)):
            tokens += int(value)
        latency = row.get("latency_s")
        if isinstance(latency, (int, float)):
            seconds += float(latency)
    return {"tokens": tokens, "seconds": round(seconds, 1), "calls": calls}


def _loop_packs(state_dir: Path, principal: str, since: str) -> dict:
    """What the agentic loop actually finished in the window, from its receipts.

    Tokens without packs are not an answer to "is this worth it": the useful
    number is tokens and GPU-seconds *per pack filled*.
    """
    path = state_dir / principal / "loop" / "receipts.jsonl"
    if not path.is_file():
        return {"packs": 0, "seconds": 0.0, "actions": 0}
    packs, seconds, actions = set(), 0.0, 0
    # The shared reader: physical lines, never `splitlines()` (2026-10-13).
    for line in jsonl_io.lines(path):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("outcome") != "acted" or str(row.get("at", ""))[:10] < since:
            continue
        actions += 1
        packs.add(row.get("pack") or row.get("verification") or row.get("at"))
        seconds += float(row.get("seconds") or 0.0)
    return {"packs": len(packs), "seconds": round(seconds, 1), "actions": actions}


def render_markdown(stats: dict, engine: dict, *, principal: str | None = None,
                    loop: dict | None = None) -> str:
    """The inference report: what we asked for, what it cost, what the box said.

    Three questions, in the order a person asks them: how much did we spend, on
    what; how efficient was it per unit of work; and was the engine healthy while
    it did it.
    """
    rows = stats["rows"]
    calls = sum(r["calls"] for r in rows)
    tokens = sum(r["total_tokens"] for r in rows)
    unmetered = sum(r["unmetered"] for r in rows)
    title = f"# Inference report — {principal}" if principal else "# Inference report"
    out = [title, "",
           f"_Window: last {stats['window_days']} days (since {stats['since']}) · "
           f"generated {stats['generated_at']}_", ""]
    out += ["## What we spent", "",
            f"- **{calls:,} calls**, **{tokens:,} tokens**"
            + (f", **{unmetered:,} calls unmetered** ({_pct(unmetered, calls)} — "
               "rows with no token counts, so the total is a floor)"
               if unmetered else ""),
            ""]
    out += ["| source | provider | model | calls | prompt | completion | total | unmetered | mean s |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        # Every ratio carries its own numerator and denominator (LJ, 2026-10-13):
        # a mean is a division, so the cell shows the two numbers divided.
        mean = (f"{_fmt(r['mean_latency_s'])} "
                f"(={_fmt(round(r['latency_s']))} s/{r['latency_calls']})"
                if r["mean_latency_s"] is not None else "—")
        out.append(f"| {r['source']} | {r['provider']} | `{r['model']}` | {r['calls']:,} | "
                   f"{r['prompt_tokens']:,} | {r['completion_tokens']:,} | "
                   f"{r['total_tokens']:,} | {r['unmetered']:,} | {mean} |")
    if not rows:
        out.append("| _no calls in the window_ | | | | | | | | |")
    out.append("")

    if loop and loop["packs"]:
        # The loop's own workload only: its tokens and its GPU seconds, divided
        # by the packs it filled.
        loop_tokens = loop.get("tokens") or 0
        loop_seconds = loop.get("seconds") or 0.0
        out += ["## What it bought", "",
                f"- The agentic loop filled **{loop['packs']} pack(s)** using "
                f"**{loop_tokens:,} tokens** in **{loop_seconds:,.0f} s of model "
                f"time** ({loop.get('calls', 0)} calls).",
                f"- That is **{loop_tokens / max(1, loop['packs']):,.0f} tokens per pack** "
                f"(={loop_tokens:,} tok/{loop['packs']} packs) and "
                f"**{loop_seconds / max(1, loop['packs']):,.0f} s per pack** "
                f"(={loop_seconds:,.0f} s/{loop['packs']} packs) — the numbers to move "
                "if the loop should go faster.",
                ""]

    out += ["## What the engine said", ""]
    if not engine.get("available"):
        out.append(f"- _unavailable_: {engine.get('reason')}")
    elif not engine.get("requests"):
        # The log was read but its tail carried no request lines (a restart onto
        # a fresh log, a rotated file). Report that shape instead of KeyError-ing
        # on metrics that do not exist in it.
        out.append(f"- _nothing measured_: {engine.get('reason') or 'no request lines in the tail'}")
    else:
        t = engine.get("totals") or {}
        gen_s = engine["generation_seconds"]
        pre_s = engine.get("prefill_seconds")
        # Every ratio below shows the two numbers it divides (LJ, 2026-10-13:
        # "where we measure 2 things, and then we report the ratio … report the
        # numbers in the numerator and denominator in parenthesis"). A median and
        # an aggregate are different facts, so both are shown where they differ.
        # Optional metrics go through _metric(): a class of line missing from the
        # tail is "n/a", never a fabricated number and never a TypeError.
        out += [
            f"- **{engine['requests']} requests**, {engine['generated_tokens']:,} tokens "
            f"generated in {gen_s:,.0f} s of GPU time.",
            f"- Decode **{_metric(engine.get('decode_tps_median'))} tok/s** median"
            + (f", {_metric(engine.get('aggregate_decode_tps'))} tok/s aggregate "
               f"(={t.get('gen_tokens', 0):,} tok/{gen_s:,.1f} s)" if gen_s else "")
            + f" (min {_metric(engine.get('decode_tps_min'))} tok/s).",
            f"- Prefill **{_metric(engine.get('prefill_tps_median'))} tok/s** median"
            + (f", {_metric(engine.get('aggregate_prefill_tps'), spec=',')} tok/s aggregate "
               f"(={t.get('prompt_tokens', 0):,} tok/{pre_s:,.1f} s)"
               if pre_s else "") + ".",
            f"- Speculative drafts accepted **{_metric(engine.get('draft_acceptance_median'), suffix='%')}** "
            f"(={t.get('draft_ok', 0):,} of {t.get('draft_n', 0):,} drafts).",
            f"- Expert cache **{_metric(engine.get('expert_cache_median'), suffix='%')}** median, "
            f"{_pct(t.get('cache_hits', 0), t.get('cache_lookups', 0), decimals=1)} overall "
            f"(={t.get('cache_hits', 0):,} hits / {t.get('cache_lookups', 0):,} lookups; "
            f"min {_metric(engine.get('expert_cache_min'), suffix='%')}).",
            f"- KV in VRAM **{_metric(engine.get('kv_vram_median'), suffix='%')}** median, "
            f"{_pct(t.get('kv_hits', 0), t.get('kv_blocks', 0), decimals=1)} overall "
            f"(={t.get('kv_hits', 0):,} of {t.get('kv_blocks', 0):,} block reads).",
            (f"- **{engine['vram_free_mib_min']:,} MiB VRAM free** at the tightest point "
             "— this is why the tick takes a run lock and ticks are sequential."
             if engine.get("vram_free_mib_min") is not None else
             "- **VRAM headroom not recorded**: the engine log's tail carries no "
             "`MiB of VRAM free` line, so it is left out rather than guessed."),
            # LJ's shape exactly: the ratio first, then what it divided.
            f"- Longest single request: {engine['longest']['tps']} tok/s "
            f"(={engine['longest']['generated']:,} tok/{engine['longest']['seconds']} s); "
            f"largest prompt {engine['biggest_prompt']:,} tokens.",
            f"- {engine['reused_requests']} of {engine['requests']} requests reused KV "
            f"(the ones that could); the log records {engine['model_loads_in_log']} "
            "model load(s), so it spans more than one server run.",
            "",
            f"_Source: `{engine['log']}` (host-local; the tail is read, not a dated "
            "window — the engine's log has no timestamps)._",
        ]
    out += ["", "## What this report cannot say", "",
            "- **Cost.** Neither provider fills `cost_usd`, so spend is not shown "
            "rather than invented.",
            "- **The crawl's tokens, which is most of the volume.** The relay "
            "returns **no `usage` field at all** (probed 2026-10-13: "
            "`getattr(response, 'usage', None)` is `None` for "
            "`opencode-go`/`deepseek-v4.1-flash`), and only 10 of lj's 7,019 crawl "
            "rows carry any accounting — the older ones have no char counts either, "
            "just the request/response payloads. Those calls count as calls, never "
            "as tokens, so every crawl total here is a floor. An estimate from the "
            "payload lengths is possible and would be labelled as an estimate; it is "
            "not shown because a guess in a cost column is worse than a blank one.",
            "- **A dated engine window.** The engine's log has no timestamps, so its "
            "numbers describe the log's tail, not this window.",
            ""]
    return "\n".join(out) + "\n"


def write_report(*, days: int = 7, state_dir: Path | None = None,
                 principals: tuple[str, ...] | None = None,
                 today: date | None = None, engine: dict | None = None) -> list[Path]:
    """Write one inference report per principal into its published `logs/` area.

    `logs/` and not `wiki/reports/`: this is an operational artifact, and the
    crawl's reports stay a pure record of what the crawl decided.
    """
    state_dir = state_dir or config.STATE_DIR
    today = today or date.today()
    if principals is None:
        principals = tuple(sorted(p.name for p in state_dir.iterdir()
                                  if p.is_dir() and (p / "dataset").is_dir())) \
            if state_dir.is_dir() else ()
    engine = engine if engine is not None else engine_health()
    written: list[Path] = []
    for principal in principals:
        stats = aggregate(days=days, state_dir=state_dir, principals=(principal,),
                          today=today)
        loop = _loop_packs(state_dir, principal, stats["since"])
        loop.update(_loop_cost(state_dir, principal, stats["since"]))
        text = render_markdown(stats, engine, principal=principal, loop=loop)
        out = state_dir / principal / "logs" / f"inference-{today.isoformat()}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        written.append(out)
    return written


if __name__ == "__main__":
    raise SystemExit(_cli())

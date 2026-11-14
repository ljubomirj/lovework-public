"""Agent-friendliness report for the Digital Jobs MCP source.

The provider advertises an agent-first MCP server: public, anonymous, structured
records, cursor pagination, documented tools. That is a claim about how pleasant
it is for a machine to use. This module records how the claim held up on each
run — endpoint reachability, the offered tool surface, schema fidelity, cursor
behaviour, rate limiting, and whether the records are complete enough to act on.

Written after every crawl that ran the source, to `state/<p>/logs/` (a report,
not an email — nobody needs this in their inbox). `sources/digitaljobs.py`
collects the statistics; this renders the verdict.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# What the provider's skill.md promises (docs/23-digitaljobs-mcp.md).
DOCUMENTED_TOOLS = (
    "search_jobs",
    "list_ai_opportunities",
    "get_my_agent_profile",
    "create_application_draft",
    "create_service_listing_draft",
    "get_draft_status",
    "cancel_draft",
)
# The tools we actually need (the rest are OAuth-only, so their absence
# anonymously is honest, not a defect).
TOOLS_WE_USE = ("search_jobs", "list_ai_opportunities")
DOCUMENTED_RESPONSE_KEYS = (
    "jobs", "total", "returned", "has_more", "next_cursor",
    "search_relaxed", "dropped_filters", "location_matched_by",
    "search_radius_miles", "source",
)
REPORT_PREFIX = "digitaljobs-friendliness"

BANDS = ((9.0, "excellent"), (7.0, "strong"), (5.0, "workable"), (3.0, "limited"))


def _ratio(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def score(stats: dict) -> tuple[float, str, list[str]]:
    """Score agent-friendliness 0–10 from the collected statistics.

    Transparent by construction: the report lists each component, so the number
    can be argued with rather than trusted.
    """
    notes: list[str] = []
    total = 0.0

    # 2.0 — availability: the surface answered at all.
    tools_ok = bool((stats.get("tools_list") or {}).get("ok"))
    transport_errors = (stats.get("http_errors") or {}).get("transport", 0)
    if tools_ok:
        total += 2.0
    else:
        notes.append("tools/list did not answer — agents cannot discover the surface")
    if transport_errors:
        total = max(0.0, total - min(1.0, 0.25 * transport_errors))
        notes.append(f"{transport_errors} transport-level failure(s)")

    # 1.0 — tool surface.
    offered = set((stats.get("tools_list") or {}).get("tools") or [])
    if offered & set(TOOLS_WE_USE):
        total += 1.0
    elif offered:
        notes.append("tools/list answered but not with the tools we need")
    else:
        notes.append("no tools offered")

    # 2.0 — schema fidelity: documented response keys.
    keys = set((stats.get("response_keys") or {}).keys())
    if keys:
        present = len(keys & set(DOCUMENTED_RESPONSE_KEYS))
        total += 2.0 * _ratio(present, len(DOCUMENTED_RESPONSE_KEYS))
        missing = [k for k in DOCUMENTED_RESPONSE_KEYS if k not in keys]
        if missing:
            notes.append("documented keys never seen: " + ", ".join(missing))
    else:
        notes.append("no structuredContent observed")

    # 1.0 — host validity: a record whose URL only the provider can reach is not
    # usable by an agent, however well-formed it looks.
    unusable = stats.get("unusable_hosts") or {}
    if unusable:
        # Defect-only: the other components already sum to 10, so a clean run must
        # not gain a point here — that would make the scale 11 and report a
        # provider as better than perfect.
        bad = sum(unusable.values())
        total = max(0.0, total - min(1.0, 0.5 * len(unusable)))
        notes.append(
            f"{bad} record(s) point at a non-public host: "
            + ", ".join(f"{h} x{n}" for h, n in sorted(unusable.items(), key=lambda kv: -kv[1]))
        )

    # 2.0 — record usability.
    records = stats.get("records", 0)
    if records:
        with_url = _ratio(stats.get("records_with_url", 0), records)
        with_title = _ratio(stats.get("records_with_title", 0), records)
        total += 1.2 * with_url + 0.8 * with_title
        if with_url < 0.99:
            notes.append(f"only {with_url:.0%} of records carry a URL")
    else:
        notes.append("no records returned")

    # 1.0 — pagination behaved (single-page completions are fine).
    searches = stats.get("searches") or []
    if searches and all(item.get("pages", 0) >= 1 for item in searches):
        total += 1.0
    elif searches:
        notes.append("a search returned no page at all")

    # 1.0 — rate-limit friendliness.
    requests = stats.get("requests", 0)
    if requests:
        limited = stats.get("rate_limited", 0)
        rate = _ratio(limited, requests)
        total += 1.0 if rate < 0.05 else 0.5 if rate < 0.2 else 0.0
        if limited:
            notes.append(
                f"{limited} rate-limit response(s) in {requests} request(s) "
                f"({rate:.0%})"
                + (" — Retry-After honoured" if stats.get("retry_after_seconds") else "")
            )

    # 1.0 — field richness.
    fields = stats.get("fields") or {}
    if records:
        from sources.digitaljobs import RECORD_FIELDS
        richness = sum(_ratio(fields.get(name, 0), records) for name in RECORD_FIELDS)
        total += _ratio(richness, len(RECORD_FIELDS))

    band = next((name for floor, name in BANDS if total >= floor), "poor")
    return round(total, 1), band, notes


def _table(rows: Iterable[tuple[str, str]]) -> list[str]:
    lines = ["| Item | Value |", "|---|---|"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    return lines


def build_report(
    stats: dict,
    *,
    principal: str,
    role: str = "general",
    run_id: str = "",
    report_path: str = "",
) -> str:
    """Render the markdown report."""
    value, band, notes = score(stats)
    tools = (stats.get("tools_list") or {})
    offered = list(tools.get("tools") or [])
    searches = stats.get("searches") or []
    fields = stats.get("fields") or {}
    records = stats.get("records", 0)

    lines = [
        "# Digital Jobs MCP — agent-friendliness report",
        "",
        f"_{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')} · "
        f"{principal.upper()}/{role}"
        + (f" · run `{run_id}`" if run_id else "") + "_",
        "",
        f"**Verdict: {band} ({value}/10).** " + (
            "The provider's agent-first claims held up on this run."
            if band in {"excellent", "strong"}
            else "The provider's agent-first claims only partly held up on this run."
        ),
        "",
        "## What we measured",
        "",
    ]
    lines += _table([
        ("HTTP attempts", f"{stats.get('requests', 0)} ({stats.get('ok', 0)} answered ok)"),
        ("Retries after HTTP 429", stats.get("retries", 0)),
        ("Backoff waited (s)", stats.get("backoff_seconds", 0.0)),
        ("Rate-limit responses", stats.get("rate_limited", 0)),
        ("Retry-After values seen", ", ".join(str(v) for v in stats.get("retry_after_seconds", [])) or "—"),
        ("Latency p50 / max (ms)", f"{stats.get('latency_ms_p50', '—')} / {stats.get('latency_ms_max', '—')}"),
        ("HTTP errors", json.dumps(stats.get("http_errors") or {})),
        ("Searches", len(searches)),
        ("Records", records),
        ("Distinct URLs", records - stats.get("duplicate_urls", 0)),
        ("Duplicate URLs across searches", stats.get("duplicate_urls", 0)),
        ("Relaxed searches", stats.get("relaxed", 0)),
    ])
    lines += ["", "## The MCP surface", ""]
    if tools.get("ok"):
        missing = [t for t in DOCUMENTED_TOOLS if t not in offered]
        extra = [t for t in offered if t not in DOCUMENTED_TOOLS]
        lines += [
            f"`tools/list` answered with **{len(offered)} tool(s)**: {', '.join('`'+t+'`' for t in offered) or '—'}",
            "",
            f"- Documented but not offered anonymously: "
            + (", ".join(f"`{t}`" for t in missing) if missing else "none"),
            f"- Offered but undocumented: " + (", ".join(f"`{t}`" for t in extra) if extra else "none"),
        ]
        if missing:
            lines += [
                "",
                "_Some of those are the OAuth-only write tools, which are expected"
                " to be absent without an account — honest disclosure rather than a"
                " gap. What matters for us is that the anonymous search tools are"
                " present._",
            ]
        else:
            lines += [
                "",
                "_The whole documented surface was offered without credentials,"
                " including the write tools we deliberately never call._",
            ]
    else:
        lines.append(f"`tools/list` did not answer: {tools.get('error') or 'unknown error'}")
    lines += ["", "## Schema fidelity", ""]
    keys = stats.get("response_keys") or {}
    if keys:
        lines += _table(
            (f"`{key}`", f"seen in {keys[key]} response(s)" if key in keys else "**never seen**")
            for key in DOCUMENTED_RESPONSE_KEYS
        )
    else:
        lines.append("No `structuredContent` observed.")
    lines += ["", "## Pagination", ""]
    if searches:
        lines += _table(
            (f"`{item.get('name')}` ({item.get('feed')})",
             f"{item.get('returned', 0)} listing(s) over {item.get('pages', 0)} page(s)")
            for item in searches
        )
    else:
        lines.append("No searches ran.")
    lines += ["", "## Record quality", ""]
    if records:
        from sources.digitaljobs import RECORD_FIELDS
        lines += _table(
            (f"`{name}`", f"{fields.get(name, 0)} / {records} ({_ratio(fields.get(name, 0), records):.0%})")
            for name in RECORD_FIELDS
        )
    else:
        lines.append("No records to inspect.")
    lines += ["", "## Verdict and what would make them friendlier", ""]
    if notes:
        lines += [f"- {note}" for note in notes]
        lines.append("")
        lines.append(
            "Each item above is something the provider could fix (a documented key"
            " that never appears, a URL-less record, a rate limit with no"
            " Retry-After) or something we could adapt to (tighter queries, fewer"
            " searches per run)."
        )
    else:
        lines.append("- Nothing to complain about on this run.")
    lines.append("")
    return "\n".join(lines)


def write_report(
    runtime,
    stats: dict,
    *,
    principal: str,
    role: str = "general",
    run_id: str = "",
    report_path: str = "",
) -> Path:
    """Write the report under the principal's logs directory."""
    out_dir = Path(runtime.wiki_root).parent / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    path = out_dir / f"{REPORT_PREFIX}-{stamp}.md"
    path.write_text(
        build_report(stats, principal=principal, role=role, run_id=run_id, report_path=report_path),
        encoding="utf-8",
    )
    # A stable pointer for the dashboard/tail without globbing.
    latest = out_dir / f"{REPORT_PREFIX}-latest.md"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path

"""
Per-source post-crawl report.

Every run writes `wiki/reports/<ts>-<principal>-<role>-report.md`, which is a flat
list of *findings* with no answer to a different question: how did each **source**
perform? Until now only one source had that answer — `digitaljobs_report.py`, and
only for its provider-facing claims (tool surface, schema keys, 429 behaviour).

This report covers every source on the axes the crawl already records:

- **Yield** — jobs known, and how many were first seen in the last N days versus
  the N days before that. A source that quietly goes to zero is the failure mode
  that hid the Jack & Jill drop pausing for weeks (2026-08) and the Gmail
  categories that never landed in a label (2026-09).
- **Lifecycle** — the registry's status mix per source (new / still_open /
  long_lasting / disappeared). A source with almost everything *disappeared* is
  republishing rather than finding.
- **Verdict mix** — what the matcher made of what each source produced, from the
  assessment ledger (which carries `source`). A source that yields a lot and
  never a GO is wasting LLM calls.
- **The mailbox's per-site breakdown** — `gmail_<principal>_jobs` is one source
  containing every alert site (LinkedIn, TotalJobs, CWJobs, JobServe, Lensa,
  TalentSource, Rec-London, JohnsonJobs, CV-Library, and the Jack & Jill drop).
  The registry cannot see past the mailbox, so this report reads the email
  archive's `category` field and reports each site separately.

No LLM calls and no network: the registry, the dataset ledger, the email archive
and the run context already hold all of it. Output goes to
`state/<principal>/logs/sources-<ts>.md` (+ `-latest.md`), a report rather than an
email.
"""

import json
import jsonl_io
import logging
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WINDOW_DAYS = int(os.getenv("LOVEWORK_SOURCE_REPORT_WINDOW_DAYS", "30"))
# A source needs this much prior activity before a zero week counts as silence
# rather than a source that was never productive.
STARVATION_FLOOR = int(os.getenv("LOVEWORK_SOURCE_REPORT_STARVATION_FLOOR", "5"))
# A source that keeps producing but collapses is the harder case: the Gmail
# source fell 162 -> 32 new jobs in a window and looked healthy to a zero-test.
COLLAPSE_RATIO = float(os.getenv("LOVEWORK_SOURCE_REPORT_COLLAPSE_RATIO", "0.25"))

_GOOD_DECISIONS = ("GO", "MAYBE")
_STATUS_ORDER = ("new", "still_open", "long_lasting", "disappeared",
                 "not_listed_in_scope")


def _as_day(value: object) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def collect_registry(csv_path: Path, today: date, window_days: int) -> Dict[str, dict]:
    """Per-source rows and lifecycle status from the job registry."""
    import csv as _csv

    cutoff = today - timedelta(days=window_days)
    prev_cutoff = cutoff - timedelta(days=window_days)
    rows: Dict[str, dict] = defaultdict(
        lambda: {"known": 0, "status": Counter(), "new_window": 0, "prev_window": 0}
    )
    path = Path(csv_path)
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            source = (row.get("source") or "unknown").strip() or "unknown"
            entry = rows[source]
            entry["known"] += 1
            entry["status"][(row.get("status") or "").strip()] += 1
            first_seen = _as_day(row.get("first_seen"))
            if first_seen is None:
                continue
            if first_seen >= cutoff:
                entry["new_window"] += 1
            elif first_seen >= prev_cutoff:
                entry["prev_window"] += 1
    return dict(rows)


def collect_verdicts(dataset_dir: Path, today: date, window_days: int) -> Dict[str, Counter]:
    """Per-source matcher verdicts from the assessment ledger."""
    cutoff = today - timedelta(days=window_days)
    verdicts: Dict[str, Counter] = defaultdict(Counter)
    path = Path(dataset_dir) / "assessments.jsonl"
    if not path.is_file():
        return {}
    for line in jsonl_io.lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = _as_day(row.get("observed_at")) or _as_day(row.get("first_seen"))
        if when is not None and when < cutoff:
            continue
        source = str(row.get("source") or "unknown")
        decision = str(row.get("decision") or "UNSCORED").upper() or "UNSCORED"
        verdicts[source][decision] += 1
    return dict(verdicts)


def collect_mailbox(
    sources_dir: Optional[Path], today: date, window_days: int
) -> Dict[Tuple[str, str], dict]:
    """Per-site breakdown of every archived mailbox, keyed by (label, category).

    The registry only knows `gmail_<principal>_jobs`, so the sites behind it —
    TotalJobs, CWJobs, JobServe, and the Jack & Jill drop — are separable here
    and nowhere else. Every label directory is read, not just the principal's
    alert label: the drop lives in its own archive (`Jack-and-Jill`) and is the
    highest-value email channel there is.
    """
    if not sources_dir:
        return {}
    gmail_root = Path(sources_dir) / "gmail"
    if not gmail_root.is_dir():
        return {}
    try:
        from sources.gmail_lj_jobs import iter_archived_emails, replay_archived_jobs
    except Exception:  # pragma: no cover - import guard
        return {}

    cutoff = today - timedelta(days=window_days)
    per_site: Dict[Tuple[str, str], dict] = defaultdict(
        lambda: {"emails": 0, "leads": 0, "recent_emails": 0, "last_seen": None}
    )
    for label_dir in sorted(p for p in gmail_root.iterdir() if p.is_dir()):
        label = label_dir.name
        for record in iter_archived_emails(gmail_root.parent, label):
            category = str(record.get("category") or "unknown")
            entry = per_site[(label, category)]
            entry["emails"] += 1
            # `archived_at` is ISO; the archived `date` header is RFC2822 and
            # ends in a "(UTC)" suffix that naive parsing chokes on.
            when = _as_day(record.get("archived_at"))
            if when is not None:
                if entry["last_seen"] is None or when > entry["last_seen"]:
                    entry["last_seen"] = when
                if when >= cutoff:
                    entry["recent_emails"] += 1
        try:
            for job in replay_archived_jobs(gmail_root.parent, label):
                per_site[(label, str(job.get("category") or "unknown"))]["leads"] += 1
        except Exception as e:
            logger.warning(f"[source-report] replay failed for {label}: {e}")
    return dict(per_site)


def _attention(source: str, row: dict) -> Optional[str]:
    """A source that was productive and has gone quiet — or collapsed.

    Silence is the Jack & Jill failure (a drop that paused for weeks). Collapse
    is the subtler one: still producing, so a zero-test passes, but at a
    fraction of before.
    """
    prev = int(row.get("prev_window", 0) or 0)
    new = int(row.get("new_window", 0) or 0)
    if prev < STARVATION_FLOOR:
        return None
    if new == 0:
        return f"silent: {prev} new job(s) in the previous window, 0 since"
    if new < prev * COLLAPSE_RATIO:
        drop = 100 - (100 * new // prev)
        return (
            f"thinning: {new} new job(s) this window against {prev} in the previous "
            f"one (−{drop}%)"
        )
    return None


def compose(
    *,
    principal: str,
    role: str,
    run_id: str,
    today: date,
    window_days: int,
    registry: Dict[str, dict],
    verdicts: Dict[str, Counter],
    mailbox: Dict[Tuple[str, str], dict],
    mailbox_label: Optional[str],
    sources_this_run: Optional[List[str]] = None,
) -> str:
    """The report: what needs attention first, then the per-source tables."""
    start = today - timedelta(days=window_days)
    names = sorted(set(registry) | set(verdicts))
    alerts = []
    for source in names:
        row = registry.get(source, {})
        reason = _attention(source, row)
        if reason:
            alerts.append(f"- **{source}** — {reason}")

    lines = [
        f"# Sources — post-crawl report, {start.isoformat()} to {today.isoformat()}",
        "",
        f"_Principal {principal} · role {role} · run `{run_id or 'n/a'}` · "
        f"window {window_days} days_",
        "",
    ]
    if sources_this_run:
        lines += [f"**Crawled this run:** {', '.join(sources_this_run)}", ""]
    lines += ["## Needs attention", ""]
    lines += alerts or ["- Nothing: every source that was productive is still producing."]
    lines += [
        "",
        "## Every source",
        "",
        "| Source | Known | New (window) | Prev window | Lifecycle | GO | MAYBE | FLAG | DROP |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for source in names:
        row = registry.get(source, {})
        status = row.get("status", Counter())
        lifecycle = ", ".join(
            f"{s}={status[s]}" for s in _STATUS_ORDER if status.get(s)
        ) or "—"
        v = verdicts.get(source, Counter())
        lines.append(
            f"| `{source}` | {row.get('known', 0)} | {row.get('new_window', 0)} "
            f"| {row.get('prev_window', 0)} | {lifecycle} | {v.get('GO', 0)} "
            f"| {v.get('MAYBE', 0)} | {v.get('FLAG', 0)} | {v.get('DROP', 0)} |"
        )

    if mailbox:
        lines += [
            "",
            "## Inside the mailboxes",
            "",
            "One registry source can hide many senders: the alert label holds every "
            "job board, and the Jack & Jill drop lives in its own archive. The "
            "registry cannot separate them; the email archive can.",
            "",
            "| Mailbox | Site (email category) | Emails | In window | Leads parsed | Last email |",
            "|---|---|---:|---:|---:|---|",
        ]
        for (label, category) in sorted(
            mailbox, key=lambda k: (-mailbox[k]["leads"], k[0], k[1])
        ):
            row = mailbox[(label, category)]
            lines.append(
                f"| `{label}` | `{category}` | {row['emails']} | {row['recent_emails']} "
                f"| {row['leads']} | {row['last_seen'] or '—'} |"
            )

    productive = [
        s for s in names
        if verdicts.get(s, Counter()).get("GO", 0) or verdicts.get(s, Counter()).get("MAYBE", 0)
    ]
    if productive:
        lines += [
            "",
            "## What each source is actually worth",
            "",
            "Ranked by GO+MAYBE verdicts in the window — the yield that justifies "
            "the crawler and the LLM calls.",
            "",
            "| Source | GO+MAYBE | DROP | Score |",
            "|---|---:|---:|---|",
        ]
        ranked = sorted(
            names,
            key=lambda s: -(verdicts.get(s, Counter()).get("GO", 0)
                            + verdicts.get(s, Counter()).get("MAYBE", 0)),
        )
        for source in ranked:
            v = verdicts.get(source, Counter())
            good = v.get("GO", 0) + v.get("MAYBE", 0)
            if not good:
                continue
            total = sum(v.values()) or 1
            lines.append(
                f"| `{source}` | {good} | {v.get('DROP', 0)} | {100 * good // total}% GO/MAYBE |"
            )
    lines.append("")
    return "\n".join(lines)


def write_report(
    runtime,
    *,
    principal: str,
    role: str = "general",
    run_id: str = "",
    today: Optional[date] = None,
    window_days: int = WINDOW_DAYS,
) -> Path:
    """Write `logs/sources-<ts>.md` (+ `-latest.md`); return the dated path."""
    today = today or date.today()
    mailbox_label = getattr(getattr(runtime, "gmail_mailbox", None), "label", None)
    sources_this_run = None
    context_path = Path(runtime.cache_dir) / "last_run_context.json"
    if context_path.is_file():
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            sources_this_run = list(context.get("sources") or []) or None
        except (OSError, json.JSONDecodeError):
            sources_this_run = None

    registry = collect_registry(Path(runtime.cache_dir) / "jobs.csv", today, window_days)
    verdicts = collect_verdicts(runtime.dataset_dir, today, window_days)
    mailbox = collect_mailbox(
        getattr(runtime, "sources_dir", None), today, window_days
    )
    text = compose(
        principal=principal,
        role=role,
        run_id=run_id,
        today=today,
        window_days=window_days,
        registry=registry,
        verdicts=verdicts,
        mailbox=mailbox,
        mailbox_label=mailbox_label,
        sources_this_run=sources_this_run,
    )

    logs = Path(runtime.wiki_root).parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = logs / f"sources-{stamp}.md"
    path.write_text(text, encoding="utf-8")
    latest = logs / "sources-latest.md"
    latest.write_text(text, encoding="utf-8")
    return path

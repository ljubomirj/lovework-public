"""Model-call transcripts — the prerequisite for exact playback (WP5).

Exact playback needs two things, and this module supplies the second: a complete
frozen execution specification (the seals already have that) and **the recorded
responses matching the exact requests**. Without transcripts, `reconstruct` can
only ever invalidate itself honestly.

Design points that matter:

* **Match on a digest of the full request, store a bounded payload.** The digest
  is computed over the *entire* untruncated request, so playback matches exactly
  even when a payload is capped; a capped payload is marked `truncated` with the
  original length rather than quietly shortened.
* **Stamp with the operation's run id** (`LOVEWORK_OUTER_RUN_ID`) — the same id a
  seal carries. The assessment ledger's rows carry *pipeline* ids that do not
  join to a seal (see docs/28), so stamping transcripts with the operation id is
  what makes "this seal's calls" answerable at all.
* **On by default, switchable off.** A transcript that does not exist cannot be
  replayed; the cost is disk, and the cap keeps it bounded. Set
  `LOVEWORK_RECORD_MODEL_CALLS=0` to disable.
* **Row schema.** Every row carries the WP5 playback keys (`call_id`,
  `run_id`, `call_index`, `at`, `model`, `provider`, `api_style`, `label`,
  `request_digest`, `request`, `response`, `finish_reason`) plus the
  accounting keys added 2026-10-12: `latency_s` (wall seconds for the
  successful attempt, always measured by the client), `prompt_tokens` /
  `completion_tokens` / `total_tokens` (from the provider's `usage` object
  when it sends one, else null — never zero-filled), `prompt_chars` /
  `completion_chars` (always computed locally from the messages/content),
  `tokens_per_second` (the provider's reported speed when present, else
  `completion_tokens / latency_s`, with `tps_source` set to `"reported"` or
  `"derived"`), and `cost_usd` (when the provider reports a cost, else
  null). Rows written before 2026-10-12 simply lack the new keys; readers
  treat a missing key as unknown, never as zero.
* **Read path.** `summarize_usage()` aggregates one ledger per
  (provider, model): requests, mean latency, token totals, tok/s where
  known, spend where known. `status.py --llm-usage` prints it.
"""
from __future__ import annotations
import jsonl_io

import hashlib
import json
import logging
from contextlib import contextmanager
import os
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

LEDGER_NAME = "model_calls.jsonl"

#: Where this run's transcripts go. A *run-scoped* value, not the global config:
#: a dry run replaces the runtime's dataset dir with a temp preview root, and a
#: recorder reading `config.DATASET_DIR` would write into live state while the
#: other ledgers went to the preview — the one ledger that must stay silent.
_ACTIVE_DATASET: ContextVar[Optional[Path]] = ContextVar(
    "lovework_transcript_dataset", default=None)


@contextmanager
def active_dataset_dir(path: Optional[Path]):
    """Bind the dataset dir for the duration of a run."""
    token = _ACTIVE_DATASET.set(Path(path) if path is not None else None)
    try:
        yield
    finally:
        _ACTIVE_DATASET.reset(token)


def set_active_dataset(path) -> None:
    """Pin the dataset dir for the rest of this process, with no context to exit.

    For a process that *is* one principal's run — the tick wrapper invokes
    `tick.py --profile <p>` once per principal — so its model calls can be
    attributed to that principal rather than falling back to `config.DATASET_DIR`
    (lj) with `run_id: unattributed` (measured 2026-10-21: vj's and kj's loop calls
    were landing in lj's ledger).
    """
    _ACTIVE_DATASET.set(Path(path) if path is not None else None)


def resolve_dataset_dir() -> Optional[Path]:
    """The run's dataset dir, falling back to the configured one."""
    active = _ACTIVE_DATASET.get()
    if active is not None:
        return active
    try:
        import config

        return getattr(config, "DATASET_DIR", None)
    except Exception:
        return None
RECORD_ENV = "LOVEWORK_RECORD_MODEL_CALLS"
MAX_CHARS_ENV = "LOVEWORK_TRANSCRIPT_MAX_CHARS"
DEFAULT_MAX_CHARS = 20000


def recording_enabled() -> bool:
    return os.getenv(RECORD_ENV, "1").strip().lower() not in ("0", "false", "no", "")


def _max_chars() -> int:
    try:
        return max(0, int(os.getenv(MAX_CHARS_ENV, str(DEFAULT_MAX_CHARS))))
    except ValueError:
        return DEFAULT_MAX_CHARS


def canonical_request(messages: Iterable[dict], *, temperature=None,
                      max_tokens=None, response_format=None,
                      reasoning_effort=None) -> dict:
    """The request shape both the recorder and a replay must agree on.

    Deliberately the *semantic* arguments, not the provider-shaped kwargs the
    wire call uses: a digest over provider kwargs can only be reproduced by
    reproducing that provider's request builder exactly, which is how a replay
    ends up unable to match its own recording.
    """
    return {
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": repr(response_format),
        "reasoning_effort": reasoning_effort,
    }


def request_digest(messages: Iterable[dict], request_kwargs: dict) -> str:
    """A digest of the *exact* request: messages plus every generation parameter.

    Computed over the full untruncated content, so a capped stored payload still
    matches exactly on replay.
    """
    payload = json.dumps({"messages": list(messages), "params": request_kwargs},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bounded(text: str, cap: int) -> dict:
    """Store text with an explicit truncation marker, never a silent cut."""
    if cap and len(text) > cap:
        return {"text": text[:cap], "truncated": True, "original_chars": len(text)}
    return {"text": text, "truncated": False, "original_chars": len(text)}


def record_call(*, dataset_dir: Optional[Path] = None, run_id: str,
                model: str, provider: str,
                api_style: str, messages: list[dict], canonical: dict,
                content: str, finish_reason: str = "", label: str = "",
                call_index: int = 0, latency_s: Optional[float] = None,
                prompt_tokens: Optional[int] = None,
                completion_tokens: Optional[int] = None,
                total_tokens: Optional[int] = None,
                prompt_chars: Optional[int] = None,
                completion_chars: Optional[int] = None,
                tokens_per_second: Optional[float] = None,
                tps_source: Optional[str] = None,
                cost_usd: Optional[float] = None) -> Optional[dict]:
    """Append one call's request and response. Returns the row, or None if off.

    The accounting kwargs are all optional: old callers omit them and old rows
    lack them. A missing value stays null — never zero-filled, so "zero" keeps
    meaning "the provider reported zero" and null means "unknown".
    """
    if not recording_enabled():
        return None
    dataset_dir = Path(dataset_dir) if dataset_dir is not None else resolve_dataset_dir()
    if dataset_dir is None:
        return None
    cap = _max_chars()
    digest = request_digest(messages, canonical)
    if prompt_chars is None:
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if completion_chars is None:
        completion_chars = len(content)
    # Honest degradation: a provider-reported speed wins; otherwise derive
    # completion tokens per wall second when both are known.
    if tokens_per_second is None and completion_tokens and latency_s:
        try:
            tokens_per_second = completion_tokens / latency_s if latency_s > 0 else None
        except (TypeError, ZeroDivisionError):
            tokens_per_second = None
        else:
            if tokens_per_second is not None:
                tps_source = "derived"
    row = {
        "call_id": f"{run_id}:{call_index}:{digest[:12]}",
        "run_id": run_id,
        "call_index": call_index,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "provider": provider,
        "api_style": api_style,
        "label": label[:120],
        "request_digest": digest,
        "request": _bounded(json.dumps(canonical, sort_keys=True,
                                       ensure_ascii=False), cap),
        "response": _bounded(content, cap),
        "finish_reason": finish_reason,
        "latency_s": latency_s,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_chars": prompt_chars,
        "completion_chars": completion_chars,
        "tokens_per_second": tokens_per_second,
        "tps_source": tps_source,
        "cost_usd": cost_usd,
    }
    path = dataset_dir / LEDGER_NAME
    try:
        dataset_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        # Never break a crawl over bookkeeping — but say so, since a missing
        # transcript is a missing ability to replay.
        logger.warning("Could not record model call: %s", e)
        return None
    return row
class Transcripts:
    """Read one run's recorded calls, indexed by exact-request digest."""

    def __init__(self, dataset_dir: Path, run_id: str):
        self.path = dataset_dir / LEDGER_NAME
        self.run_id = run_id
        self._rows: list[dict] = []
        if self.path.is_file():
            for line in jsonl_io.lines(self.path):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("run_id") == run_id:
                    self._rows.append(row)
        self._by_digest: dict[str, list[dict]] = {}
        for row in self._rows:
            self._by_digest.setdefault(row["request_digest"], []).append(row)
        self._used: set[str] = set()

    @property
    def count(self) -> int:
        return len(self._rows)

    @property
    def truncated_payloads(self) -> int:
        return sum(1 for r in self._rows
                   if r["request"].get("truncated") or r["response"].get("truncated"))

    def lookup(self, digest: str) -> Optional[str]:
        """The recorded response for an exact request, or None.

        Returns None rather than guessing: a near-miss request is a *different*
        execution, and serving it would be fabrication.
        """
        rows = self._by_digest.get(digest)
        if not rows:
            return None
        row = rows[0]
        self._used.add(row["call_id"])
        return row["response"]["text"]

    def unused_calls(self) -> list[str]:
        return [r["call_id"] for r in self._rows if r["call_id"] not in self._used]

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "recorded_calls": self.count,
            "distinct_requests": len(self._by_digest),
            "truncated_payloads": self.truncated_payloads,
            "ledger": str(self.path),
        }

def _iter_ledger_rows(path: Path) -> Iterable[dict]:
    """Yield ledger rows, skipping blanks and corrupt lines.

    The live ledger has measured history of partial writes (docs/43 §9:
    a repair once left 50 fragments in 7,012 rows), so a reader that dies
    on one bad line makes the whole summary unavailable.
    """
    # Physical lines, via the shared reader: `splitlines()` also breaks on the
    # Unicode separators a JSON string may legally contain, and this ledger is
    # written with `ensure_ascii=False` (the class fixed across 16 readers on
    # 2026-10-13 — this one used a bare `text.splitlines()` and was missed).
    try:
        physical = jsonl_io.lines(path)
    except OSError:
        # A principal with no ledger yet is not an error: this reader feeds a
        # summary, and "no calls recorded" is a true answer. `jsonl_io.lines`
        # itself stays strict, so a *missing expected* file is still loud.
        return
    for line in physical:
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            logger.debug("Skipping unreadable model_calls row")
            continue


def summarize_usage(dataset_dir: Path) -> dict:
    """Aggregate accounting per (provider, model) over the whole ledger.

    Missing optional fields degrade honestly: token/spend totals count only
    rows that report them (`*_known` says how many), `mean_latency_s` counts
    only rows that measured it, and `tokens_per_second` is reported only
    when at least one row knows a speed. §4 cost arithmetic in
    docs/30-inference-chain-and-data-map.md reads these totals — the
    aggregation never zero-fills an unknown.
    """
    groups: dict[tuple[str, str], dict] = {}
    total_rows = 0
    skipped_rows = 0
    for row in _iter_ledger_rows(Path(dataset_dir) / LEDGER_NAME):
        total_rows += 1
        try:
            key = (str(row.get("provider") or "unknown"),
                   str(row.get("model") or "unknown"))
        except Exception:
            skipped_rows += 1
            continue
        g = groups.setdefault(key, {
            "provider": key[0], "model": key[1], "requests": 0,
            "latency_s_total": 0.0, "latency_s_known": 0,
            "prompt_tokens": 0, "prompt_tokens_known": 0,
            "completion_tokens": 0, "completion_tokens_known": 0,
            "total_tokens": 0, "total_tokens_known": 0,
            "prompt_chars": 0, "completion_chars": 0,
            "cost_usd": 0.0, "cost_usd_known": 0,
            "tps_sum": 0.0, "tps_known": 0, "tps_reported": 0,
        })
        g["requests"] += 1
        lat = row.get("latency_s")
        if isinstance(lat, bool):
            lat = None
        if isinstance(lat, (int, float)) and lat >= 0:
            g["latency_s_total"] += float(lat)
            g["latency_s_known"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            val = row.get(field)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) and val >= 0:
                g[field] += val
                g[f"{field}_known"] += 1
        for field in ("prompt_chars", "completion_chars"):
            val = row.get(field)
            if isinstance(val, int) and val >= 0:
                g[field] += val
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and cost >= 0:
            g["cost_usd"] += float(cost)
            g["cost_usd_known"] += 1
        tps = row.get("tokens_per_second")
        if isinstance(tps, (int, float)) and tps > 0:
            g["tps_sum"] += float(tps)
            g["tps_known"] += 1
            if row.get("tps_source") == "reported":
                g["tps_reported"] += 1
    by_model = []
    for g in groups.values():
        # `latency_s_total` is *kept*: every ratio a report prints must be able to
        # show its own numerator and denominator (LJ, 2026-10-13), and a mean
        # whose total was popped cannot.
        lat_total = g["latency_s_total"]
        tps_total = g.pop("tps_sum")
        lat_known = g["latency_s_known"]
        tps_known = g["tps_known"]
        g["mean_latency_s"] = lat_total / lat_known if lat_known else None
        g["mean_tokens_per_second"] = tps_total / tps_known if tps_known else None
        g["aggregate_tokens_per_second"] = (
            g["total_tokens"] / lat_total if lat_total and g["total_tokens_known"] else None)
        del g["prompt_chars"], g["completion_chars"]
        by_model.append(g)
    by_model.sort(key=lambda g: (-g["requests"], g["provider"], g["model"]))
    return {
        "ledger": str(Path(dataset_dir) / LEDGER_NAME),
        "rows": total_rows,
        "skipped_rows": skipped_rows,
        "groups": len(by_model),
        "by_model": by_model,
    }


def render_usage_text(summary: dict) -> str:
    """One-line-per-(provider, model) totals for `status.py --llm-usage`."""
    lines = [f"# LLM usage — {summary['rows']} recorded call(s), "
             f"{summary['groups']} provider/model group(s)",
             ""]
    if not summary["by_model"]:
        lines.append("- no recorded calls")
        return "\n".join(lines) + "\n"
    for g in summary["by_model"]:
        lat = ("%.1fs" % g["mean_latency_s"]
               if g["mean_latency_s"] is not None else "unknown")
        toks: list[str] = []
        for field, short in (("prompt_tokens", "in"), ("completion_tokens", "out"),
                             ("total_tokens", "total")):
            if g[f"{field}_known"]:
                toks.append(f"{g[field]} {short} ({g[f'{field}_known']}/{g['requests']} rows)")
            else:
                toks.append(f"{short}: unknown")
        tps = ("%.0f tok/s (%d/%d rows%s)" % (
            g["mean_tokens_per_second"], g["tps_known"], g["requests"],
            ", provider-reported" if g["tps_reported"] else ", derived")
            if g["mean_tokens_per_second"] is not None else "tok/s: unknown")
        spend = ("$%.4f (%d/%d rows)" % (
            g["cost_usd"], g["cost_usd_known"], g["requests"])
            if g["cost_usd_known"] else "spend: unknown")
        # Every ratio shows the two numbers it divides (LJ, 2026-10-13): the
        # mean, the mean speed and the aggregate speed are all divisions, so each
        # carries its own numerator and denominator.
        mean_with_parts = lat
        if g["latency_s_known"]:
            mean_with_parts = (f"{lat} (={g['latency_s_total']:.1f} s/"
                               f"{g['latency_s_known']} calls)")
        tps_with_parts = tps
        if g["mean_tokens_per_second"] is not None and g["latency_s_total"]:
            tps_with_parts = (f"{tps}; {g['aggregate_tokens_per_second']:.0f} tok/s "
                              f"aggregate (={g['total_tokens']:,} tok/"
                              f"{g['latency_s_total']:.1f} s)")
        lines.append(
            f"- {g['provider']} / {g['model']}: "
            f"{g['requests']} request(s), mean latency {mean_with_parts}, "
            f"{', '.join(toks)}, {tps_with_parts}, {spend}")
    return "\n".join(lines) + "\n"

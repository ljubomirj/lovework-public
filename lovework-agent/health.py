#!/usr/bin/env python3
"""Internal health: one metric registry, one snapshot, one variance.

The detectors already exist: `wiki_audit` (wiki classes), `monitor.signals`
(loop and scheduler), `tick.audit_receipts` (receipt integrity), `pack_index`
(pack coverage), `status`/`situation` (crawl and cockpit). This module does
**not** detect anything new. It names the healthy target for each fact, reads
the one authority that owns it, and computes the distance.

A metric is a contract:

    name        stable key (`wiki.defects`)
    subsystem   the part it belongs to (`wiki`)
    direction   lower / higher / band
    target      the good state
    scale       the value at which the error reaches 1
    weight      severity in the weighted mean
    source      the one function that owns the fact
    freshness   how long an observation is valid
    required    whether the deployment must observe it
    authority   who owns the target: agent / principal / history

Exit codes: 0 healthy, 1 defects (variance > 0), 2 a required metric is
unknown (blindness is not a pass), 3 no principal state.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

#: A wiki audit is ~10 s on lj's 2,166 pages; a tick must not pay that twice an
#: hour. The cache is the measurement, not a second audit.
WIKI_CACHE_TTL = 3600
RECEIPTS_CACHE_TTL = 3600
#: A cache older than this is not a measurement, it is a hint.
MAX_SOURCE_AGE = timedelta(hours=26)

#: Classes owned by the pack/lead metrics below. `wiki.defects` is the wiki
#: text/links/titles remainder, so the weighted mean cannot count one defect
#: twice.
PACK_LEAD_CLASSES = frozenset({"go_without_pack", "go_unprepared",
                               "lead_without_page"})


@dataclass(frozen=True)
class Metric:
    name: str
    subsystem: str
    kind: str                     # gauge / counter / age / bool / ratio
    direction: str                # lower / higher / band
    target: Any                   # float, bool, or (low, high)
    scale: float
    weight: float
    source: str
    freshness: timedelta
    required: bool = True
    authority: str = "agent"
    description: str = ""
    tolerance: float = 0.0

    def target_text(self) -> str:
        if isinstance(self.target, tuple):
            return f"{self.target[0]}..{self.target[1]}"
        return str(self.target)


#: The first slice. Each one is a fact an existing authority already reads.
METRICS: tuple[Metric, ...] = (
    Metric("loop.heartbeat_age_min", "loop", "age", "lower", 150.0, 120.0, 3.0,
           "monitor.signals", timedelta(hours=2),
           description="the newest heartbeat's age; the monitor's own 150-minute line"),
    Metric("loop.failed_receipts_24h", "loop", "counter", "lower", 0.0, 3.0, 3.0,
           "monitor.signals", timedelta(hours=2),
           description="failed receipts in the monitor's 24-hour window"),
    Metric("scheduler.failing_jobs", "scheduler", "counter", "lower", 0.0, 3.0, 3.0,
           "monitor.signals", timedelta(hours=2),
           description="scheduled jobs whose latest run did not complete"),
    Metric("wiki.defects", "wiki", "counter", "lower", 0.0, 10.0, 3.0,
           "wiki_audit.audit_wiki", timedelta(hours=26),
           description="repairable wiki text/link/title defects, pack/lead classes excluded"),
    Metric("packs.go_without_pack", "packs", "counter", "lower", 0.0, 2.0, 3.0,
           "wiki_audit.audit_wiki", timedelta(hours=26),
           description="current GO adverts with no pack at all"),
    Metric("packs.go_unprepared", "packs", "counter", "lower", 0.0, 2.0, 2.0,
           "wiki_audit.audit_wiki", timedelta(hours=26),
           description="current GO adverts whose pack carries no material"),
    Metric("leads.missing_page", "leads", "counter", "lower", 0.0, 10.0, 2.0,
           "wiki_audit.audit_wiki", timedelta(hours=26),
           description="judged adverts with no lead page"),
    Metric("receipts.audit_defects", "receipts", "counter", "lower", 0.0, 3.0, 2.0,
           "tick.audit_receipts", timedelta(hours=26),
           description="receipt evidence/target/duplicate defects"),
    Metric("repo.hub_lag", "repo", "counter", "lower", 0.0, 3.0, 2.0,
           "git (principal repo vs deploy hub)", timedelta(hours=2),
           required=False,
           description="commits in HEAD not yet in the principal hub"),
    Metric("deploy.code_dirty", "deploy", "counter", "lower", 0.0, 1.0, 2.0,
           "git status of the runtime tree", timedelta(hours=2),
           required=False,
           description="uncommitted code files outside state/ (the deploy gate)"),
    Metric("deploy.stamp_matches", "deploy", "bool", "higher", True, 1.0, 2.0,
           "deployed.json vs git HEAD", timedelta(hours=2),
           required=False,
           description="the deployed stamp names the checked-out commit"),
)

METRIC_BY_NAME = {m.name: m for m in METRICS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalise(metric: Metric, reading: dict) -> dict:
    """One raw observation → {value, error, status, ...}.

    `error` is the distance from the target, normalised to [0, 1]:
    * lower-is-better: (value - target - tolerance) / scale, floored at 0;
    * higher-is-better: (target - value) / scale, floored at 0;
    * band: distance outside [low, high] / scale.

    Unknown is not healthy: an applicable required metric with no observation
    has error 1. An inapplicable metric has status `n/a` and is excluded.
    """
    out = dict(reading)
    out.setdefault("value", None)
    out.setdefault("detail", "")
    out.setdefault("source_at", "")
    applicable = bool(reading.get("applicable", True))
    observed = bool(reading.get("observed", out.get("value") is not None))
    out["applicable"] = applicable
    out["observed"] = observed
    if not applicable:
        out.update(error=0.0, status="n/a")
        return out
    if not observed or out["value"] is None:
        out.update(error=1.0, status="unknown")
        return out
    value = out["value"]
    if metric.kind == "bool" or isinstance(metric.target, bool):
        ok = bool(value) == bool(metric.target)
        out.update(error=0.0 if ok else 1.0, status="ok" if ok else "bad")
        return out
    try:
        number = float(value)
    except (TypeError, ValueError):
        out.update(error=1.0, status="unknown")
        return out
    tolerance = max(0.0, float(getattr(metric, "tolerance", 0.0) or 0.0))
    if metric.direction == "band" and isinstance(metric.target, tuple):
        low = float(metric.target[0]) - tolerance
        high = float(metric.target[1]) + tolerance
        if low <= number <= high:
            error = 0.0
        elif number < low:
            error = _clamp((low - number) / max(metric.scale, 1e-9))
        else:
            error = _clamp((number - high) / max(metric.scale, 1e-9))
    elif metric.direction == "higher":
        error = _clamp((float(metric.target) - tolerance - number)
                       / max(metric.scale, 1e-9))
    else:
        error = _clamp((number - float(metric.target) - tolerance)
                       / max(metric.scale, 1e-9))
    if error <= 0.0:
        status = "ok"
    elif error < 1.0:
        status = "warn"
    else:
        status = "bad"
    out.update(error=round(error, 6), status=status)
    return out


def build_snapshot(principal: str, readings: dict[str, dict], *,
                   at: str | None = None, previous: dict | None = None,
                   gaps: dict | None = None) -> dict:
    """The snapshot: every metric, the weighted variance, the worst, the rose."""
    at = at or _iso()
    metrics: dict[str, dict] = {}
    total_num = 0.0
    total_den = 0.0
    by_subsystem: dict[str, list[float]] = {}
    worst: list[dict] = []
    for metric in METRICS:
        reading = normalise(metric, readings.get(metric.name, {}))
        reading.update(
            subsystem=metric.subsystem,
            kind=metric.kind,
            direction=metric.direction,
            target=metric.target,
            tolerance=metric.tolerance,
            scale=metric.scale,
            weight=metric.weight,
            source=metric.source,
            authority=metric.authority,
        )
        metrics[metric.name] = reading
        if not reading["applicable"]:
            continue
        total_num += metric.weight * reading["error"]
        total_den += metric.weight
        by_subsystem.setdefault(metric.subsystem, []).append(
            metric.weight * reading["error"])
        if reading["error"] > 0:
            worst.append({"name": metric.name, "error": reading["error"],
                          "weight": metric.weight, "value": reading["value"],
                          "target": metric.target})
    variance = round(total_num / total_den, 6) if total_den else 0.0
    by = {name: round(sum(vals) / max(1e-9, sum(
        m.weight for m in METRICS if m.subsystem == name and
        metrics[m.name]["applicable"])), 6)
        for name, vals in by_subsystem.items()}
    worst.sort(key=lambda row: (-(row["weight"] * row["error"]), row["name"]))
    prev_errors = (previous or {}).get("errors") or {}
    rose = sorted(
        name for name, row in metrics.items()
        if row["applicable"] and row["error"] >
        float(prev_errors.get(name, row["error"])) + 0.01
    ) if prev_errors else []
    applicable = [m for m in METRICS if metrics[m.name]["applicable"]]
    observed = [m for m in applicable if metrics[m.name]["observed"]]
    required = [m for m in applicable if m.required]
    unknown = [m for m in required if not metrics[m.name]["observed"]]
    return {
        "principal": principal,
        "at": at,
        "metrics": metrics,
        "variance": {
            "total": variance,
            "by_subsystem": by,
            "worst": worst[:5],
            "rose": rose,
            "coverage": {
                "required": len(required),
                "applicable": len(applicable),
                "observed": len(observed),
                "unknown": len(unknown),
            },
        },
        "gaps": gaps or {},
    }


# ── collectors ───────────────────────────────────────────────────────────

def _reading(value=None, *, observed: bool = True, applicable: bool = True,
             source_at: str = "", detail: str = "") -> dict:
    return {"value": value, "observed": observed, "applicable": applicable,
            "source_at": source_at, "detail": detail}


def _cache_path(state_root: Path, name: str) -> Path:
    return state_root / "health" / name


def _read_cache(path: Path, ttl: int) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    at = _parse_iso(data.get("at"))
    if at is None or (_now() - at).total_seconds() > ttl:
        return None
    return data


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _wiki_audit(principal: str, state_root: Path, *, deep: bool,
                refresh: bool, write: bool) -> dict | None:
    cache = _cache_path(state_root, "wiki-audit.json")
    if not (deep or refresh):
        cached = _read_cache(cache, WIKI_CACHE_TTL)
        if cached:
            return cached
    try:
        import wiki_audit

        wiki_root = state_root / "wiki"
        if not wiki_root.is_dir():
            return None
        report = wiki_audit.audit_wiki(wiki_root)
        payload = {"at": _iso(), "classes": report.get("classes") or {},
                   "gaps": report.get("gaps") or {},
                   "total": report.get("total", 0),
                   "org_pages": report.get("org_pages", 0)}
        if write:
            _write_cache(cache, payload)
        return payload
    except Exception as exc:                  # a detector failure is unknown
        return {"at": _iso(), "error": f"{type(exc).__name__}: {exc}",
                "classes": {}, "gaps": {}, "total": None}


def _receipts_audit(principal: str, state_root: Path, *, deep: bool,
                    refresh: bool, write: bool) -> dict | None:
    cache = _cache_path(state_root, "receipts-audit.json")
    if not (deep or refresh):
        cached = _read_cache(cache, RECEIPTS_CACHE_TTL)
        if cached:
            return cached
    try:
        import tick

        report = tick.audit_receipts(principal, state_dir=state_root.parent)
        payload = {"at": _iso(), "checked": report.get("checked", 0),
                   "defects": report.get("defects") or [],
                   "corrected": report.get("corrected", 0),
                   "pruned": report.get("pruned", 0),
                   "moved": report.get("moved", 0)}
        if write:
            _write_cache(cache, payload)
        return payload
    except Exception as exc:
        return {"at": _iso(), "error": f"{type(exc).__name__}: {exc}",
                "defects": [], "checked": 0}


def _principal_repo(state_root: Path) -> Path | None:
    try:
        real = state_root.resolve()
    except OSError:
        return None
    # .../lovework-principal-<p>/state/<p> → .../lovework-principal-<p>
    if len(real.parents) >= 2 and real.parent.name == "state":
        return real.parents[1]
    return None


def _git(args: list[str], cwd: Path | None = None,
         git_dir: Path | None = None) -> subprocess.CompletedProcess | None:
    cmd = ["git"]
    if cwd:
        cmd += [f"-c", f"safe.directory={cwd}"]
    if git_dir:
        cmd += [f"-c", f"safe.directory={git_dir}", "--git-dir", str(git_dir)]
    cmd += args
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def _hub_lag(state_root: Path) -> dict:
    repo = _principal_repo(state_root)
    if repo is None:
        return _reading(applicable=False, observed=False,
                        detail="not a principal repo layout")
    remote = _git(["remote", "get-url", "deploy"], cwd=repo)
    if remote is None or remote.returncode != 0 or not remote.stdout.strip():
        return _reading(applicable=False, observed=False,
                        detail="no deploy remote")
    url = remote.stdout.strip()
    hub = Path(url[7:]) if url.startswith("file://") else Path(url)
    if not hub.is_absolute() and "@" in url:
        return _reading(applicable=False, observed=False,
                        detail="hub is remote (ssh); deep probe not implemented")
    if not hub.is_dir():
        return _reading(applicable=False, observed=False,
                        detail=f"hub not readable at {hub}")
    head = _git(["rev-parse", "HEAD"], cwd=repo)
    tip = _git(["rev-parse", "HEAD"], git_dir=hub)
    if not head or not tip or head.returncode or tip.returncode:
        return _reading(applicable=False, observed=False,
                        detail="git could not read HEAD")
    ahead = _git(["rev-list", "--count", f"{tip.stdout.strip()}..HEAD"], cwd=repo)
    if not ahead or ahead.returncode:
        return _reading(applicable=False, observed=False,
                        detail="git could not compare HEAD with the hub")
    try:
        value = int(ahead.stdout.strip())
    except ValueError:
        return _reading(applicable=False, observed=False,
                        detail="git returned a non-number")
    return _reading(value, source_at=_iso(),
                    detail=f"HEAD {head.stdout.strip()[:9]} vs hub {tip.stdout.strip()[:9]}")


def _deploy_metrics() -> dict[str, dict]:
    # A dev checkout may inspect the production deployment without pretending
    # its own tree is the deployed one.
    root = Path(os.getenv("LOVEWORK_DEPLOY_ROOT") or config.LOVEWORK_ROOT)
    stamp_path = root.parent / "deployed.json"
    if not stamp_path.is_file():
        return {
            "deploy.code_dirty": _reading(applicable=False, observed=False,
                                          detail="no deployed.json (dev tree)"),
            "deploy.stamp_matches": _reading(applicable=False, observed=False,
                                             detail="no deployed.json (dev tree)"),
        }
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "deploy.code_dirty": _reading(applicable=True, observed=False,
                                          detail=f"deployed.json unreadable: {exc}"),
            "deploy.stamp_matches": _reading(applicable=True, observed=False,
                                             detail=f"deployed.json unreadable: {exc}"),
        }
    status = _git(["status", "--porcelain"], cwd=root)
    head = _git(["rev-parse", "HEAD"], cwd=root)
    dirty = None
    if status is not None and status.returncode == 0:
        lines = [line for line in status.stdout.splitlines()
                 if not line[3:].startswith(("state/", "profiles/"))]
        dirty = len(lines)
    matches = None
    if head is not None and head.returncode == 0:
        matches = head.stdout.strip() == str(stamp.get("sha", "")).strip()
    return {
        "deploy.code_dirty": _reading(dirty, observed=dirty is not None,
                                      source_at=_iso(),
                                      detail="uncommitted code outside state/"),
        "deploy.stamp_matches": _reading(matches, observed=matches is not None,
                                         source_at=_iso(),
                                         detail=f"stamp {str(stamp.get('sha',''))[:9]}"),
    }


def _monitor_readings(principal: str, state_dir: Path,
                      now: datetime) -> dict[str, dict]:
    try:
        import monitor

        sig = monitor.signals(profiles=[principal], state_dir=state_dir, now=now)
    except Exception as exc:
        detail = f"monitor.signals failed: {type(exc).__name__}: {exc}"
        return {name: _reading(observed=False, detail=detail)
                for name in ("loop.heartbeat_age_min", "loop.failed_receipts_24h",
                             "scheduler.failing_jobs")}
    data = (sig.get("principals") or {}).get(principal) or {}
    beat = data.get("heartbeat") or {}
    age = beat.get("minutes_old")
    age_ok = isinstance(age, (int, float)) and age >= 0
    readings = {
        "loop.heartbeat_age_min": _reading(
            age if age_ok else None, observed=age_ok,
            source_at=beat.get("at") or "",
            detail=f"outcome {beat.get('outcome')!r}: {beat.get('why')!r}"),
    }
    receipts = data.get("receipts") or {}
    readings["loop.failed_receipts_24h"] = _reading(
        receipts.get("failed"), observed="failed" in receipts,
        source_at=sig.get("at") or "",
        detail=f"acted {receipts.get('acted')}, blocked {receipts.get('blocked')}, "
               f"total {receipts.get('total')}")
    if sig.get("cron_store") == "unavailable":
        readings["scheduler.failing_jobs"] = _reading(
            observed=False, source_at=sig.get("at") or "",
            detail="scheduler store unavailable — not checked")
    else:
        failing = sum(1 for row in sig.get("cron") or []
                      if row.get("state") == "failing")
        readings["scheduler.failing_jobs"] = _reading(
            failing, source_at=sig.get("at") or "",
            detail=f"{len(sig.get('cron') or [])} job(s) in the window")
    return readings


def collect(principal: str, *, deep: bool = False, refresh: bool = False,
            write: bool = False, now: datetime | None = None) -> dict:
    """Read the authorities and build one snapshot. Never raises."""
    now = now or _now()
    state_dir = Path(config.STATE_DIR)
    state_root = state_dir / principal
    if not state_root.is_dir():
        snapshot = build_snapshot(principal, {}, at=_iso(now),
                                  gaps={"error": f"no state at {state_root}"})
        snapshot["missing_state"] = str(state_root)
        return snapshot
    readings = _monitor_readings(principal, state_dir, now)
    audit = _wiki_audit(principal, state_root, deep=deep, refresh=refresh,
                        write=write)
    if audit is None or audit.get("total") is None:
        detail = (audit or {}).get("error", "wiki audit unavailable")
        for name in ("wiki.defects", "packs.go_without_pack",
                     "packs.go_unprepared", "leads.missing_page"):
            readings[name] = _reading(observed=False, detail=str(detail))
        gaps = {}
    else:
        classes = audit.get("classes") or {}
        wiki_defects = sum(n for name, n in classes.items()
                           if name not in PACK_LEAD_CLASSES)
        readings["wiki.defects"] = _reading(
            wiki_defects, source_at=audit.get("at") or "",
            detail=f"{len(classes)} class(es), {audit.get('org_pages', 0)} pages")
        readings["packs.go_without_pack"] = _reading(
            classes.get("go_without_pack", 0), source_at=audit.get("at") or "")
        readings["packs.go_unprepared"] = _reading(
            classes.get("go_unprepared", 0), source_at=audit.get("at") or "")
        readings["leads.missing_page"] = _reading(
            classes.get("lead_without_page", 0), source_at=audit.get("at") or "")
        gaps = audit.get("gaps") or {}
    receipts = _receipts_audit(principal, state_root, deep=deep, refresh=refresh,
                               write=write)
    if receipts is None or receipts.get("error"):
        readings["receipts.audit_defects"] = _reading(
            observed=False, detail=(receipts or {}).get("error", "audit unavailable"))
    else:
        defects = receipts.get("defects") or []
        readings["receipts.audit_defects"] = _reading(
            len(defects), source_at=receipts.get("at") or "",
            detail=", ".join(sorted({d.get("kind", "?") for d in defects}))[:180])
    readings.update(_deploy_metrics())
    readings["repo.hub_lag"] = _hub_lag(state_root)
    previous = _last_history(state_root)
    return build_snapshot(principal, readings, at=_iso(now),
                          previous=previous, gaps=gaps)


def _last_history(state_root: Path) -> dict | None:
    path = state_root / "health" / "history.jsonl"
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, json.JSONDecodeError):
        return None


def write_snapshot(snapshot: dict) -> Path:
    state_root = Path(config.STATE_DIR) / snapshot["principal"] / "health"
    state_root.mkdir(parents=True, exist_ok=True)
    latest = state_root / "latest.json"
    latest.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n",
                      encoding="utf-8")
    row = {
        "at": snapshot["at"],
        "principal": snapshot["principal"],
        "variance": snapshot["variance"]["total"],
        "coverage": snapshot["variance"]["coverage"],
        "errors": {name: row["error"] for name, row in
                   snapshot["metrics"].items() if row["applicable"]},
        "values": {name: row["value"] for name, row in
                   snapshot["metrics"].items() if row["applicable"]},
    }
    with (state_root / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return latest


def render(snapshot: dict) -> str:
    v = snapshot["variance"]
    cov = v["coverage"]
    lines = [f"{snapshot['principal']}: variance {v['total']:.3f} · "
             f"coverage {cov['observed']}/{cov['applicable']} · {snapshot['at']}"]
    order = {m.name: i for i, m in enumerate(METRICS)}
    for name, row in sorted(snapshot["metrics"].items(),
                            key=lambda kv: order.get(kv[0], 999)):
        if not row["applicable"]:
            continue
        mark = {"ok": "ok", "warn": "warn", "bad": "BAD", "unknown": "??"}[row["status"]]
        lines.append(f"  {name:28s} {str(row['value']):>10s}  target "
                     f"{METRIC_BY_NAME[name].target_text():>7s}  "
                     f"error {row['error']:.2f}  {mark}")
    if v["worst"]:
        lines.append("  worst: " + ", ".join(
            f"{row['name']} {row['error']:.2f}" for row in v["worst"]))
    if v["rose"]:
        lines.append("  rose: " + ", ".join(v["rose"]))
    if snapshot.get("gaps"):
        lines.append("  gaps: " + ", ".join(
            f"{k} {v}" for k, v in sorted(snapshot["gaps"].items())))
    return "\n".join(lines)


def read_status(state_root: Path) -> dict | None:
    """The newest stored snapshot as the brief/monitor read it. No collection.

    This is a read of the one authority (`health/latest.json` and its
    `history.jsonl`), not a second health calculation.
    """
    state_root = Path(state_root)
    root = state_root / "health"
    latest_path = root / "latest.json"
    if not latest_path.is_file():
        return None
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    variance_block = latest.get("variance") or {}
    variance = float(variance_block.get("total", 0.0))
    coverage = variance_block.get("coverage") or {}
    history: list[dict] = []
    history_path = root / "history.jsonl"
    if history_path.is_file():
        try:
            for line in history_path.read_text(
                    encoding="utf-8").splitlines()[-80:]:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            history = []
    previous = history[-2] if len(history) >= 2 else None
    previous_variance = (float(previous.get("variance", 0.0))
                         if previous is not None else None)
    delta = (variance - previous_variance
             if previous_variance is not None else 0.0)
    return {
        "at": latest.get("at"),
        "variance": variance,
        "previous_variance": previous_variance,
        "delta": round(delta, 6),
        "coverage": coverage,
        "unknown": int(coverage.get("unknown", 0) or 0),
        "rose": list(variance_block.get("rose") or []),
        "worst": list(variance_block.get("worst") or [])[:3],
        "principal": latest.get("principal") or state_root.name,
    }


def brief_line(status: dict | None) -> str:
    """One line for the brief: the variance and its change, not the whole table."""
    if not status:
        return ""
    parts = [f"variance {status['variance']:.3f}"]
    if status.get("previous_variance") is not None:
        parts.append(f"delta {status['delta']:+.3f}")
    if status.get("unknown"):
        parts.append(f"unknown {status['unknown']}")
    if status.get("rose"):
        parts.append("rose: " + ", ".join(status["rose"]))
    for row in status.get("worst") or []:
        parts.append(f"worst {row.get('name')} {float(row.get('error', 0)):.2f}")
        break
    return "Health: " + " · ".join(parts)


def _trend_rows(state_root: Path, *, days: int = 7) -> list[tuple[str, dict]]:
    path = Path(state_root) / "health" / "history.jsonl"
    if not path.is_file():
        return []
    by_day: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = str(row.get("at", ""))[:10]
            if day:
                by_day[day] = row
    except OSError:
        return []
    return sorted(by_day.items())[-max(1, int(days)):]


def render_trend(principal: str, state_root: Path, *, days: int = 7) -> str:
    """The weekly view: the variance series and the worst subsystem now."""
    rows = _trend_rows(state_root, days=days)
    lines = [f"{principal}: health trend, last {max(1, int(days))} day(s)"]
    if not rows:
        lines.append("  no history yet")
        return "\n".join(lines)
    for day, row in rows:
        coverage = row.get("coverage") or {}
        lines.append(
            f"  {day} variance {float(row.get('variance', 0.0)):.3f} "
            f"(unknown {coverage.get('unknown', 0)})")
    latest_path = Path(state_root) / "health" / "latest.json"
    if latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            metrics = latest.get("metrics") or {}
            by_subsystem: dict[str, float] = {}
            for metric in METRICS:
                row = metrics.get(metric.name) or {}
                if not row.get("applicable"):
                    continue
                by_subsystem[metric.subsystem] = (
                    by_subsystem.get(metric.subsystem, 0.0)
                    + metric.weight * float(row.get("error", 0.0)))
            if by_subsystem:
                worst = sorted(by_subsystem.items(), key=lambda item: -item[1])[:3]
                lines.append("  worst subsystems: " + ", ".join(
                    f"{name} {value:.3f}" for name, value in worst))
        except (OSError, json.JSONDecodeError):
            pass
    return "\n".join(lines)


def write_trend(principal: str, state_root: Path, *, days: int = 7) -> Path:
    root = Path(state_root) / "health"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isocalendar()
    path = root / f"trend-{stamp.year}-W{stamp.week:02d}.md"
    text = "# " + render_trend(principal, state_root, days=days) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="lj")
    ap.add_argument("--json", action="store_true", help="print the whole snapshot")
    ap.add_argument("--explain", action="store_true", help="print the metric table")
    ap.add_argument("--deep", action="store_true",
                    help="force fresh wiki/receipt audits (slow)")
    ap.add_argument("--refresh", action="store_true",
                    help="same as --deep for the cached measurements")
    ap.add_argument("--write", action="store_true",
                    help="write latest.json, history.jsonl and the audit caches")
    ap.add_argument("--trend", action="store_true",
                    help="print the recent variance trend and worst subsystems")
    ap.add_argument("--days", type=int, default=7,
                    help="days of history for --trend (default 7)")
    args = ap.parse_args(argv)
    config.set_active_profile(args.profile)
    if args.trend:
        state_root = Path(config.STATE_DIR) / args.profile
        if not state_root.is_dir():
            print(f"{args.profile}: no state at {state_root}", file=sys.stderr)
            return 3
        print(render_trend(args.profile, state_root, days=args.days))
        if args.write:
            write_trend(args.profile, state_root, days=args.days)
        return 0
    snapshot = collect(args.profile, deep=args.deep, refresh=args.refresh,
                       write=args.write)
    if snapshot.get("missing_state"):
        print(f"{args.profile}: no state at {snapshot['missing_state']}",
              file=sys.stderr)
        return 3
    if args.write:
        write_snapshot(snapshot)
    if args.json:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        print(render(snapshot))
    cov = snapshot["variance"]["coverage"]
    if cov["unknown"]:
        return 2
    return 1 if snapshot["variance"]["total"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

"""Censoring-aware availability episodes and the survival projection (WP3b box 4).

The registry is a *current-state* table: one row per advert carrying aggregates
(`first_seen`, `last_seen`, `status`). The observation history lives in the
**seals** — each seal is one dated snapshot of the registry — so episodes are
built from sealed snapshots, and everything an episode says is one of two kinds:

* an **assertion** — a seal listed the advert, so we saw it open at that moment;
* an **inference** — a seal did not list it, which bounds a closure to the
  interval between the last seal that listed it and the first that did not. The
  instant of closure is never observed and is never claimed.

That distinction is the whole point: "we saw it" and "it must have closed
somewhere in this window" are different facts, and collapsing them is how a
lifecycle model starts lying about the world.

**Honest limits, stated in the results rather than implied:** absence is only
evidence when the source that would have listed it was actually enumerated (a
capped feed or a failed fetch leaves the gap *unobserved*, not empty), the
registry's own dates are processing dates, and a duration measured to the last
sighting is a **lower bound** on how long the advert was really open.
"""
from __future__ import annotations
import jsonl_io

import csv
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

REGISTRY_CSV = "state/cache/jobs.csv"

#: Episode states. `open` is right-censored (the newest seal still listed it);
#: `closed` is interval-censored (we know only the window it closed in).
STATE_OPEN = "open"
STATE_CLOSED = "closed"

CENSOR_RIGHT = "right"
CENSOR_INTERVAL = "interval"


def _advert_key(row: dict) -> str:
    """The advert's identity: its registry hash, or a derived one for old rows."""
    h = (row.get("hash") or "").strip()
    if h:
        return h
    return hashlib.sha256(
        "|".join((row.get("org", ""), row.get("title", ""), row.get("url", "")))
        .encode("utf-8")).hexdigest()[:16]


@dataclass
class Observation:
    """One seal's view of one advert — the atomic evidence.

    `qualified` says whether an *absence* at this seal is evidence at all: it is
    only evidence when the source that would list the advert read its whole
    scope (see `_absence_verdict`).
    """

    run_id: str
    sealed_at: str
    present: bool
    row: dict = field(default_factory=dict)
    qualified: bool = False
    reason: str = ""


@dataclass
class Episode:
    """A maximal run of seals that listed the advert, plus its inferred end."""

    key: str
    org: str
    title: str
    url: str
    start: str                      # sealed_at of the first listing
    last_present: str               # sealed_at of the last listing
    end: Optional[str]              # sealed_at of the first absence, or None
    state: str
    censoring: str
    assertions: list[dict] = field(default_factory=list)
    inference: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    last_primary_seen: str = ""
    posted_at: str = ""
    #: Seals where the advert was not listed but the scope was not read — the
    #: gaps we cannot interpret. Recorded so an "open" episode whose last
    #: assertion is old explains itself.
    unobserved: list[dict] = field(default_factory=list)

    @property
    def episode_id(self) -> str:
        """Deterministic identity: the advert plus when the episode began.

        Derived **only from immutable facts**, deliberately excluding `state`:
        an episode that goes from open to closed is the *same* episode with
        changed facts, so its identity must not move — otherwise a correction
        could never name what it supersedes. A re-listing is distinguished by
        its different `start`, which is immutable.
        """
        return hashlib.sha256(
            f"{self.key}|{self.start}".encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "advert_key": self.key,
            "org_name": self.org,
            "title": self.title,
            "url": self.url,
            "start": self.start,
            "last_present": self.last_present,
            "end": self.end,
            "state": self.state,
            "censoring": self.censoring,
            "assertions": self.assertions,
            "inference": self.inference,
            "sources": self.sources,
            "last_primary_seen": self.last_primary_seen,
            "posted_at": self.posted_at,
            "unobserved": self.unobserved,
        }


def _read_frozen_registry(bundle: Path) -> list[dict]:
    path = bundle / REGISTRY_CSV
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _coverage(manifest) -> dict[str, dict]:
    """Per-source coverage verdicts recorded by the seal itself."""
    out: dict[str, dict] = {}
    for attempt in getattr(manifest, "coverage", []) or []:
        out[attempt.source] = {"outcome": attempt.outcome, "scope": attempt.scope,
                               "cache_use": getattr(attempt, "cache_use", "unknown")}
    return out


def _observations(engine) -> list[tuple[str, str, dict[str, dict], dict[str, dict]]]:
    """(run_id, sealed_at, {advert_key: row}, coverage) for every seal, oldest first."""
    out = []
    for run_id in engine.sealed_run_ids():
        manifest = engine.read_manifest(run_id)
        if manifest is None:
            continue
        rows = _read_frozen_registry(engine.bundle_path(run_id))
        out.append((run_id, manifest.clock.sealed_at,
                    {_advert_key(r): r for r in rows}, _coverage(manifest)))
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def _absence_verdict(source: str, coverage: dict[str, dict],
                     last_scope: Optional[str]) -> tuple[bool, str]:
    """Is an absence at this seal evidence that the advert is gone?

    Only when the source actually read its whole scope: a partial or failed
    traversal, a cached-only replay, a changed query, or a source that did not
    run at all all leave the gap **unobserved**, and an unobserved gap is not
    evidence (docs/28 §WP3b box 5, "no false life/closure").
    """
    c = coverage.get(source)
    if c is None:
        return False, "source_not_run"
    if c["outcome"] != "complete":
        return False, f"scope_not_complete:{c['outcome']}"
    if c.get("cache_use") == "cached_only":
        return False, "cached_only_replay"
    if last_scope is not None and c["scope"] != last_scope:
        return False, "query_changed"
    return True, "complete"


def _identity_shifts(eps: list[Episode],
                     seals: list[tuple]) -> list[dict]:
    """Adverts whose *identity* changed between seals (a corrected hash).

    An identity correction looks exactly like a closure followed by a new
    listing. We cannot resolve which it is from here, so we do the honest thing:
    report the shift, and mark the affected closure as suspect rather than
    presenting an artifact of our own bookkeeping as a fact about the world.
    """
    by_name: dict[tuple[str, str], list[Episode]] = {}
    for e in eps:
        by_name.setdefault((e.org.lower(), e.title.lower()), []).append(e)
    shifts = []
    for (org, title), group in sorted(by_name.items()):
        if len(group) < 2:
            continue
        group.sort(key=lambda e: e.start)
        for older, newer in zip(group, group[1:]):
            if older.state != STATE_CLOSED:
                continue
            shifts.append({
                "org_name": older.org, "title": older.title,
                "from_key": older.key, "to_key": newer.key,
                "closed_interval": [older.last_present, older.end],
                "appeared_at": newer.start,
                "note": ("the same org and title under a different advert key: "
                         "either the advert was re-listed, or its identity was "
                         "corrected — the closure above may be an artifact"),
            })
            for inf in older.inference:
                inf["identity_shift_suspected"] = True
    return shifts


def episodes(engine, *, org: str | None = None,
             keys: Iterable[str] | None = None) -> dict:
    """Availability episodes per advert across the seals.

    A re-listing starts a **new** episode: absent-then-present is two episodes,
    not one long one, because the advert genuinely left and came back.
    """
    seals = _observations(engine)
    if not seals:
        return {"status": "no_seals", "principal": engine.state_root.name,
                "episodes": [], "seals": 0,
                "detail": "no sealed snapshots to observe availability from",
                "scope": SCOPE_NOTE}
    wanted = set(keys) if keys else None
    keys_seen: set[str] = set()
    for _, _, rows, _ in seals:
        keys_seen.update(rows)

    built: list[Episode] = []
    for key in sorted(keys_seen):
        if wanted is not None and key not in wanted:
            continue
        row_seen = next((rows[key] for _, _, rows, _ in seals if key in rows), {})
        source = row_seen.get("source") or ""
        series: list[Observation] = []
        last_scope: Optional[str] = None
        for run_id, sealed_at, rows, coverage in seals:
            if key in rows:
                series.append(Observation(run_id, sealed_at, True, rows[key]))
                last_scope = (coverage.get(source) or {}).get("scope", last_scope)
                continue
            qualified, reason = _absence_verdict(source, coverage, last_scope)
            series.append(Observation(run_id, sealed_at, False, row_seen,
                                      qualified=qualified, reason=reason))
        if org and org.lower() not in (series[0].row.get("org") or "").lower():
            continue
        built.extend(_episodes_for(key, series))
    shifts = _identity_shifts(built, seals)
    return {
        "status": "ok",
        "principal": engine.state_root.name,
        "seals": len(seals),
        "identity_shifts": shifts,
        "episodes": [e.to_dict() for e in built],
        "counts": {
            "episodes": len(built),
            "open": sum(1 for e in built if e.state == STATE_OPEN),
            "closed": sum(1 for e in built if e.state == STATE_CLOSED),
        },
        "scope": SCOPE_NOTE,
    }


SCOPE_NOTE = ("seal-level observations: an assertion is a seal that listed the "
              "advert; a closure is only ever an interval between the last "
              "listing and the first absence, never an instant")


def _episodes_for(key: str, series: list[Observation]) -> list[Episode]:
    """Split one advert's presence series into episodes.

    Only a **qualified** absence closes an episode. An unobserved gap (partial
    or failed traversal, cached-only replay, changed query, source not run) is
    recorded as `unobserved` and does nothing: it cannot close the advert, and
    it must not split the episode either — a later sighting after an unobserved
    gap is *not* a re-listing, because we never saw the advert leave.
    """
    built: list[Episode] = []
    current: list[Observation] = []
    unobserved: list[dict] = []
    for obs in series:
        if obs.present:
            current.append(obs)
            continue
        if not current:
            continue
        if obs.qualified:
            built.append(_close(key, current, obs, unobserved))
            current, unobserved = [], []
        else:
            unobserved.append({"run_id": obs.run_id, "sealed_at": obs.sealed_at,
                               "reason": obs.reason})
    if current:
        built.append(_open(key, current, unobserved))
    return built


def _base(key: str, obs: list[Observation]) -> dict:
    row = obs[-1].row or obs[0].row
    return {
        "key": key,
        "org": row.get("org", ""),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "sources": sorted({(o.row.get("source") or "") for o in obs
                           if o.row.get("source")}),
        "last_primary_seen": row.get("last_primary_seen", ""),
        "posted_at": row.get("posted_at", ""),
    }


def _assertions(obs: list[Observation]) -> list[dict]:
    """Every seal that *listed* the advert — the assertions, not inferences."""
    return [{"run_id": o.run_id, "sealed_at": o.sealed_at} for o in obs]


def _open(key: str, obs: list[Observation],
          unobserved: list[dict] | None = None) -> Episode:
    return Episode(
        **(_base(key, obs)),
        start=obs[0].sealed_at, last_present=obs[-1].sealed_at, end=None,
        state=STATE_OPEN, censoring=CENSOR_RIGHT,
        assertions=_assertions(obs),
        inference=[],
        unobserved=unobserved or [],
    )


def _close(key: str, obs: list[Observation], absent: Observation,
           unobserved: list[dict] | None = None) -> Episode:
    """The advert was listed at `obs[-1]` and absent at `absent` — the closure
    lies somewhere in between, and that interval is the entire claim."""
    return Episode(
        **(_base(key, obs)),
        start=obs[0].sealed_at, last_present=obs[-1].sealed_at,
        end=absent.sealed_at, state=STATE_CLOSED, censoring=CENSOR_INTERVAL,
        assertions=_assertions(obs),
        inference=[{
            "basis": "absence_after_last_listing",
            "from": obs[-1].sealed_at,
            "to": absent.sealed_at,
            "absent_run_id": absent.run_id,
            "note": ("closure is bounded to this interval; the instant was not "
                     "observed, and the absence counts only because the source "
                     "read its whole scope at that seal"),
        }],
        unobserved=unobserved or [],
    )


# ── survival projection ──────────────────────────────────────────────────

def _days(a: str, b: str) -> int:
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def survival(result: dict) -> dict:
    """A small-n survival summary that keeps its censoring honest.

    Closed episodes contribute an event at their **lower-bound** duration (the
    closure happened after the last listing, so the true duration is longer);
    open episodes are right-censored at their observed duration. A
    Kaplan-Meier-style curve is reported because it is the standard shape, but
    with the bias named: using a lower-bound event time *shortens* every
    observed duration, so the curve understates survival, and the interval
    censoring means no instant of closure is known at all.
    """
    eps = result.get("episodes") or []
    if not eps:
        # The shape stays the same whatever the data: a consumer (or a renderer)
        # must not have to branch on emptiness to read a field.
        return {"status": result.get("status", "ok"), "n": 0, "n_closed": 0,
                "n_open": 0, "median_lower_bound_days": None,
                "longest_open_days": None,
                "longest_closed_lower_bound_days": None, "curve": [],
                "detail": "no episodes to project from",
                "caveats": [SCOPE_NOTE]}
    durations = []
    for e in eps:
        d = _days(e["start"], e["last_present"])
        durations.append((d, e["state"] == STATE_OPEN))
    durations.sort()
    at_risk = len(durations)
    curve = []
    for d, censored in durations:
        if not censored:
            curve.append({"days": d, "at_risk": at_risk, "event": "closed"})
        at_risk -= 1
    closed = [d for d, censored in durations if not censored]
    open_ = [d for d, censored in durations if censored]
    median = sorted(closed)[len(closed) // 2] if closed else None
    return {
        "status": "ok",
        "n": len(eps),
        "n_closed": len(closed),
        "n_open": len(open_),
        "median_lower_bound_days": median,
        "longest_open_days": max(open_) if open_ else None,
        "longest_closed_lower_bound_days": max(closed) if closed else None,
        "curve": curve,
        "caveats": [
            SCOPE_NOTE,
            "event times are lower bounds: a closure is known only to lie after "
            "the last listing, so the curve understates real survival",
            "registry dates are processing dates, not world dates",
            "small n: this is availability evidence, not a calibrated hiring model",
        ],
    }


# ── optional materialisation (only if a consumer needs it) ───────────────

OUTCOME_EVENT_KIND = "availability_episode"


def materialize_outcome_events(result: dict, path: Path,
                               *, principal: str = "") -> dict:
    """Append availability outcome events with deterministic identities.

    Idempotent by construction: the identity is derived from the episode's
    immutable facts, so re-materialising adds nothing. A **correction** appends a
    row that names the episode it supersedes (`supersedes`) instead of rewriting
    history — the earlier claim stays readable, which is the only way a
    correction can be audited later.
    """
    episodes_ = result.get("episodes") or []
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.is_file():
        for line in jsonl_io.lines(path):
            if line.strip():
                row = json.loads(line)
                existing[row.get("outcome_id")] = row
    added, superseded = 0, 0
    with path.open("a", encoding="utf-8") as f:
        for e in episodes_:
            outcome_id = f"availability:{principal}:{e['episode_id']}"
            prior = existing.get(outcome_id)
            row = {
                "outcome_id": outcome_id,
                "event_type": OUTCOME_EVENT_KIND,
                "principal": principal,
                "episode_id": e["episode_id"],
                "advert_key": e["advert_key"],
                "org_name": e["org_name"],
                "title": e["title"],
                "state": e["state"],
                "censoring": e["censoring"],
                "start": e["start"],
                "last_present": e["last_present"],
                "end": e["end"],
            }
            if prior is None:
                f.write(json.dumps(row, sort_keys=True) + "\n")
                existing[outcome_id] = row
                added += 1
            elif {k: prior.get(k) for k in ("state", "end")} != {
                    k: row[k] for k in ("state", "end")}:
                # The world moved (e.g. a re-listing, or a closure we can now
                # bound differently): append a superseding row.
                f.write(json.dumps({**row, "supersedes": outcome_id,
                                    "correction": True}, sort_keys=True) + "\n")
                existing[outcome_id] = row
                superseded += 1
    return {"added": added, "superseded": superseded, "path": str(path),
            "total_after": len(existing)}

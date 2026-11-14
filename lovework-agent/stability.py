"""Scope-aware stability comparisons for MANUAL/report QA (WP4b).

When a report changes between two runs, three very different things can be
responsible, and only one of them is news:

* **scope or capping** — the source did not read the same ground (a capped feed,
  a partial traversal, a changed query, a cached-only replay). A difference here
  is our coverage, not the world;
* **profile or policy** — the lens changed (a different profile digest, a new
  policy version). A difference here is our judgement, not the world;
* **the world** — same ground, same lens, and the advert appeared or vanished.

Collapsing these is how a QA review ends up reporting our own crawl behaviour as
a hiring signal. This module separates them, and it **keeps both seals' original
revisions** (digests) rather than describing a merged picture: the question is
"what changed between these two artifacts", so both artifacts stay named.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REGISTRY_CSV = "state/cache/jobs.csv"

#: Attribution buckets, in the order they are trusted. A difference is blamed on
#: our coverage first, then on our lens, and only then on the world — because the
#: first two are things we did, and claiming them as news is the failure mode.
SCOPE = "scope_or_capping"
POLICY = "profile_or_policy"
WORLD = "world"
UNATTRIBUTED = "unattributed"   # AS0: no known cause — not proven world


def _frozen_registry(engine, run_id: str) -> dict[str, dict]:
    """The advert rows a seal froze, keyed by the registry's own identity."""
    path = engine.bundle_path(run_id) / REGISTRY_CSV
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {_key(row): row for row in csv.DictReader(f)}


def _key(row: dict) -> str:
    h = (row.get("hash") or "").strip()
    return h or "|".join((row.get("org", ""), row.get("title", ""),
                          row.get("url", "")))


def _coverage(manifest) -> dict[str, dict]:
    return {c.source: {"scope": c.scope, "outcome": c.outcome,
                       "cache_use": getattr(c, "cache_use", "unknown")}
            for c in (getattr(manifest, "coverage", []) or [])}


def _inputs(manifest) -> dict[str, str]:
    return {i.role: i.sha256 for i in (getattr(manifest, "inputs", []) or [])}


def compare(engine, from_run: str, to_run: str) -> dict:
    """Attribute the differences between two seals to coverage, lens, or world."""
    a = engine.read_manifest(from_run)
    b = engine.read_manifest(to_run)
    if a is None or b is None:
        return {"status": "unknown_run",
                "detail": "both run ids must be sealed bundles",
                "from": from_run, "to": to_run}

    cov_a, cov_b = _coverage(a), _coverage(b)
    scope_changes = []
    for source in sorted(set(cov_a) | set(cov_b)):
        ca, cb = cov_a.get(source), cov_b.get(source)
        if ca == cb:
            continue
        scope_changes.append({
            "source": source,
            "from": ca or "not run",
            "to": cb or "not run",
            "reads_the_world": bool(cb and cb["outcome"] == "complete"
                                    and cb.get("cache_use") != "cached_only"),
        })

    inputs_a, inputs_b = _inputs(a), _inputs(b)
    lens_changes = []
    for role in sorted(set(inputs_a) | set(inputs_b)):
        if inputs_a.get(role) != inputs_b.get(role):
            lens_changes.append({"role": role, "from": inputs_a.get(role),
                                 "to": inputs_b.get(role)})
    policy_a = getattr(a, "policy_version", None)
    policy_b = getattr(b, "policy_version", None)
    if policy_a != policy_b:
        lens_changes.append({"role": "policy_version", "from": policy_a,
                             "to": policy_b})

    rows_a, rows_b = _frozen_registry(engine, from_run), _frozen_registry(engine, to_run)
    scope_sources = {c["source"] for c in scope_changes}
    changed = []
    for key in sorted(set(rows_a) | set(rows_b)):
        ra, rb = rows_a.get(key), rows_b.get(key)
        if (ra or {}).get("status") == (rb or {}).get("status") and ra and rb:
            continue
        source = (rb or ra or {}).get("source", "")
        if rb is None:
            kind = "vanished"
        elif ra is None:
            kind = "appeared"
        else:
            kind = "status_changed"
        if source in scope_sources:
            attribution = SCOPE
        elif lens_changes:
            attribution = POLICY
        else:
            # AS0 (docs/34): scope and lens both look unchanged, but that
            # only means *we* cannot see a cause. Absence of a known cause
            # is not evidence of a world change — same-day re-crawls,
            # unverifiable boards and identity churn all live here. The
            # residual is honest: unattributed, never defaulted to world.
            attribution = UNATTRIBUTED
        changed.append({"advert_key": key, "kind": kind, "attribution": attribution,
                        "source": source,
                        "org_name": (rb or ra or {}).get("org", ""),
                        "title": (rb or ra or {}).get("title", ""),
                        "status_from": (ra or {}).get("status"),
                        "status_to": (rb or {}).get("status")})

    by_bucket = {bucket: [c for c in changed if c["attribution"] == bucket]
                 for bucket in (SCOPE, POLICY, WORLD, UNATTRIBUTED)}
    comparable = not scope_changes and not lens_changes
    caveats = []
    if scope_changes:
        caveats.append(
            "coverage differs between these runs: differences from those sources "
            "are our enumeration, not the world")
    if lens_changes:
        caveats.append(
            "the profile or policy differs between these runs: differences are "
            "our judgement, not the world")
    if not caveats:
        caveats.append(
            "same scope and same lens: the differences below are the only ones "
            "that can be read as the world changing")

    return {
        "status": "ok",
        "from": {"run_id": from_run, "sealed_at": a.clock.sealed_at,
                 "digest": a.digest()},
        "to": {"run_id": to_run, "sealed_at": b.clock.sealed_at,
               "digest": b.digest()},
        "comparable": comparable,
        "verdict": ("comparable: same scope, same lens" if comparable else
                    "not comparable: coverage and/or lens changed"),
        "scope_or_capping": scope_changes,
        "profile_or_policy": lens_changes,
        "world": by_bucket[WORLD],
        "unattributed": by_bucket[UNATTRIBUTED],
        "attributed": by_bucket,
        "counts": {
            "adverts_from": len(rows_a), "adverts_to": len(rows_b),
            "changed": len(changed),
            "world_changes": len(by_bucket[WORLD]),
            "unattributed_changes": len(by_bucket[UNATTRIBUTED]),
        },
        # The original revisions stay named: this is a statement about two
        # artifacts, so neither is merged into the other.
        "revisions_kept": [a.digest(), b.digest()],
        "caveats": caveats,
    }

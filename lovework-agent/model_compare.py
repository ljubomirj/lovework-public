#!/usr/bin/env python3
"""Score the same adverts with several models, in parallel, and compare.

A model swap is a *measurement* change, not a neutral one: `mimo-v2.6-flash`
scored VJ's 2026-09-22 sweep ~1.5-2 points below `mimo-v2.5` and emptied the GO
tier (0 GO against 30 the day before), while `mimo-v2.5` on a 5-advert batch
agreed with it on only 2 of 5 verdicts. Section counts from two different days
cannot separate that from a genuinely weaker market, so this tool holds the
corpus fixed and varies only the model.

It reuses the reassessment's evidence path (registry record → retained primary
advert) and reports, per model: score mean/median, parse failures, latency, how
often its decision matched the others, and the per-advert table. One advert = one
`match()` call per model, so cost scales with the advert count, not the registry.

Usage::

    ../venv/bin/python3 model_compare.py --profile vj --role data-statistics-pricing \
        --models mimo-v2.5,mimo-v2.6-flash,deepseek-v4.1-flash --limit 20 --workers 3 \
        --json /home/user/tmp/model-compare-vj.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

# Measure RAW model output: applying the calibration while measuring the scale it
# describes would be circular (and would make the models look like they agree).
os.environ.setdefault("LOVEWORK_CALIBRATION", "off")

import config
from enrichment import LeadEnricher
from llm_client import LLMClient
from matcher import JobMatcher
from principal_runtime import resolve_principal_runtime
from reassess import _record_description

logger = logging.getLogger("model_compare")


def pick_adverts(profile: str, limit: int) -> List[dict]:
    """The most recent registry records that still have retained advert evidence."""
    from job_registry import JobRegistry

    runtime = resolve_principal_runtime(profile)
    registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    enricher = LeadEnricher(cache_dir=runtime.cache_dir / "enrichment")

    records = list(registry.all_jobs())
    records.sort(key=lambda r: (getattr(r, "first_seen", "") or ""), reverse=True)
    picked: List[dict] = []
    for record in records:
        if not (record.url or "").strip():
            continue
        evidence = enricher.load_cached(_record_description(record), record.url)
        if not (evidence.primary_text or "").strip():
            continue
        picked.append({
            "org": record.org,
            "title": record.title,
            "url": record.url,
            "location": getattr(record, "location", "") or "",
            "seniority": getattr(record, "seniority", "") or "",
            "status": getattr(record, "status", "") or "",
            "advert": evidence.primary_text,
        })
        if len(picked) >= limit:
            break
    return picked


def score_one(model: str, advert: dict, profile_text: str) -> dict:
    """One match call. Returns the verdict plus what the comparison needs."""
    matcher = JobMatcher(LLMClient(model=model), profile_text, use_history=False)
    started = time.monotonic()
    try:
        result = matcher.match(
            advert["title"], advert["advert"], advert["org"],
            job_url=advert["url"], location=advert["location"],
        )
        elapsed = time.monotonic() - started
        unscored = str(result.reasoning).startswith("UNSCORED")
        return {
            "model": model,
            "org": advert["org"],
            "title": advert["title"],
            "score": result.combined_score,
            "decision": result.decision,
            "action": result.recommended_action,
            "parsed": not unscored,
            "seconds": round(elapsed, 1),
            "reasoning": str(result.reasoning)[:200],
        }
    except Exception as e:  # noqa: BLE001 - a failed call is data, not a crash
        return {
            "model": model, "org": advert["org"], "title": advert["title"],
            "score": None, "decision": "ERROR", "action": "", "parsed": False,
            "seconds": round(time.monotonic() - started, 1), "reasoning": f"{type(e).__name__}: {e}"[:200],
        }


def summarise(rows: List[dict], models: List[str], advert_count: int) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    by_advert: Dict[tuple, Dict[str, str]] = {}
    for row in rows:
        by_advert.setdefault((row["org"], row["title"]), {})[row["model"]] = row["decision"]
    for model in models:
        mine = [r for r in rows if r["model"] == model and r["score"] is not None]
        scores = [r["score"] for r in mine]
        agreements = 0
        comparisons = 0
        for _advert, decisions in by_advert.items():
            if model not in decisions:
                continue
            for other, decision in decisions.items():
                if other == model:
                    continue
                comparisons += 1
                agreements += int(decision == decisions[model])
        out[model] = {
            "calls": len([r for r in rows if r["model"] == model]),
            "parsed": len(mine),
            "score_mean": round(statistics.mean(scores), 2) if scores else None,
            "score_median": round(statistics.median(scores), 2) if scores else None,
            "score_range": [min(scores), max(scores)] if scores else None,
            "seconds_mean": round(statistics.mean([r["seconds"] for r in mine]), 1) if mine else None,
            "decision_agreement": round(agreements / comparisons, 2) if comparisons else None,
            "decisions": {
                d: sum(1 for r in mine if r["decision"] == d)
                for d in ("GO", "MAYBE", "FLAG", "DROP")
            },
            "adverts_scored": advert_count,
        }
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--models", required=True,
                        help="comma-separated model ids to compare")
    parser.add_argument("--limit", type=int, default=20, help="adverts per model")
    parser.add_argument("--workers", type=int, default=3, help="parallel calls")
    parser.add_argument("--json", default="", help="also write the raw rows here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    profile_text = config.load_profile_text(args.profile, role=args.role)
    adverts = pick_adverts(args.profile, args.limit)
    if not adverts:
        print(f"[compare] {args.profile}: no records with retained advert evidence")
        return 1

    print(f"[compare] {len(adverts)} advert(s) x {len(models)} model(s) = "
          f"{len(adverts) * len(models)} calls, {args.workers} in parallel")
    started = time.monotonic()
    tasks = [(model, advert) for advert in adverts for model in models]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda t: score_one(t[0], t[1], profile_text), tasks))
    elapsed = time.monotonic() - started

    summary = summarise(rows, models, len(adverts))
    print(f"\n[compare] done in {elapsed / 60:.1f} min\n")
    print(f"{'model':22} {'parsed':>7} {'mean':>6} {'median':>7} {'range':>12} "
          f"{'sec':>6} {'agree':>6}  decisions")
    for model in models:
        s = summary[model]
        print(f"{model:22} {s['parsed']:>3}/{s['calls']:<3} {str(s['score_mean']):>6} "
              f"{str(s['score_median']):>7} {str(s['score_range']):>12} "
              f"{str(s['seconds_mean']):>6} {str(s['decision_agreement']):>6}  {s['decisions']}")

    print("\n[compare] per advert (score/decision per model)")
    for advert in adverts:
        cells = []
        for model in models:
            row = next((r for r in rows
                        if r["model"] == model and r["org"] == advert["org"]
                        and r["title"] == advert["title"]), None)
            cells.append(f"{model.split('-')[0][:6]}:{row['score'] if row else '?'}/{row['decision'][:2] if row else '?'}")
        print(f"  {advert['org'][:26]:26} {advert['title'][:34]:34} {'  '.join(cells)}")

    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows, "adverts": adverts},
                                              indent=2), encoding="utf-8")
        print(f"\n[compare] raw rows → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

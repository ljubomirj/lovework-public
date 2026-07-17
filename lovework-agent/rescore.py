#!/usr/bin/env python3
"""
Re-score historical findings using the current matcher rules.

The matcher's rules evolve (org-level cooldown, work-auth kill, etc.). The
wiki stores findings from past runs with their OLD decision. This tool
re-runs the matcher against each historical entry and updates the wiki
index accordingly (rebuild_index with the new decisions).

It does NOT modify the org pages themselves — those are append-only by
design (so the cross-check appends stay intact). It only rebuilds the
`wiki/index.md` with the corrected decisions.

Usage:
    python rescore.py                          # rescore all entries from disk
    python rescore.py --dry-run               # show what would change
    python rescore.py --org Poolside           # rescore just one org
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import config
from job_registry import JobRegistry
from matcher import (
    JobMatcher,
    MatchResult,
    _apply_reapply_kill,
    _apply_work_auth_kill,
    _check_reapply_kill,
    _check_work_auth_kill,
)
from llm_client import LLMClient
from wiki_store import WikiEntry, WikiStore


def _parse_org_page(path: Path) -> list[dict]:
    """Parse entries from an org's wiki page. Returns [{date, org, title, url,
    location, source, score, decision, reasoning}] — one per `### DATE — Title`
    block.
    """
    text = path.read_text(encoding="utf-8")
    section_re = re.compile(r"^###\s+(?P<date>\d{4}-\d{2}-\d{2})\s+—\s+(?P<title>.+?)\s*$")
    decision_re = re.compile(r"^\s*-\s*\*\*Decision\*\*:\s*\*?(?P<decision>GO|MAYBE|FLAG|DROP)\*?(?:\s*\((?P<score>\d+(?:\.\d+)?)/10\))?")
    url_re = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
    loc_re = re.compile(r"^\s*-\s*\*\*Location\*\*:\s*(?P<loc>.+)")
    source_re = re.compile(r"^\s*-\s*\*\*Source\*\*:\s*(?P<src>.+)")
    reasoning_re = re.compile(r"^\s*-\s*\*\*Reasoning\*\*:\s*(?P<r>.+?)(?=\n###|\n\s*-\s*\*\*|\Z)")

    org = path.stem.replace("_", " ")
    out: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        sec = section_re.match(line.rstrip())
        if sec:
            if current:
                out.append(current)
            current = {
                "date": sec.group("date"),
                "title": sec.group("title").strip(),
                "org": org,
            }
            continue
        if current is None:
            continue
        m = decision_re.match(line)
        if m:
            current["decision"] = m.group("decision")
            if m.group("score"):
                current["score"] = float(m.group("score"))
            continue
        m = url_re.match(line)
        if m:
            current["url"] = m.group("url").strip()
            continue
        m = loc_re.match(line)
        if m:
            current["location"] = m.group("loc").strip()
            continue
        m = source_re.match(line)
        if m:
            current["source"] = m.group("src").strip()
            continue
        m = reasoning_re.match(line)
        if m:
            current["reasoning"] = m.group("r").strip()
    if current:
        out.append(current)
    return out


def _rescore_entry(entry: dict) -> Optional[dict]:
    """Re-run the pre-LLM kills (reapply, work-auth) on an entry. Returns
    the entry with updated decision / reasoning if the kill fires, or
    None if the entry should be skipped (e.g. it's a cross-check block).

    The LLM match isn't re-run (would cost $$); we only check the
    rule-based pre-LLM kills. The historical LLM-decision is kept
    unless a kill fires.
    """
    # Skip cross-check appends — they are informational, not real findings.
    if "prior-contact cross-check" in entry.get("title", "").lower():
        return None
    org = entry.get("org", "")
    title = entry.get("title", "")
    location = entry.get("location", "")
    description = entry.get("reasoning", "") + " " + entry.get("title", "")

    reapply_kill = _check_reapply_kill(org, title)
    work_auth_kill = _check_work_auth_kill(location, description)

    if reapply_kill or work_auth_kill:
        # Build a MatchResult so we can apply the kill formatters.
        mr = MatchResult(
            score=entry.get("score", 0.0),
            decision=entry.get("decision", ""),
            reasoning=entry.get("reasoning", ""),
        )
        mr = _apply_reapply_kill(mr, reapply_kill)
        mr = _apply_work_auth_kill(mr, work_auth_kill)
        return {
            **entry,
            "decision": mr.decision,
            "score": mr.score,
            "reasoning": mr.reasoning,
            "_was_killed_by": reapply_kill or work_auth_kill,
        }
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--org", help="Rescore just one org (substring match)")
    args = parser.parse_args()

    orgs_dir = config.WIKI_ORGS
    files = sorted(orgs_dir.glob("*.md"))
    if args.org:
        files = [f for f in files if args.org.lower() in f.stem.lower()]

    all_entries: list[dict] = []
    changes: list[tuple[Path, dict, dict]] = []
    skipped = 0
    for f in files:
        for entry in _parse_org_page(f):
            rescore = _rescore_entry(entry)
            if rescore is None:
                skipped += 1
                continue
            all_entries.append(WikiEntry(
                org_name=rescore["org"],
                title=rescore["title"],
                url=rescore.get("url"),
                location=rescore.get("location"),
                score=rescore.get("score", 0.0),
                decision=rescore.get("decision", ""),
                reasoning=rescore.get("reasoning", ""),
                source=rescore.get("source", ""),
                date=rescore.get("date", ""),
            ))
            if rescore.get("_was_killed_by"):
                changes.append((f, entry, rescore))

    print(f"Parsed {sum(1 for _ in all_entries)} entries from {len(files)} org pages ({skipped} skipped as cross-checks)")
    if changes:
        print(f"\n{len(changes)} entries would be auto-DROPPED by current rules:")
        for f, before, after in changes[:30]:
            old_decision = before.get("decision", "?")
            old_score = before.get("score", "?")
            print(f"  {before['org']} — {before['title']} ({before.get('date')}, was {old_decision} {old_score})")
            print(f"    killed by: {after['_was_kill'][:120] if '_was_kill' in after else after.get('_was_killed_by', '?')[:120]}")
        if len(changes) > 30:
            print(f"  …and {len(changes) - 30} more.")
    else:
        print("\nNo entries would be killed by current rules.")

    if args.dry_run:
        print("\n(dry run — no wiki changes written)")
        return

    if not changes:
        print("\nNothing to update.")
        return

    # Rebuild the index with the corrected decisions.
    ws = WikiStore()
    ws.rebuild_index(all_entries)
    print(f"\nWiki index rebuilt: {config.WIKI_ROOT / 'index.md'}")


if __name__ == "__main__":
    main()

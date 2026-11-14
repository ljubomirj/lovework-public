#!/usr/bin/env python3
"""The self-healing pass — one door, one order, every repair class (2026-10-08).

Self-healing is the agent's immune system: assume faults exist, find them,
repair the CLASS, and run again tomorrow whether or not anyone noticed. This
command is the beat of that loop for a principal's wiki.

Why one door matters: two repair tools grew on two hosts (`wiki_repair.py` and
`wiki_org_repair.py`), and the ORDER between them is not optional. Names and
URLs move pages; the dedupe and the shape normalisers must run *after* a move,
because a fold appends blocks to a target page and can re-create the duplicate
identity the dedupe had just collapsed. On 2026-10-08 the two were run in the
wrong order by hand and `duplicate_org_blocks` went 0 -> 9 within the hour.

The pass, in order:

  1. names   — rename / merge / fold / retitle a page whose name is not an
               organisation, and record every move in `org-renames.json`
               (`--rebuild-map`), because a move that is not recorded
               manufactures a dead link in the sealed reports;
  2. urls    — strip tracking parameters from advert URLs;
  3. links   — re-check the newest report's advert links when their last check
               is older than `LINK_DAYS`, and ACT on the verdicts: each problem
               becomes a `link_problem` event (so the morning brief says "the
               advert behind GO #3 is dead") and enters the audited classes (so
               rot alarms when it rises). LJ, 2026-10-08: *"checking for links
               is only useful if we do something with the results."*
  4. map     — re-derive the rename map from the provenance comments and git's
               rename detection, so a move made by any writer resolves;
  5. shapes  — normalise report-shaped blocks, notes and HTML entities, then
               collapse duplicate blocks (idempotent; safe to re-run);
  6. measure — `wiki_audit.py` for this principal, and compare with the last
               pass: a class that ROSE is an alarm even when the total fell.

Exit codes: 0 clean, 1 defects remain, 2 a class rose since the last pass,
75 the principal's writer lease is held by another operation (retry later).

Usage:
    selfheal.py --profile lj [--apply] [--json]
    selfheal.py --all [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from writer_boundary import WriterBusy, standalone_writer_guard

STATE_NAME = "selfheal.json"

#: Re-check a report's advert links when the last check is older than this many
#: days. The Tue/Thu link-recheck crons were folded into this step (2026-10-08).
LINK_DAYS = 3
#: How many report entries (GO first) one link re-check may open.
LINK_LIMIT = 25

#: Verdicts that mean the advert behind a wiki GO row is gone: the row is
#: demoted, not merely counted.
CLOSED_GO_VERDICTS = ("expired", "unreachable")

def _wiki_go_rows(wiki_root: Path) -> list[tuple[Path, str, str, str, str]]:
    """Every live GO/MAYBE row on file: (page, org, title, url, decision).

    Org-page blocks are date-headed (`### 2026-09-27 — Title`), so the org is
    the page's own `# Org` heading, not the block head.
    """
    import re as _re

    from wiki_store import org_page_paths

    rows = []
    # Both directories: an agency's page carries GO rows too (2026-10-15).
    for page in org_page_paths(Path(wiki_root)):
        text = page.read_text(encoding="utf-8", errors="ignore")
        head_m = _re.search(r"(?m)^# (.+?)\s*$", text)
        org = head_m.group(1).strip() if head_m else page.stem.replace("_", " ")
        for block in _re.split(r"(?m)^### ", text)[1:]:
            head = block.splitlines()[0] if block.splitlines() else ""
            if " — " not in head:
                continue
            title = head.split(" — ", 1)[1].strip().removesuffix("🆕").strip()
            decision = _re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE|DROP)\b", block)
            if not decision or decision.group(1) == "DROP":
                continue
            url_m = _re.search(r"(?m)^- \*\*URL\*\*: (\S+)", block)
            url = url_m.group(1).split(" ")[0] if url_m else ""
            if url.lower().startswith("http"):
                rows.append((page, org, title, url, decision.group(1)))
    return rows


def run_closed_audit(profile: str, wiki_root: Path, *, apply: bool,
                     checker=None) -> dict:
    """Demote the wiki GO/MAYBE rows whose advert has since closed.

    The newest-report re-check cannot see these rows: a GO scored on 2026-09-27
    leaves every later report, but its wiki row — and the pack the backfill
    cuts from it — lives on. So this step re-opens every live GO/MAYBE advert
    link on file (GO volume bounds the fetch count) and demotes the rows whose
    page carries a closure signal, in place, reusing the ruling shape the
    eligibility repair writes. A demoted row is DROP before the packs step
    runs, so no new pack is cut for a closed advert.

    Report-only by default: without `apply` it measures (the audit class
    counts the rows that WOULD be demoted) and never fetches or writes.
    `checker` is the `link_check.check_url` seam for tests.
    """
    import link_check

    out: dict = {"checked": 0, "demoted": 0, "classes": {}, "skipped": ""}
    rows = _wiki_go_rows(Path(wiki_root))
    if not rows:
        out["skipped"] = "no live GO/MAYBE rows on file"
        return out
    if not apply:
        out["skipped"] = (f"report-only pass: {len(rows)} live GO/MAYBE row(s) "
                           "would be re-opened")
        return out
    check = checker or link_check.check_url
    demoted = 0
    verdicts: list = []
    for page, org, title, url, _decision in rows:
        try:
            verdict = check(url, title, org=org)
        except Exception:
            continue                    # a refused check is not a defect
        out["checked"] += 1
        if verdict.verdict in CLOSED_GO_VERDICTS:
            verdicts.append(verdict)
        if verdict.verdict not in CLOSED_GO_VERDICTS:
            continue
        from wiki_repair import _apply_eligibility_ruling

        if verdict.verdict == "expired":
            reason = (f"advert closed (closed-advert audit: expiry marker "
                      f"{verdict.evidence!r} on the page)")
        else:
            reason = (f"advert unreachable (closed-advert audit: "
                      f"{verdict.detail or 'fetch failed'}".strip()[:100] + ")")
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        head, sep, tail = text.partition("### ")
        new_blocks, changed = [], False
        for block in tail.split("### ") if sep else []:
            full = "### " + block
            if url in full and _contains_decision(full, ("GO", "MAYBE")):
                full = _apply_eligibility_ruling(full, reason)
                changed = True
            new_blocks.append(full)
        if changed:
            page.write_text(head + "".join(new_blocks), encoding="utf-8")
            demoted += 1
    out["demoted"] = demoted
    if demoted:
        out["classes"] = {"closed_go_demoted": demoted}
    # **Record the evidence where the other reader looks** (2026-10-14). This pass
    # opens the page and finds the closure, and until now it kept the finding to
    # itself: it demoted the wiki row and discarded the verdict, so `retire` — which
    # decides the *piles* from `cache/link_checks/` — never saw it and the pack
    # stayed. One measurement, one record, two consumers: the same writer the world
    # pass uses (`link_check.save_machine_readable`).
    if verdicts and not out.get("record_error"):
        try:
            from datetime import date as _date

            from link_check import save_machine_readable
            from principal_runtime import resolve_principal_runtime

            runtime = resolve_principal_runtime(profile)
            counts: dict = {}
            for verdict in verdicts:
                counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
            out["recorded"] = str(save_machine_readable(
                runtime,
                Path(wiki_root) / "reports"
                / f"closed-audit-{_date.today():%Y-%m-%d}.md",
                {"counts": counts, "verdicts": verdicts}))
        except Exception as exc:        # the demotion stands; say what failed
            out["record_error"] = f"{type(exc).__name__}: {exc}"[:120]
    return out


def _contains_decision(block: str, decisions: tuple[str, ...]) -> bool:
    import re as _re

    return bool(_re.search(
        r"(?m)^- \*\*Decision\*\*: (%s)\b" % "|".join(decisions), block))



def _load_state(state_root: Path) -> dict:
    path = state_root / "cache" / STATE_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_state(state_root: Path, payload: dict) -> None:
    path = state_root / "cache" / STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _last_link_check(cache_dir: Path, report_path: Path) -> Optional[datetime]:
    """When this report's links were last checked, from the machine records.

    `save_machine_readable` writes `cache/link_checks/<report-stem>.json` and a
    timestamped sibling per re-check, so the newest file for this report is the
    cursor. Absent means never checked.
    """
    out_dir = Path(cache_dir) / "link_checks"
    if not out_dir.is_dir():
        return None
    stem = Path(report_path).stem
    newest: Optional[datetime] = None
    for path in out_dir.glob(f"{stem}*.json"):
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def run_links(profile: str, wiki_root: Path, *, apply: bool,
              days: int = 3, limit: int = 25) -> dict:
    """Check the newest report's advert links — and ACT on what comes back.

    LJ, 2026-10-08: *"checking for links is only useful if we do something with
    the results of the checks."* Before this step the results went to a report
    block and a per-org memory note, which is a record, not an action: nothing
    said "the advert behind GO #3 is dead" in the channel the principal reads,
    and a class of rot could not rise into an alarm.

    So this does three things the old cron could not:

      1. **curses** — re-checks only when the last check is older than `days`
         (the Tue/Thu crons became this step's cadence);
      2. **speaks** — each problem verdict becomes a `link_problem` event, which
         the watch's own ledger carries into the morning brief;
      3. **counts** — the verdicts enter the audited classes, so a rising rot
         class alarms exactly like a rising wiki class (`_compare`).

    The verdicts themselves, the report block, the sealed child receipt and the
    per-org memory mirror all stay where they were: `link_check.recheck` is the
    authority for all four, and this step does not duplicate it.
    """
    import link_check

    # Deliberately NOT `resolve_principal_runtime`: that binds config.CACHE_DIR /
    # config.WIKI_ROOT, which are import-time constants a test fixture does not
    # patch, so inside a test this step would hold live paths and could write to
    # the real state. All it needs is the principal's own cache dir — the same
    # root `run_pass` already works from (LJ's advisory, 2026-10-08).
    cache_dir = Path(config.STATE_DIR) / profile / "cache"
    report = link_check.newest_report(Path(wiki_root))
    out: dict = {"report": report.name if report else "", "checked": 0,
                 "classes": {}, "events": 0, "skipped": ""}
    if report is None:
        out["skipped"] = "no full report"
        return out
    last = _last_link_check(cache_dir, report)
    if last is not None:
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        if age_days < days:
            out["skipped"] = f"checked {age_days:.1f}d ago (< {days}d)"
            return out
    if not apply:
        out["skipped"] = "report-only pass: would re-check"
        return out

    result = link_check.recheck(report, limit=limit, principal=profile)
    out["checked"] = result["checked"]
    # `recheck` reports everything that is not `ok`, and that set includes
    # `skipped` — the checker declining to judge (a work-mode relaxation, a URL
    # that is not an advert). A skip is NOT a defect: counting it would put noise
    # in the alarm surface and in the brief, and noise is how an alarm gets
    # ignored. Only verdicts that say the advert failed to hold up count.
    defects = [v for v in result["problems"]
               if v.verdict in ("expired", "unreachable", "authwall",
                                "live_unmatched", "work_mode_conflict")]
    out["verdicts"] = result["counts"]
    counts: dict[str, int] = {}
    for verdict in defects:
        counts[f"link_{verdict.verdict}"] = counts.get(f"link_{verdict.verdict}", 0) + 1
    out["classes"] = counts

    # Speak: one event per problem verdict, on the watch's own ledger, so the
    # brief carries it the same morning. Watched content is data — the verdict
    # names the evidence; it never instructs.
    events_path = Path(config.STATE_DIR) / profile / "dataset" / "events.jsonl"
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for verdict in defects:
        rows.append({
            "schema_version": 1,
            "event_id": uuid4().hex,
            "kind": "link_problem",
            "principal": profile,
            "observed_at": at,
            "subject": f"{verdict.verdict}: {verdict.org} — {verdict.title}",
            "detail": {"verdict": verdict.verdict, "url": verdict.url,
                       "status": verdict.status, "org": verdict.org,
                       "title": verdict.title,
                       "report": report.name},
            "evidence_ref": f"wiki/reports/{report.name}",
            "cursor": {"from": "selfheal", "to": at},
        })
    if rows:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        out["events"] = len(rows)
    return out


def run_pass(profile: str, *, apply: bool = False) -> dict:
    """One self-healing pass over one principal. Returns the report dict."""
    import wiki_audit
    import wiki_org_repair
    import wiki_repair

    state_root = config.STATE_DIR / profile
    wiki_root = state_root / "wiki"
    repo_root = Path(config.LOVEWORK_ROOT)
    if not wiki_root.is_dir():
        return {"principal": profile, "error": f"no wiki at {wiki_root}"}

    out: dict = {"principal": profile, "at": datetime.now(timezone.utc)
                 .isoformat(timespec="seconds"), "applied": apply}
    if apply:
        plan = wiki_org_repair.plan_names(wiki_root)
        out["names"] = wiki_org_repair.apply_names(wiki_root, repo_root, plan)
        out["urls"] = wiki_org_repair.apply_urls(wiki_root)

    # links: URL work like `urls`, and before the shape normalisers so a block
    # this step appends is normalised in the same pass. It reports what is due on
    # a report-only pass and does the re-check only with --apply.
    out["links"] = run_links(profile, wiki_root, apply=apply,
                             days=LINK_DAYS, limit=LINK_LIMIT)

    # closed-advert audit (Sainsbury's, 2026-10-12): the newest-report re-check
    # above cannot see a GO that left every later report — the wiki row lives
    # on, and the packs step below cuts dossiers from it. Demote closed rows
    # HERE, before packs, so a dead advert gets DROP, never a new pack.
    try:
        out["closed_audit"] = run_closed_audit(profile, wiki_root, apply=apply)
    except Exception as exc:                 # never fail the pass over a fetch
        out["closed_audit"] = {"error": f"{type(exc).__name__}: {exc}",
                               "checked": 0, "demoted": 0, "classes": {}}
    out["rename_map"] = wiki_org_repair.rebuild_rename_map(wiki_root, repo_root)
    # Every GO must lead to prepared material — the pack is the product (LJ,
    # 2026-10-09). Runs before the audit, so the class it drains is measured
    # after the repair, exactly like the shapes step.
    try:
        from application_packs import prepare_missing_packs

        out["packs"] = prepare_missing_packs(profile, dry_run=not apply)
    except Exception as exc:                 # never fail the pass over a pack
        out["packs"] = {"error": f"{type(exc).__name__}: {exc}"}
    # Every judgement keeps its evidence: MAYBE, FLAG and DROP get a lead page
    # (LJ, 2026-10-09: "we have to have some information about the ad … we
    # analyze it - we save the result of our analysis").
    try:
        from leads import prepare_missing_leads

        out["leads"] = prepare_missing_leads(profile, dry_run=not apply)
    except Exception as exc:
        out["leads"] = {"error": f"{type(exc).__name__}: {exc}"}
    # `eligibility` too: the rows the principal's own rules forbid are ruled on
    # by the same predicate the matcher uses, so the wiki stops recommending
    # them (LJ's Gradium ruling, 2026-10-09) and the audit class drains itself
    # instead of sitting at 236 forever.
    out["shapes"] = wiki_repair.repair_wiki(
        wiki_root, dedupe_blocks=True, normalize_notes=True,
        normalize_report_blocks=True, unescape_entities=True,
        fix_no_url=True, eligibility=True, cooldowns=True, junk_titles=True,
        implausible_locations=True, local_links=True,
        dry_run=not apply)
    # **Rebuild the index after the rulings** (2026-10-17). Every repair here changes
    # *verdicts* — eligibility, re-check, cooldown — and the index is built by the
    # crawl, so a ruling that lands after the crawl's own rebuild leaves the index
    # showing a verdict the page no longer holds. LJ's PostHog case is exactly that:
    # the page had been ruled DROP, the pack said `SUBMITTED 2026-10-16 · REJECTED
    # 2026-10-17`, and the index still listed it as `PACK … 8.9/10`. One rebuild here
    # closes the whole class, because every repair feeds it.
    # **Org-memory slugs self-heal too** (2026-10-22). The 2026-10-05 repair
    # was host-local, and every host-a↔host-b merge re-imports the raw
    # pre-slug directory names (measured: 133 raw dirs across the three
    # principals on 2026-10-22, four of them freshly minted same-day) — the
    # exact "a repair is host-local until it is re-applied" class the wiki
    # repairs already pay for. `normalize_memory_slugs` is the one authority
    # (`mem.slug`); riding the daily pass means no merge can reintroduce the
    # class for longer than a day.
    try:
        import normalize_memory_slugs as _nms

        moves = _nms.apply_for_profile(principal, apply=apply)
        if moves:
            out["memory_slugs"] = f"{moves} move(s)"
    except Exception as exc:              # never fail the pass over memories
        out["memory_slugs"] = f"{type(exc).__name__}: {exc}"[:120]
    if apply:
        try:
            from wiki_store import WikiStore

            store = WikiStore(root=wiki_root)
            store.rebuild_index(store._entries_from_org_pages(
                decisions=("GO", "MAYBE", "FLAG", "DROP")))
            out["index_rebuilt"] = True
        except Exception as exc:              # never fail the pass over the index
            out["index_rebuilt"] = f"{type(exc).__name__}: {exc}"[:120]
    report = wiki_audit.audit_wiki(wiki_root)
    out["classes"] = report["classes"]
    out["gaps"] = report.get("gaps", {})

    # A dead advert link is a defect like any other, and must be able to ALARM:
    # folded into the same class map, so "link_expired rose from 2 to 5" reads
    # exactly like "duplicate_org_blocks rose" (LJ: act on the check results).
    # The closed-advert audit joins the same map: a demoted GO is a defect the
    # pass repaired, and a rise in "closed_go_demoted" says adverts are dying
    # faster than the crawls notice.
    link_classes = dict((out.get("links") or {}).get("classes") or {})
    for name, count in ((out.get("closed_audit") or {}).get("classes") or {}).items():
        link_classes[name] = link_classes.get(name, 0) + count
    for name, count in link_classes.items():
        out["classes"][name] = out["classes"].get(name, 0) + count
    out["total"] = report["total"] + sum(link_classes.values())
    return out


def _compare(previous: dict, classes: dict) -> list[str]:
    """Classes that ROSE since the last pass — the alarm, not the total.

    A first pass has no baseline and must be silent (the same quiet-first-run
    rule the watchtower follows): reporting every class as "rising" because
    nothing was recorded before is the false alarm that teaches a reader to
    ignore the line.
    """
    prior = previous.get("classes")
    if not prior:
        return []
    # A class the baseline has never seen cannot be said to have RISEN. A
    # genuinely new class enters the counts and the total without crying wolf on
    # its first appearance — the same quiet-first-run rule the watchtower
    # follows, applied one level down (and it is what lets the link classes join
    # this map without a false alarm on the day they are introduced).
    return sorted(name for name, n in classes.items()
                  if name in prior and n > prior[name])


def selfheal(profile: str, *, apply: bool = False) -> dict:
    """One guarded pass: take the writer lease, heal, measure, record."""
    state_root = config.STATE_DIR / profile
    try:
        with standalone_writer_guard(profile, mode="direct",
                                     state_root=state_root, shared=()):
            previous = _load_state(state_root)
            result = run_pass(profile, apply=apply)
            if "error" in result:
                return result
            result["rose"] = _compare(previous, result["classes"])
            result["previous_total"] = previous.get("total")
            # The baseline is a MEASUREMENT cursor, so it advances on every
            # pass; only the repairs need `--apply`. `rose` is persisted WITH it
            # — the watch reads this file and reports "rising: <classes>" in the
            # brief, and until 2026-10-08 the key was computed and then dropped,
            # so the alarm reached stdout and never the brief.
            _save_state(state_root, {
                **{k: result[k] for k in ("at", "total", "classes", "gaps")},
                "rose": list(result.get("rose") or []),
                "applied": bool(apply),
            })
            return result
    except WriterBusy as busy:
        return {"principal": profile, "busy": True,
                "error": f"writer busy: {busy.resource.key}",
                "attempt": str(getattr(busy, "attempt_path", ""))}


def _print(result: dict) -> int:
    if result.get("busy"):
        print(f"{result['principal']}: [LOCK] {result['error']} — retry later")
        return 75
    if "error" in result:
        print(f"{result['principal']}: {result['error']}", file=sys.stderr)
        return 1
    print(f"{result['principal']}: {result['total']} defect(s) on file "
          f"(was {result['previous_total']})")
    for name, count in sorted(result["classes"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:26s} {count}")
    for name, count in sorted(result.get("gaps", {}).items(), key=lambda kv: -kv[1]):
        print(f"  (gap) {name:20s} {count}")
    links = result.get("links") or {}
    if links.get("report"):
        state = (f"checked {links['checked']}" if links.get("checked")
                 else links.get("skipped") or "not checked")
        print(f"  links: {links['report']} — {state}"
              + (f", {links['events']} problem event(s)" if links.get("events") else ""))
    if result.get("rose"):
        print(f"  ALARM: rising since the last pass: {', '.join(result['rose'])}")
    print(f"  shapes: {result['shapes']}")
    if result.get("packs"):
        print(f"  packs: {result['packs']}")
    if result.get("leads"):
        print(f"  leads: {result['leads']}")
    print(f"  rename map: {result['rename_map']}")
    return 2 if result.get("rose") else (1 if result["total"] else 0)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--all", action="store_true", help="every principal")
    parser.add_argument("--apply", action="store_true",
                        help="write the repairs (default: measure and plan only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    profiles = ([p.name for p in sorted(config.STATE_DIR.iterdir())
                 if (p / "wiki").is_dir()] if args.all else [args.profile])
    results = [selfheal(p, apply=args.apply) for p in profiles]
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for r in results:
            _print(r)
    if any(r.get("busy") for r in results):
        return 75
    if any(r.get("rose") for r in results):
        return 2
    return 1 if any(r.get("total") for r in results) else 0


if __name__ == "__main__":
    sys.exit(_main())

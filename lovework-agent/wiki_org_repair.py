#!/usr/bin/env python3
"""Wiki repair — bring a principal's org pages back to the naming and URL rules.

Report-only by default; `--apply` performs the actions. Every action is
recorded, nothing is deleted (a page that loses its own identity is moved to
`wiki/_attic/`, and its findings are merged into a page that keeps them).

Two classes, both found by `wiki_audit.py` on 2026-10-07:

  names   a page whose name is not an organisation, or carries a tail the
          source concatenated onto it. Three outcomes, in this order:
            1. `clean_org_name` recovers the company (`LAGO - https://…` →
               `LAGO`, `Tesco&nbsp;Welwyn …` → `Tesco`) → RENAME, or MERGE
               when a page with that name already exists;
            2. the name is not an organisation but contains an existing page's
               name as a prefix (`DeepMind_Student_Researcher`) → MERGE into
               that page — evidence, not a guess;
            3. nothing identifies an organisation (`Results from the new
               AI-powered job search`) → FOLD into `_Unattributed`, keeping the
               raw name on every migrated block.
  urls    advert URLs still carrying tracking params (`wiki_store.clean_url`
          cleans at write time; pages written before that keep the tail).

Prevention lives at the write path (`lead_identity.is_non_org_name` in
`wiki_store._canonical_org_for_new`); this tool repairs what already exists.

Usage:
    wiki_org_repair.py --profile lj [--apply] [--names|--urls] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from lead_identity import (_looks_like_prose, clean_org_name, is_noise_company,
                           is_non_org_name)

#: The junk bucket `wiki_store` routes un-named findings to. Never renamed,
#: never merged away: it is the honest home for "we could not tell who this is".
#: The name comes from the writer, not from a second spelling here.
from wiki_store import UNATTRIBUTED_ORG as UNATTRIBUTED, is_canonical_block
ATTIC = "_attic"

#: One finding block: a `### ` heading plus its `- **Field**:` lines. Two shapes
#: exist — the crawl report's `### <date> — <title>`, and the backfilled page's
#: `### <title>` (no date; `wiki_backfill.py` rebuilt 956 pages that way) — so
#: the heading is what delimits a block, not the date.
BLOCK_RE = re.compile(r"^### [^\n]*\n(?:(?!###)[^\n]*\n?)*", re.MULTILINE)
H1_RE = re.compile(r"^# (.+)$", re.M)
URL_LINE_RE = re.compile(r"^(\s*-\s*\*\*URL\*\*:\s*)(\S+)\s*$", re.M)

#: A stem must be at least this long to be a recovery target, so a two-letter
#: page cannot swallow every name that begins with it.
MIN_RECOVERY_STEM = 4

#: A name that reads as prose is only recovered onto a page name this long —
#: short page names ("Build", "CLEAR", "London") are as likely to be an
#: unrelated company or a junk bucket of their own.
MIN_DISTINCTIVE_STEM = 6


def _collision_key(name: str) -> str:
    """Two names that a reader would call the same page."""
    return (name or "").casefold().replace("-", "_")


def _fold_key(name: str) -> str:
    """A comparison key that ignores the marks a stem may have lost."""
    import unicodedata
    text = re.sub(r"[®™©]", "", name or "")
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _stem(name: str) -> str:
    """The page stem for a name — the writer's own rule, not a copy of it."""
    from wiki_store import WikiStore
    return WikiStore._safe_filename(name)


def _blocks(text: str) -> list[str]:
    return BLOCK_RE.findall(text)


def _preamble(text: str) -> str:
    first = BLOCK_RE.search(text)
    return text[:first.start()] if first else text


def plan_names(wiki_root: Path) -> dict:
    """Decide what to do with every org page. Pure read — no writes."""
    orgs = wiki_root / "orgs"
    stems = sorted(p.stem for p in orgs.glob("*.md"))
    # `-` and `_` are the same separator to a reader ("amodo-design" and
    # "Amodo_Design" are one company), so they must be one key here too.
    present = {_collision_key(s): s for s in stems}

    def recovery_target(stem: str) -> str | None:
        """The longest existing page whose name is a prefix of `stem`.

        A prose name is only recovered onto a *distinctive* page name (>= 6
        characters): "Barclays payments acceptance business provides …" is
        Barclays, while "Build and maintain trusted relationships …" must not be
        folded onto the unrelated page named "Build".
        """
        low = stem.casefold()
        prose = _looks_like_prose(stem.replace("_", " "))
        best = None
        for other in stems:
            if other == stem or len(other) < MIN_RECOVERY_STEM:
                continue
            if prose and len(other) < MIN_DISTINCTIVE_STEM:
                continue
            # Never recover onto a page that is itself not an organisation:
            # "London Hybrid" belongs in the junk bucket, not on a page called
            # "London" that is itself a place.
            if is_non_org_name(other):
                continue
            o = other.casefold()
            if low.startswith(o) and (len(low) == len(o) or low[len(o)] in "_- "):
                if best is None or len(other) > len(best):
                    best = other
        return best

    actions: list[dict] = []
    for stem in stems:
        if stem == UNATTRIBUTED:
            continue
        # A stem that lost characters its own H1 kept ("Deutsche_B_rse" for
        # "Deutsche Börse") is renamed from the H1, which still carries them.
        text = (orgs / f"{stem}.md").read_text(encoding="utf-8", errors="ignore")
        h1 = H1_RE.search(text)
        if h1:
            title = h1.group(1).strip()
            from_h1 = _stem(title)
            # Only when the H1 carries a character the stem could not hold: a
            # stem is ASCII, so a non-ASCII character in the title is the
            # mangling signature ("Deutsche_B_rse" ← "Deutsche Börse"). A page
            # whose H1 is *itself* the defect is handled by the cleaning rules.
            if from_h1 and any(ord(c) > 127 for c in title) \
                    and not is_non_org_name(title) \
                    and _fold_key(from_h1) != _fold_key(stem):
                target = present.get(_collision_key(from_h1))
                actions.append({
                    "action": "merge" if target and target != stem else "rename",
                    "page": stem, "into": target if target and target != stem else from_h1,
                    "why": f"the H1 keeps characters the stem lost ({h1.group(1)[:40]})"})
                continue
        cleaned = clean_org_name(stem)
        candidate = _stem(cleaned) if cleaned else ""
        if candidate and candidate != stem and not is_non_org_name(candidate):
            target = present.get(_collision_key(candidate))
            if target and target != stem:
                actions.append({"action": "merge", "page": stem, "into": target,
                                "why": f"cleans to an existing page ({candidate})"})
                continue
            # Every remaining rule in `clean_org_name` strips a *mechanical*
            # tail (a URL, a `&nbsp;` join, a "formerly" clause, a city+state),
            # so the cleaned name is trustworthy whatever its length. Guessing a
            # head out of a prose name is deliberately NOT done — that is the
            # prefix recovery below, which requires an existing page as
            # evidence.
            actions.append({"action": "rename", "page": stem, "into": candidate,
                            "why": f"recovers the name from the tail ({cleaned})"})
            continue
        # `is_noise_company` is the audit's `junk_org_name` predicate: a name
        # that is a salary band, a contract type, a UI string or an age. The
        # two predicates must agree or the tool cannot repair what the audit
        # reports — "Competitive" and "Negotiable" were flagged for a day with
        # no planned action (2026-10-08).
        if is_non_org_name(stem) or is_noise_company(stem.replace("_", " ")):
            target = recovery_target(stem)
            if target:
                actions.append({"action": "merge", "page": stem, "into": target,
                                "why": "not an organisation, but its name begins "
                                       "with an existing page"})
            else:
                actions.append({"action": "fold", "page": stem, "into": UNATTRIBUTED,
                                "why": "not an organisation, and nothing in the name "
                                       "identifies one"})
            continue
        # Last, and only for a page that stays where it is: an H1 that still
        # reads as the *old* name (a page renamed before this rule existed)
        # disagrees with the page name, and the H1 is the page's title.
        if h1:
            title = h1.group(1).strip()
            want = clean_org_name(title)
            # The H1 carries a tail the page name no longer has ("40GRID -
            # Full-time Remote" above a page called 40GRID) and it cleans to
            # *this* page — so the title is the only thing left to fix. A title
            # that merely spells the name differently ("St. James's Place") is
            # left alone: it is the proper spelling.
            if (want != title and _stem(want) == stem
                    and not is_non_org_name(want)):
                actions.append({"action": "retitle", "page": stem, "into": want,
                                "why": f"the H1 still carries its tail ({title[:40]})"})
    # Two pages a reader would call one page ("amodo-design" / "Amodo_Design"):
    # the smaller merges into the larger, so nothing is lost.
    by_collision: dict = {}
    for stem in stems:
        by_collision.setdefault(_collision_key(stem), []).append(stem)
    moved = {a["page"] for a in actions}
    for group in by_collision.values():
        if len(group) < 2:
            continue
        keep = max(group, key=lambda s: (orgs / f"{s}.md").stat().st_size)
        for stem in group:
            if stem != keep and stem not in moved:
                actions.append({"action": "merge", "page": stem, "into": keep,
                                "why": "two pages for one company"})

    return {"wiki": str(wiki_root), "org_pages": len(stems), "actions": actions}


def plan_urls(wiki_root: Path) -> dict:
    """Pages whose advert URLs still carry tracking params. Pure read."""
    from wiki_store import clean_url
    orgs = wiki_root / "orgs"
    dirty: list[dict] = []
    for path in sorted(orgs.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits = [(m.group(2), clean_url(m.group(2)))
                for m in URL_LINE_RE.finditer(text)]
        changed = [(old, new) for old, new in hits if new != old]
        if changed:
            dirty.append({"page": path.stem, "urls": len(changed),
                          "example": changed[0][0][:80]})
    return {"wiki": str(wiki_root), "pages_with_tracking_urls": len(dirty),
            "urls": sum(d["urls"] for d in dirty), "pages": dirty}


def _migrate_block(block: str, source_stem: str, raw_name: str) -> str:
    """One migrated block, carrying its provenance — and in the canonical shape.

    The raw name stays on the block: the point of the junk bucket is to keep
    what we could not attribute, not to launder it into a company.

    The shape is normalised on the way through, because a move must not
    propagate a defect: this tool merges and folds whole blocks into a target
    page, so a block in the *report's* shape (a numbered heading, `**Score**`,
    no date) used to travel into the target and reappear as
    `foreign_block_schema`. That is exactly how host-b's Patch-Wednesday run
    (f016ce63) put 37 report-shaped blocks into org pages on 2026-10-08 — the
    migration did not create them, it carried them.
    """
    from wiki_repair import normalize_note_block, normalize_report_block

    full = block if block.startswith("### ") else "### " + block
    if not is_canonical_block(full):
        fixed = normalize_note_block(block) or normalize_report_block(full, raw_name)
        if fixed is not None:
            block = ("".join(fixed) if isinstance(fixed, list)
                     else fixed).lstrip("\n")
            if block.startswith("### "):
                block = block[4:]
    marker = (f"<!-- migrated 2026-10-07 from {source_stem} "
              f"(raw name: {raw_name}) -->\n")
    return marker + block


def _retitle(path: Path, stem: str) -> None:
    """Point the page's H1 at its new name, keeping any non-ASCII spelling."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = H1_RE.search(text)
    if not m:
        return
    title = m.group(1).strip()
    if any(ord(c) > 127 for c in title):
        return                      # the title is the proper spelling already
    new_title = stem.replace("_", " ").strip()
    if new_title and new_title != title:
        path.write_text(H1_RE.sub(f"# {new_title}", text, count=1), encoding="utf-8")


def record_rename(wiki_root: Path, old_stem: str, new_stem: str) -> None:
    """Register a page move in the principal's rename map.

    The map IS the reusable resolver: `doc_serve.resolve_wiki_org` consults it
    before falling back to a fold, so a sealed report's link to a moved page
    still lands on the evidence. Nothing wrote it after the 2026-10-07
    migration — this tool renamed, merged, folded and atticked pages for a day
    without a single map entry — so every move manufactured a dead link the
    resolver could never repair ("Competitive" and "Negotiable" were folded
    into `_Unattributed` on 2026-10-08 and their reports went dead; the map
    still held 104 entries, all from the migration).
    """
    if not old_stem or not new_stem or old_stem == new_stem:
        return
    path = wiki_root / "org-renames.json"
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    if data.get(old_stem) == new_stem:
        return
    data[old_stem] = new_stem
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


PROVENANCE_RE = re.compile(r"<!--\s*migrated[^>]*?from\s+([^\s>]+)")


def _wiki_git_root(wiki_root: Path, repo_root: Path) -> Path:
    """The git worktree that tracks `wiki_root`, nearest first.

    After the Part B split the wiki lives in the principal's repo, not the
    engine checkout: `state/example/wiki` is a symlink into
    `lovework-principal-lj`. `config.LOVEWORK_ROOT` is still the right answer
    for the single-repo layout; walking up from the resolved wiki finds the
    repo that actually tracks it. Using the engine root unconditionally made
    `orgs.relative_to(repo_root)` raise, so self-heal died before its audit and
    the maintenance wrapper marked the daily pass done (2026-11-04).
    """
    resolved = wiki_root.resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return parent
    return repo_root


def rebuild_rename_map(wiki_root: Path, repo_root: Path) -> dict:
    """Fill the rename map from the evidence the moves already left behind.

    Two independent sources, both on disk, neither a guess:

    * the provenance comment a migration writes into the page that KEEPS the
      findings (`<!-- migrated 2026-10-07 from <old_stem> -->`), which says
      where an old page's evidence went;
    * git's own rename detection (`git log --diff-filter=R`), which says what
      was renamed to what.

    Existing entries are never overwritten: a recorded move is a stronger
    statement than a re-derivation. Without this the map described only the
    2026-10-07 migration (lj 104 entries) while ~250 later moves — attic
    moves, folds, renames — were invisible to the resolver, so their reports
    stayed dead.
    """
    import subprocess

    orgs = wiki_root / "orgs"
    found: dict[str, str] = {}
    for f in sorted(orgs.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in PROVENANCE_RE.finditer(text):
            found.setdefault(m.group(1), f.stem)
    git_root = _wiki_git_root(wiki_root, repo_root)
    try:
        orgs_rel = orgs.resolve().relative_to(git_root)
    except ValueError:
        orgs_rel = None
    lines: list[str] = []
    if orgs_rel is not None:
        proc = subprocess.run(
            ["git", "log", "--diff-filter=R", "--name-status", "--format=",
             "--", str(orgs_rel)],
            cwd=str(git_root), capture_output=True, text=True)
        lines = proc.stdout.split("\n")
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            found.setdefault(Path(parts[1]).stem, Path(parts[2]).stem)

    path = wiki_root / "org-renames.json"
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = {}
    added = {k: v for k, v in found.items()
             if k not in existing and k != v and (orgs / f"{v}.md").is_file()}
    if added:
        merged = {**existing, **added}
        path.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    return {"candidates": len(found), "added": len(added),
            "total": len(existing) + len(added)}


def apply_names(wiki_root: Path, repo_root: Path, result: dict) -> dict:
    """Perform the planned actions. Returns counts and the failures, if any."""
    orgs = wiki_root / "orgs"
    attic = wiki_root / ATTIC
    attic.mkdir(exist_ok=True)
    done = {"rename": 0, "merge": 0, "fold": 0, "retitle": 0}
    failures: list[str] = []

    def git_mv(src: Path, dst: Path) -> None:
        """Move a page, keeping git history where git can follow it."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        git_root = _wiki_git_root(wiki_root, repo_root)
        try:
            src_rel = src.resolve().relative_to(git_root)
            dst_rel = dst.resolve().relative_to(git_root)
        except ValueError:
            src.rename(dst)
            return
        proc = subprocess.run(
            ["git", "mv", str(src_rel), str(dst_rel)],
            cwd=str(git_root), capture_output=True, text=True)
        if proc.returncode == 0:
            return
        # Outside the repo (a fixture), or git mv refused an untracked source:
        # a plain move keeps the bytes even when history cannot follow them.
        src.rename(dst)

    for act in result["actions"]:
        src = orgs / f"{act['page']}.md"
        if not src.is_file():
            failures.append(f"{act['page']}: source page missing")
            continue
        try:
            if act["action"] == "retitle":
                _retitle(src, act["into"].replace(" ", "_"))
                done["retitle"] = done.get("retitle", 0) + 1
            elif act["action"] == "rename":
                dst = orgs / f"{act['into']}.md"
                # The H1 is the page's title: leaving the old (junk or mangled)
                # title above a corrected name is a defect of its own.
                _retitle(src, act["into"])
                git_mv(src, dst)
                record_rename(wiki_root, act["page"], act["into"])
                done["rename"] += 1
            elif act["action"] in ("merge", "fold"):
                target = orgs / f"{act['into']}.md"
                moved = "".join(
                    _migrate_block(b, act["page"], act["page"])
                    for b in _blocks(src.read_text(encoding="utf-8", errors="ignore")))
                if not moved:
                    # A page with no blocks (a stub) has nothing to migrate —
                    # it still belongs in the attic, not left as a defect.
                    git_mv(src, attic / f"{act['page']}.md")
                    done[act["action"]] += 1
                    continue
                with target.open("a", encoding="utf-8") as fh:
                    fh.write("\n" + moved)
                git_mv(src, attic / f"{act['page']}.md")
                # The evidence now lives on the target page, so a report's link
                # to the old name must resolve there.
                record_rename(wiki_root, act["page"], act["into"])
                done[act["action"]] += 1
        except Exception as e:                      # never silently swallow
            failures.append(f"{act['page']}: {type(e).__name__}: {e}")
    return {"applied": done, "failures": failures}


def apply_urls(wiki_root: Path) -> dict:
    """Strip tracking params from every advert URL line. Idempotent."""
    from wiki_store import clean_url
    orgs = wiki_root / "orgs"
    changed_pages = changed_urls = 0
    for path in sorted(orgs.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        new_text, n = URL_LINE_RE.subn(
            lambda m: f"{m.group(1)}{clean_url(m.group(2))}", text)
        if n and new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed_pages += 1
            changed_urls += sum(
                1 for m in URL_LINE_RE.finditer(text) if clean_url(m.group(2)) != m.group(2))
    return {"pages_rewritten": changed_pages, "urls_cleaned": changed_urls}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="perform the actions (default: report only)")
    parser.add_argument("--names", action="store_true", help="names class only")
    parser.add_argument("--urls", action="store_true", help="URL class only")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--rebuild-map", action="store_true",
                        help="fill org-renames.json from the moves on disk")
    args = parser.parse_args(argv)

    wiki_root = config.STATE_DIR / args.profile / "wiki"
    if not wiki_root.is_dir():
        print(f"no wiki at {wiki_root}", file=sys.stderr)
        return 1
    do_names = args.names or not args.urls
    do_urls = args.urls or not args.names
    repo_root = Path(config.LOVEWORK_ROOT)

    out: dict = {"principal": args.profile, "applied": args.apply}
    if args.rebuild_map:
        out["rename_map"] = rebuild_rename_map(wiki_root, repo_root)
    if do_names:
        names = plan_names(wiki_root)
        counts: dict = {}
        for act in names["actions"]:
            counts[act["action"]] = counts.get(act["action"], 0) + 1
        out["names"] = {"plan": counts, "actions": names["actions"]}
        if args.apply:
            out["names"]["result"] = apply_names(wiki_root, repo_root, names)
    if do_urls:
        urls = plan_urls(wiki_root)
        out["urls"] = {"pages": urls["pages_with_tracking_urls"],
                       "urls": urls["urls"]}
        if args.apply:
            out["urls"]["result"] = apply_urls(wiki_root)

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    print(f"{args.profile}: {'APPLIED' if args.apply else 'plan only'}")
    if do_names:
        plan = out["names"]["plan"]
        print(f"  names: rename {plan.get('rename', 0)}, merge {plan.get('merge', 0)}, "
              f"fold {plan.get('fold', 0)}")
        for act in out["names"]["actions"]:
            print(f"    {act['action']:6s} {act['page'][:56]:58s} -> {act['into'][:34]}")
        if args.apply:
            print(f"    applied: {out['names']['result']['applied']}")
            for f in out["names"]["result"]["failures"]:
                print(f"    FAILED: {f}")
    if do_urls:
        print(f"  urls: {out['urls']['pages']} page(s), {out['urls']['urls']} URL(s)")
        if args.apply:
            print(f"    {out['urls']['result']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

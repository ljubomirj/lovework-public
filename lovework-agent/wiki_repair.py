"""One-time, idempotent repairs over a principal's org pages (2026-10-07).

The wiki's writers are fixed so the defects cannot be created again; this tool
repairs what is already on disk. It only ever touches `wiki/orgs/` — the
sealed reports are history and are never rewritten (their links are resolved
at the serving layer and reported by `wiki_audit.py` instead).

  --dedupe-blocks   one advert, one block per observation day. The page writer
                    used to append unconditionally, so an advert reassessed
                    ten times in one run — or re-ingested from the same email
                    archive four times in a week — accumulated a block per
                    assessment: lj 3,431 redundant blocks (26% of the page
                    body), vj 898, kj 1,377. The LAST block wins (it is the
                    newest verdict) and a provenance comment records what was
                    collapsed.

Usage:
    wiki_repair.py --profile lj --dedupe-blocks [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import config

BLOCK_SPLIT_RE = re.compile(r"(?m)^### ")
H1_RE = re.compile(r"(?m)^# (.+)$")
STAMP = "2026-10-07"


def _org_name(text: str, path: Path) -> str:
    m = H1_RE.search(text)
    return m.group(1).strip() if m else path.stem.replace("_", " ")


def dedupe_page(text: str, org: str):
    """Collapse repeated (advert identity, observation day) blocks.

    Returns (new_text, collapsed_count).
    """
    from wiki_store import block_day, block_identity

    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    groups: dict[tuple, list[int]] = {}
    for i, block in enumerate(blocks):
        full = "### " + block
        groups.setdefault((block_identity(org, full), block_day(full)), []).append(i)
    drop: set[int] = set()
    comment: dict[int, str] = {}
    collapsed = 0
    for _, idxs in groups.items():
        if len(idxs) < 2:
            continue
        drop.update(idxs[:-1])
        collapsed += len(idxs) - 1
        comment[idxs[-1]] = (
            f"<!-- repair {STAMP}: {len(idxs) - 1} duplicate block(s) for this "
            f"advert and day collapsed; kept the newest verdict -->\n")
    if not collapsed:
        return text, 0
    out = [prefix]
    for i, block in enumerate(blocks):
        if i in drop:
            continue
        out.append(comment.get(i, "") + "### " + block)
    return "".join(out), collapsed


CANONICAL_DATE_RE = re.compile(r"^### \d{4}-\d{2}-\d{2} — ")
MALFORMED_NOTE_RE = re.compile(
    r"(?m)^- \*\*Reasoning\*\*: ### (?P<day>\d{4}-\d{2}-\d{2}) — prior contact found")
SOURCE_ENTRY_RE = re.compile(r"(?m)^- \*\*Source entry\*\*: (?P<value>.+)$")
PRIOR_CONTACT_RE = re.compile(r"(?m)^- \*\*Prior contact\*\*: (?P<value>.+)$")
NOTE_URL_RE = re.compile(r"(https?://\S+)")


def normalize_note_block(block: str):
    """Rewrite a hand-built prior-contact block into the canonical note shape.

    `crosscheck` passed a whole markdown block as `reasoning`, so the page got
    `### DATE — (prior-contact cross-check)`, an empty `**Decision**` and a
    second `### ` heading *inside* the block — 49 in lj, 6 in vj (2026-10-07).
    The writer is fixed; this repairs what it already wrote.
    """
    from wiki_store import render_org_note

    m = MALFORMED_NOTE_RE.search(block)
    if not m:
        return None
    entry = SOURCE_ENTRY_RE.search(block)
    contact = PRIOR_CONTACT_RE.search(block)
    url = ""
    if entry:
        um = NOTE_URL_RE.search(entry.group("value"))
        url = um.group(1) if um else ""
    return render_org_note(
        "prior contact found", m.group("day"), "prior-contact cross-check",
        "cross_check",
        {"URL": url,
         "Source entry": entry.group("value").strip() if entry else "",
         "Prior contact": contact.group("value").strip() if contact else ""},
    )


def normalize_notes_page(text: str) -> tuple[str, int]:
    """Rewrite every malformed note block on one page.

    Two shapes: the hand-built prior-contact block (a nested heading, an empty
    Decision), and a block with a canonical date heading but *no* verdict line
    at all — the GO-pack diligence note ("### 2026-10-06 — Company
    investigation (GO pack diligence)") that an agent wrote straight into the
    page. The second gains `- **Note**: <its own title>`, which is what the
    heading already says.
    """
    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    changed = 0
    out = [prefix]
    for block in blocks:
        fixed = normalize_note_block(block)
        if fixed is not None:
            changed += 1
            out.append("\n".join(fixed))
            continue
        full = "### " + block
        if (CANONICAL_DATE_RE.match(full)
                and "- **Decision**" not in block and "- **Note**" not in block):
            head = block.split("\n", 1)[0]
            title = head.split(" — ", 1)[1].strip() if " — " in head else ""
            body = block.split("\n", 1)[1] if "\n" in block else ""
            out.append(f"### {head}\n\n- **Note**: {title.lower()}\n"
                       f"{body.rstrip()}\n"
                       f"<!-- normalised {STAMP}: the block had no verdict line; "
                       f"it is a note, not a finding -->\n\n")
            changed += 1
            continue
        out.append(full)
    return "".join(out), changed


REPORT_HEADING_RE = re.compile(r"^###\s+(?:\d+\.\s+)?(?P<org>.*?) — (?P<title>.*?)\s*$")
ACTION_RE = re.compile(r"(?m)^- \*\*Action\*\*: (\S+)")


def normalize_report_block(block: str, page_org: str, decisions: dict | None = None):
    """Fix the SHAPE of a report block that leaked into an org page.

    The org pages carry 37 blocks in the report's shape (`### 8. Amodo Design
    (amododesign.com) — Sheffield/London, UK`, `- **Score**: 3.5/10`,
    `- **Action**: WATCH`, no date): some writer appended report blocks to org
    pages, and the org-page reader needs `### DATE — Title` plus a verdict line,
    so these blocks are invisible to the index and every date-anchored rule.

    The verdict is NOT recovered by guessing. Two candidate inverses were
    measured and both are lossy:

    * `matcher._decision_from_action(**Action**)` — the report chose its section
      with this mapping, but the model can return `decision=FLAG` with
      `action=WATCH`, so decoding gives the wrong bucket in 11 of 37 blocks;
    * looking the advert up in the sealed reports — the same advert was decided
      differently by different runs, so 35 of 37 are ambiguous.

    So the block keeps every field it has (`**Score**`, `**Action**`,
    `**Reasoning**`, …) verbatim, gains the observation date from its own
    "Found via" line, and states that the coarse verdict is not recorded here.
    The index will not list it as a GO/MAYBE/FLAG — which is correct, because
    nobody knows which bucket it was in.
    """
    from wiki_backfill import _entry_from_report_block

    head = block.split("\n", 1)[0]
    m = REPORT_HEADING_RE.match(head)
    if not m:
        return None
    title = m.group("title").strip().removesuffix("🆕").strip()
    # The date comes from the block's OWN evidence, never from the clock:
    # `WikiEntry.date` defaults to today, so trusting it would stamp every
    # undated block with the day the repair ran (2026-10-08) — an invented
    # observation date, which is the class this whole sweep is about.
    dm = (re.search(r"\*\*Found via\*\*:.*?\((\d{4}-\d{2}-\d{2})\)", block)
          or re.search(r"observed since (\d{4}-\d{2}-\d{2})", block))
    if not dm:
        return None
    day = dm.group(1)
    decision = (decisions or {}).get(_block_key(block))
    if decision:
        verdict = f"- **Decision**: {decision}"
        note = (f"<!-- normalised {STAMP}: verdict {decision} from the sealed "
                f"report that carries this advert -->")
    else:
        verdict = ("- **Decision**: UNSCORED (verdict not recorded on this page — "
                   "block copied from a report)")
        note = (f"<!-- normalised {STAMP} from the report block shape; the "
                f"verdict was never recorded here -->")
    body = block.split("\n", 1)[1].rstrip("\n")
    return f"### {day} — {title}\n\n{verdict}\n{body}\n{note}\n\n"


def _block_key(block: str) -> str:
    """The advert's identity inside a report block (URL, else org+title)."""
    um = re.search(r"(?m)^- \*\*URL\*\*: (\S+)", block)
    if um:
        return "url:" + um.group(1)
    hm = REPORT_HEADING_RE.match(block.split("\n", 1)[0])
    if hm:
        return "ot:" + hm.group("org").lower() + "|" + hm.group("title").lower()
    return ""


def unique_report_decisions(reports_dir: Path) -> dict:
    """Advert -> the one decision every sealed report agrees on.

    Only an *unambiguous* verdict is usable: the same advert was decided
    differently by different runs (35 of the 37 leaked blocks), so a lookup
    that took "the newest" would silently pick one run's opinion.
    """
    from wiki_backfill import _report_entries

    seen: dict[str, set] = {}
    if not reports_dir.is_dir():
        return {}
    for rep in sorted(reports_dir.glob("*.md")):
        for _stem, entry, _ident in _report_entries(rep):
            key = ("url:" + entry.url) if entry.url else (
                f"ot:{entry.org_name.lower()}|{entry.title.lower()}")
            seen.setdefault(key, set()).add(entry.decision)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def normalize_report_blocks_page(text: str, org: str, decisions=None):
    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    out = [prefix]
    changed = 0
    for block in blocks:
        full = "### " + block
        if CANONICAL_DATE_RE.match(full):
            out.append(full)
            continue
        fixed = normalize_report_block(full, org, decisions)
        if fixed is None:
            out.append(full)
            continue
        changed += 1
        out.append(fixed)
    return "".join(out), changed


ENTITY_IN_TEXT_RE = re.compile(
    r"&(?:amp|apos|quot|lt|gt|nbsp|#\d+|#x[0-9a-fA-F]+);|_x[0-9a-fA-F]{2}_")


def unescape_page(text: str) -> tuple[str, int]:
    """Decode HTML entities and `_xNN_` escapes left in a page's text.

    Extraction decodes entities once on the way in (`wiki_store._unescape`),
    but pages written before that rule keep them: `&amp;` in a title, `&#x27;`
    in a block, `&apos;` inside an advert URL (which makes the link dead) —
    16 across the three wikis (2026-10-08 audit). `&nbsp;` becomes a plain
    space: the escape existed to hold a line together, and the wiki is not a
    renderer.
    """
    import html

    if not ENTITY_IN_TEXT_RE.search(text):
        return text, 0
    changed = 0

    def _sub(m):
        nonlocal changed
        token = m.group(0)
        decoded = html.unescape(token)
        if token.startswith("_x"):
            try:
                decoded = chr(int(token[2:4], 16))
            except ValueError:                # pragma: no cover - defensive
                return token
        if decoded == token:
            return token
        changed += 1
        return " " if decoded == "\xa0" else decoded

    return ENTITY_IN_TEXT_RE.sub(_sub, text), changed


NO_URL_LINE_RE = re.compile(r"(?m)^(- \*\*URL\*\*: )_not\s*$")


def fix_no_url_placeholders(text: str) -> tuple[str, int]:
    """Rewrite the broken `- **URL**: _not` back to the canonical placeholder.

    A parser that split the spaced placeholder `_not available_` on whitespace
    kept the first token, so pages carry `_not` as if it were a URL — and two
    readers learned to accept it instead of the writer being fixed (2026-10-08).
    """
    from wiki_store import NO_URL_PLACEHOLDER

    return NO_URL_LINE_RE.subn(lambda m: m.group(1) + NO_URL_PLACEHOLDER, text)


#: Decisions that an eligibility ruling can overturn. A DROP in an ineligible
#: location is the CORRECT verdict, so it is never rewritten.
_OVERTURNABLE = ("GO", "MAYBE")

_AXIS_LINES = ("Fit", "Reach", "Flourish", "Combined")


def _advert_text(block: str, place: str) -> str:
    """The advert's own text for this row — the authority for work-mode.

    The cached advert page (`crawler._cache_path(url)`) when we have it, because
    the model's reasoning is an EVALUATION of the advert, not the advert: the
    2026-10-03 Callosum GO's reasoning says "minus 1-2 for on-site London
    friction against the WFH-primary culture preference", and the escape hatch
    in the work-mode rule reads that "WFH culture" as a remote option. The cached
    page says only "On-site", so the rule fires. That is the same authority the
    matcher and the link re-check use — one authority per fact.
    """
    try:
        from wiki_store import cached_advert_path

        url_m = re.search(r"(?m)^- \*\*URL\*\*: (\S+)", block)
        if url_m:
            cached = cached_advert_path(url_m.group(1).split(" ")[0])
            if cached is not None:
                text = cached.read_text(encoding="utf-8", errors="ignore")
                if text.strip():
                    return text
    except Exception:
        pass
    return block


def _eligibility_reason(block: str, profile: str) -> str | None:
    """Why this row is a recommendation the principal cannot take — or None.

    The principal's OWN rules, and the same functions the matcher uses, so the
    repair cannot disagree with the scorer about "where is this job". The
    country-exclusion kill gets one extra guard: a location that also names a
    UK place is left alone, because the US regex's bare "richmond" reads
    "Richmond upon Thames" as Richmond, Virginia (VJ, found 2026-10-09).
    """
    from lead_identity import classify_work_location
    from matcher import (_check_country_auth_kill, _check_work_location_kill,
                         _check_work_mode_kill)

    loc = re.search(r"(?m)^- \*\*Location\*\*: (.+)$", block)
    place = loc.group(1).strip() if loc else ""
    allow_list = _check_work_location_kill(place, "", profile)
    if allow_list:
        return allow_list
    # The work-mode ruling (LJ, 2026-10-06) is a fact about the advert, and the
    # BLOCK carries it: the 2026-10-03 Callosum GO's own "Evidence alignment"
    # line says "London on-site → … commutable from the home town", which the matcher's
    # kill fires on and the link re-check never saw (it looks at the fetched page
    # text). So the ruling reads the same evidence, from the page itself.
    work_mode = _check_work_mode_kill(place, _advert_text(block, place), profile)
    if work_mode:
        return work_mode
    country = _check_country_auth_kill(place, "", profile, "")
    if country:
        is_remote, names_allowed, _others = classify_work_location(place)
        if not is_remote and not names_allowed:
            return country
    return None


def apply_cooldown_rules(text: str, profile: str,
                        dataset_dir: Optional[Path] = None) -> tuple[str, int]:
    """Overturn every GO/MAYBE the **outcomes ledger** says is in cooldown.

    LJ, 2026-10-17, on the PostHog row: *"We applied and got a NO on this one …
    Should be blacklisted — cool off period (12 months or so?) has started. Ad should
    be dropped."*

    The assessment already knows how to refuse a re-apply: `matcher._check_reapply_kill`
    reads the applications tree, Gmail **and** the outcomes ledger. But the sweep
    finished at 17:42 and the rejection was recorded at **19:06** — a new fact arrived
    after the verdict that used it, and nothing re-judged the row. That is the class:
    **the ledger is the fact authority, so a new fact must invalidate the verdict
    derived from it** — the same "newest observation wins" rule the index, the leads
    and the piles already follow.

    Runs before the packs step in `selfheal`, so a cooled-down GO does not get a
    dossier cut for it, and the ruling is the same reversible shape the eligibility
    repair writes.
    """
    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    try:
        from matcher import _check_reapply_kill
    except Exception:
        return text, 0

    prefix, blocks = parts[0], parts[1:]
    out = [prefix]
    changed = 0
    for block in blocks:
        full = "### " + block
        decision = re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE)\b", full)
        if not decision:
            out.append(full)
            continue
        org = _org_from_page(text)
        title = _title_from_block(full)
        if not org or not title:
            out.append(full)
            continue
        try:
            # …and ask about the org the advert's own URL names, not only the one
            # the page title gave: the HuggingFace GOs of 2026-10-18 were filed under
            # two HF *Space* names, so the HuggingFace cooldown never matched them.
            reason = _check_reapply_kill(org, title, dataset_dir=dataset_dir,
                                         use_gmail=False, job_url=_url_from_block(full))
        except Exception:
            reason = None
        if not reason:
            out.append(full)
            continue
        changed += 1
        out.append(_apply_ruling(full, reason, label="cooldown ruling"))
    return "".join(out), changed


def apply_junk_title_rules(text: str) -> tuple[str, int]:
    """Overturn every GO/MAYBE whose **title is not a role**.

    LJ, 2026-10-18, on `Spanish National Cancer Research Centre CNIO — Madrid, Spain`,
    GO 7.3 and sitting in the GO section: *"The decision here is rightly DROP."* He is
    right, and the row is the shape the audit has reported for months as `junk_title`
    (720 such blocks on lj alone): the extractor took the **location** for the
    **position**, so the row names a city, has no advert link, and cannot be applied
    to. A GO that cannot be acted on is not a GO.

    The same reversible ruling the eligibility and cooldown repairs write, for the
    same reason: the verdict is wrong, the evidence stays, and the history is kept in
    a provenance comment. `lead_identity.is_noise_title` is the authority — the same
    predicate the crawl's junk split and the pack guard use, so the three cannot
    disagree about what a role title is.
    """
    from lead_identity import is_noise_title

    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    out = [prefix]
    changed = 0
    for block in blocks:
        full = "### " + block
        if not re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE)\b", full):
            out.append(full)
            continue
        title = _title_from_block(full)
        if not title or not is_noise_title(title):
            out.append(full)
            continue
        changed += 1
        out.append(_apply_ruling(
            full, f"the title {title!r} is not a role title (noise/location), so the "
                  f"row cannot be acted on", label="junk-title ruling"))
    return "".join(out), changed


def apply_implausible_location_rules(text: str) -> tuple[str, int]:
    """Move a non-place Location value into a Note for an actionable row.

    The Location field is a parsed place; a salary line or a prose paragraph is
    not. The evidence stays in a Note and the field is removed, so the audit
    stops counting a defect the principal cannot act on. Non-actionable history
    is left alone — the audit reports it as a gap, not work.
    """
    from lead_identity import is_noise_location

    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    out = [prefix]
    changed = 0
    for block in blocks:
        full = "### " + block
        d = re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE)\b", full)
        if not d:
            out.append(full)
            continue
        m = re.search(r"(?m)^- \*\*Location\*\*: (.+)$", full)
        if not m:
            out.append(full)
            continue
        value = m.group(1).strip()
        if not (is_noise_location(value) or len(value) > 90):
            out.append(full)
            continue
        changed += 1
        full = full[:m.start()] + full[m.end():]
        full = full.rstrip() + "\n- **Note**: unparsed location from the source: " + value + "\n\n"
        out.append(full)
    return "".join(out), changed


def repair_local_links(wiki_root: Path, *, dry_run: bool = False) -> dict:
    """Rewrite local links that only resolve through the fold/rename fallback,
    and unwrap links that resolve to nothing (their text stays).

    Sealed reports are not touched: they are evidence, not a live document. The
    audit counts their links as a gap for the same reason.
    """
    import re as _re

    from wiki_audit import _LinkResolver
    from wiki_store import org_page_paths

    principal_root = Path(wiki_root).resolve().parents[2]
    state_root = Path(wiki_root).resolve().parent
    resolver = _LinkResolver(principal_root)
    link_re = _re.compile(r"(?<!!)\[([^\]]*)\]\(<?([^)>\s]+)>?\)")
    stats = {"pages": 0, "rewritten": 0, "unwrapped": 0}
    for path in org_page_paths(wiki_root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        changed = False

        def repl(m):
            nonlocal changed
            label, target = m.group(1), m.group(2)
            if not target.lstrip("/").startswith("principals/"):
                return m.group(0)
            resolved, fallback = resolver.resolve(target)
            if resolved is None:
                stats["unwrapped"] += 1
                changed = True
                return label
            if not fallback:
                return m.group(0)
            try:
                rel = resolved.resolve().relative_to(principal_root.resolve())
            except ValueError:
                return m.group(0)
            parts = rel.parts
            if len(parts) < 4 or parts[0] != "state" or parts[1] != state_root.name:
                return m.group(0)
            leading = "/" if target.startswith("/") else ""
            new = (leading + "principals/" + state_root.name + "/"
                   + "/".join(parts[2:]))
            if new == target:
                return m.group(0)
            stats["rewritten"] += 1
            changed = True
            return f"[{label}]({new})"

        new_text = link_re.sub(repl, text)
        if changed:
            stats["pages"] += 1
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
    return stats


def _org_from_page(text: str) -> str:
    """The organisation a page is about — its H1, the writer's own authority."""
    found = re.search(r"(?m)^# (.+)$", text)
    return found.group(1).strip() if found else ""


def _url_from_block(block: str) -> str:
    """The advert URL a block carries — the employer's own evidence for its org."""
    found = re.search(r"(?m)^- \*\*URL\*\*: (https?://\S+)", block or "")
    return found.group(1).strip() if found else ""


def _title_from_block(block: str) -> str:
    head = block.splitlines()[0] if block else ""
    head = head.lstrip("# ").strip()
    return head.split(" — ", 1)[1].strip() if " — " in head else ""


def _apply_eligibility_ruling(block: str, reason: str) -> str:
    """The eligibility ruling — `_apply_ruling` with its own label."""
    return _apply_ruling(block, reason, label="eligibility ruling")


def _apply_ruling(block: str, reason: str, *, label: str) -> str:
    """Rewrite one GO/MAYBE block as the DROP the ruler would have produced.

    Zeroes the axis scores exactly as `matcher._apply_work_auth_kill` does, so
    a repaired page is indistinguishable from a re-scored one, and records the
    previous verdict in a provenance comment — the ruling is reversible and the
    history is kept.
    """
    d = re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE) \((\d+(?:\.\d+)?)/10\)\s*$",
                  block)
    if not d:
        return block
    before = f"was {d.group(1)} ({d.group(2)}/10)"
    axes = " ".join(f"{m.group(1)} {m.group(2)}" for m in re.finditer(
        r"(?m)^- \*\*(Fit|Reach|Flourish|Combined)\*\*: ([0-9.]+)/10\s*$", block))
    out = re.sub(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE) \(\d+(?:\.\d+)?/10\)\s*$",
                 "- **Decision**: DROP (0.0/10)", block, count=1)
    out = re.sub(r"(?m)^- \*\*(Fit|Reach|Flourish|Combined)\*\*: [0-9.]+/10\s*$",
                 lambda m: f"- **{m.group(1)}**: 0.0/10", out)
    # …and the action, exactly as `_apply_work_auth_kill` does: a DROP with
    # "APPLY_NOW" left on it is an instruction a reader would follow.
    out = re.sub(r"(?m)^- \*\*Action\*\*: .*$", "- **Action**: DROP", out)
    out = re.sub(r"(?m)^(\s*)- \*\*Reasoning\*\*: ",
                 lambda m: f"{m.group(1)}- **Reasoning**: AUTO-DROP: {reason}. ",
                 out, count=1)
    # The ruling's own date, not the repo's migration stamp: it is the date the
    # verdict was overturned, and it only ever fires once per block (a ruled
    # block is DROP, so the predicate no longer matches it).
    from datetime import datetime

    comment = (f"<!-- {label} {datetime.now():%Y-%m-%d}: {before}"
               + (f", {axes}" if axes else "") + " -->\n")
    return comment + out


def apply_eligibility_rules(text: str, profile: str) -> tuple[str, int]:
    """Overturn every GO/MAYBE the principal's own eligibility rules forbid."""
    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    out = [prefix]
    changed = 0
    for block in blocks:
        full = "### " + block
        d = re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE)\b", full)
        if not d:
            out.append(full)
            continue
        reason = _eligibility_reason(full, profile)
        if not reason:
            out.append(full)
            continue
        changed += 1
        out.append(_apply_eligibility_ruling(full, reason))
    return "".join(out), changed


def _profile_for(wiki_root: Path) -> str:
    """The principal's own work_auth.md — the rules are per-profile."""
    try:
        import config
        principal = Path(wiki_root).parent.name
        return (config.PROFILES_DIR / principal / "work_auth.md").read_text(
            encoding="utf-8", errors="ignore")
    except Exception:
        return ""


#: Re-check verdicts that mean the advert is gone or the role became
#: unworkable — the reason for a ruling, taken from the evidence the re-check
#: collected (the page text), not from a re-derivation.
_RECHECK_RULINGS = {
    "expired": "advert closed (re-check {day}: expiry marker on the page)",
    "unreachable": "advert unreachable (re-check {day}: HTTP {status})",
    "work_mode_conflict": "work-mode conflict (re-check {day}: {detail})",
}


def _newest_recheck(cache_dir: Path) -> dict:
    """The newest machine record of a link re-check, or {} when there is none."""
    out_dir = Path(cache_dir) / "link_checks"
    if not out_dir.is_dir():
        return {}
    newest, payload = None, None
    for path in sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:5]:
        try:
            cand = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(cand, dict) and (newest is None or
                                      str(cand.get("checked_at") or "") >
                                      str(payload.get("checked_at") or "")):
            newest, payload = path, cand
    return payload or {}


def apply_recheck_rulings(text: str, record: dict) -> tuple[str, int]:
    """Rule out the rows the newest re-check already judged — from its verdicts.

    The 06:00 pass opens every actionable advert and records what it found;
    until this step that went to a report block and an org-memory note, which
    is a record, not an action ("checking for links is only useful if we do
    something with the results"). A 404 verdict means the row's link is dead;
    an expiry marker means the advert closed; a work-mode conflict means the
    role became unworkable for this principal. Each is the same kind of fact
    the eligibility rule carries, so it is applied the same way: DROP, the
    axis scores zeroed, and a provenance comment naming the evidence.
    """
    verdicts = {v.get("url"): v for v in (record.get("verdicts") or []) if v.get("url")}
    if not verdicts:
        return text, 0
    parts = BLOCK_SPLIT_RE.split(text)
    if len(parts) < 2:
        return text, 0
    prefix, blocks = parts[0], parts[1:]
    out, changed = [prefix], 0
    for block in blocks:
        full = "### " + block
        if not re.search(r"(?m)^- \*\*Decision\*\*: (GO|MAYBE)\b", full):
            out.append(full)
            continue
        url_m = re.search(r"(?m)^- \*\*URL\*\*: (\S+)", full)
        v = verdicts.get(url_m.group(1)) if url_m else None
        if v is None or v.get("verdict") not in _RECHECK_RULINGS:
            out.append(full)
            continue
        reason = _RECHECK_RULINGS[v["verdict"]].format(
            day=str(v.get("checked_at") or record.get("checked_at") or "")[:10],
            status=v.get("status") or "?",
            detail=(v.get("detail") or "").strip()[:80])
        out.append(_apply_eligibility_ruling(full, reason))
        changed += 1
    return "".join(out), changed


def repair_wiki(wiki_root: Path, *, dedupe_blocks: bool = False,
                eligibility: bool = False,
                cooldowns: bool = False,
                junk_titles: bool = False,
                implausible_locations: bool = False,
                local_links: bool = False,
                recheck_rulings: bool = False,
                normalize_notes: bool = False,
                normalize_report_blocks: bool = False,
                unescape_entities: bool = False,
                fix_no_url: bool = False,
                dry_run: bool = False) -> dict:
    from wiki_store import org_page_paths

    orgs = wiki_root / "orgs"          # kept for the callers that name a directory
    pages = org_page_paths(wiki_root)  # …and this is what we actually repair
    if not orgs.is_dir():
        return {"error": "no orgs dir"}
    stats = {"pages": 0, "pages_changed": 0, "blocks_collapsed": 0,
             "notes_normalised": 0, "report_blocks_normalised": 0,
             "entities_decoded": 0, "no_url_fixed": 0, "eligibility_rulings": 0,
             "cooldown_rulings": 0, "junk_title_rulings": 0,
             "implausible_location_rulings": 0, "local_links": {},
             "recheck_rulings": 0}
    recheck = _newest_recheck(Path(wiki_root).parent / "cache") if recheck_rulings else {}
    decisions = unique_report_decisions(wiki_root / "reports") if normalize_report_blocks else {}
    for path in pages:
        text = path.read_text(encoding="utf-8", errors="ignore")
        stats["pages"] += 1
        new = text
        if fix_no_url:
            new, fixed = fix_no_url_placeholders(new)
            stats["no_url_fixed"] += fixed
        if recheck:
            new, ruled = apply_recheck_rulings(new, recheck)
            stats["recheck_rulings"] += ruled
        if eligibility:
            new, ruled = apply_eligibility_rules(
                new, _profile_for(wiki_root))
            stats["eligibility_rulings"] += ruled
        if junk_titles:
            # A GO whose title is a place cannot be applied to; the audit has counted
            # them as `junk_title` for months and left them standing (2026-10-18).
            new, ruled = apply_junk_title_rules(new)
            stats["junk_title_rulings"] += ruled
        if implausible_locations:
            # The field is a parsed place; a salary line or prose is not. Keep
            # the evidence in a Note and remove the field (2026-11-04).
            new, ruled = apply_implausible_location_rules(new)
            stats["implausible_location_rulings"] += ruled
        if cooldowns:
            # **After the eligibility rules and before the packs step**: a GO whose
            # org is in a cooldown (a recorded application, rejection, interview or
            # offer) is not a fresh opportunity, and the ledger is the authority for
            # it. The PostHog row of 2026-10-17 is the case: the sweep finished at
            # 17:42, the rejection was recorded at 19:06, and the row stayed GO 8.9
            # until something re-judged it.
            new, ruled = apply_cooldown_rules(
                new, _profile_for(wiki_root),
                # The state layout: `state/<p>/wiki` → `state/<p>/dataset`, where the
                # outcomes ledger is the fact authority (derived, not a new parameter).
                wiki_root.parent / "dataset")
            stats["cooldown_rulings"] += ruled
        if unescape_entities:
            new, fixed = unescape_page(new)
            stats["entities_decoded"] += fixed
        if normalize_report_blocks:
            new, fixed = normalize_report_blocks_page(new, _org_name(new, path),
                                                       decisions)
            stats["report_blocks_normalised"] += fixed
        if normalize_notes:
            new, fixed = normalize_notes_page(new)
            stats["notes_normalised"] += fixed
        if dedupe_blocks:
            new, collapsed = dedupe_page(new, _org_name(new, path))
            stats["blocks_collapsed"] += collapsed
        if new != text:
            stats["pages_changed"] += 1
            if not dry_run:
                path.write_text(new, encoding="utf-8")
    if local_links:
        stats["local_links"] = repair_local_links(wiki_root, dry_run=dry_run)
    return stats


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--dedupe-blocks", action="store_true")
    parser.add_argument("--normalize-notes", action="store_true")
    parser.add_argument("--normalize-report-blocks", action="store_true")
    parser.add_argument("--unescape-entities", action="store_true")
    parser.add_argument("--fix-no-url", action="store_true")
    parser.add_argument("--junk-titles", action="store_true",
                        help="overturn GO/MAYBE whose title is not a role (a location, "
                             "a contract type, prose) — a GO that cannot be acted on")
    parser.add_argument("--cooldowns", action="store_true",
                        help="overturn GO/MAYBE whose org is in a cooldown per the "
                             "outcomes ledger (applied/rejection/interview/offer)")
    parser.add_argument("--eligibility", action="store_true",
                        help="overturn GO/MAYBE the principal's own work-location "
                             "and country-exclusion rules forbid")
    parser.add_argument("--recheck-rulings", action="store_true",
                        help="overturn GO/MAYBE the newest link re-check ruled out "
                             "(expired, 404/410, work-mode conflict)")
    parser.add_argument("--implausible-locations", action="store_true",
                        help="move a non-place Location value into a Note for GO/MAYBE")
    parser.add_argument("--local-links", action="store_true",
                        help="rewrite masked local links and unwrap dead ones "
                             "(sealed reports untouched)")
    parser.add_argument("--all", action="store_true",
                        help="run every repair")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.all:
        args.dedupe_blocks = args.normalize_notes = True
        args.normalize_report_blocks = args.unescape_entities = True
        args.fix_no_url = True
        args.eligibility = True
        args.cooldowns = True
        args.junk_titles = True
        args.recheck_rulings = True
        args.implausible_locations = True
        args.local_links = True
    if not (args.dedupe_blocks or args.normalize_notes
            or args.normalize_report_blocks or args.unescape_entities
            or args.fix_no_url or args.eligibility or args.cooldowns
            or args.junk_titles or args.recheck_rulings
            or args.implausible_locations or args.local_links):
        parser.error("nothing to do: pass --dedupe-blocks/--normalize-notes/--all")
    root = config.STATE_DIR / args.profile / "wiki"
    if not root.is_dir():
        print(f"no wiki at {root}", file=sys.stderr)
        return 1
    stats = repair_wiki(root, dedupe_blocks=args.dedupe_blocks,
                        normalize_notes=args.normalize_notes,
                        normalize_report_blocks=args.normalize_report_blocks,
                        unescape_entities=args.unescape_entities,
                        fix_no_url=args.fix_no_url, eligibility=args.eligibility,
                        cooldowns=args.cooldowns, junk_titles=args.junk_titles,
                        recheck_rulings=args.recheck_rulings,
                        implausible_locations=args.implausible_locations,
                        local_links=args.local_links,
                        dry_run=args.dry_run)
    print(args.profile, stats)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

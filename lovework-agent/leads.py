"""Every lead we judged gets its evidence saved — MAYBE, FLAG and DROP too.

LJ, 2026-10-09: *"We have to have some information about the ad, the company etc
— that we use decide if it's a MAYBE or GO or DROP. Otherwise — how can we
possibly decide? … Now for MAYBE — we collect company and advert info => we save
that as a background check. We don't need to do tailored CV, filling the form
info and so on. But we do know elements that appear in the GO packs: i) the url
and the text of the advert ii) the url and the text of the application form. So
this should form part of a MAYBE pack. We should save it down. We fetch it, we
save it. We analyze it - we save the result of our analysis. All this should be
in the Wiki, should not be forgotten. Same goes for the DROP-s."*

So a **lead page** is the pack's evidence half, for every decision that is not a
GO:

  state/<principal>/leads/YYYYMMDD-Company_name-Advert_name/lead.md

It carries the advert link and the text we fetched, the application form when the
advert names one, our analysis (the same block the wiki holds), and a link to the
org page and the org's memory. Nothing tailored is prepared — that is what a GO
pack adds.

**Why `leads/` and not `applications/`:** LJ: *"maybe not a good idea to clog the
applications/ — maybe have something else, maybe adverts/, or leads/ as their top
level directory?"* A lead is a judgement, not an application; `applications/`
holds the cases where the principal acted, and 6,750 judgements would drown them.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

import config
from job_registry import _job_hash
from wiki_store import WikiEntry, cached_advert_path

logger = logging.getLogger("leads")

LEAD_PAGE_NAME = "lead.md"
#: The decisions that get a lead page. GO is excluded: its evidence is the pack.
DEFAULT_DECISIONS = ("MAYBE", "FLAG", "DROP")
_APPLY_HOST_RE = re.compile(
    r"https?://[^\s)\]]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|"
    r"smartrecruiters\.com|myworkdayjobs\.com|recruitee\.com|bamboohr\.com|"
    r"teamtailor\.com|/apply|/application)",
    re.I)


def leads_root(principal: str = "lj") -> Path:
    return config.STATE_DIR / principal.lower() / "leads"


def _slug(org: str, title: str, when: str, identity: str = "") -> str:
    """`YYYYMMDD-Company_name-Advert_name` — LJ's naming, without a suffix.

    Deliberately NOT `pack_slug`: the `-LoveWork` suffix marks an *application*
    pack, and a lead is a judgement, not an application. Two different adverts
    can sanitise to the same slug (a truncated title, a shared org), so the
    advert's identity disambiguates — without it one page would overwrite
    another (measured: 1,613 pages landing in 1,545 directories).
    """
    from application_packs import _slug_part

    try:
        day = date.fromisoformat((when or "")[:10])
    except ValueError:
        day = date.today()
    slug = f"{day.strftime('%Y%m%d')}-{_slug_part(org, 40)}-{_slug_part(title, 60)}"
    return f"{slug}-{identity[:6]}" if identity else slug


def lead_dir(principal: str, org: str, title: str, when: str,
             identity: str = "") -> Path:
    return leads_root(principal) / _slug(org, title, when, identity)


def _form_link(entry: WikiEntry, advert_text: str) -> str:
    m = _APPLY_HOST_RE.search(f"{advert_text}\n{entry.advert_excerpt or ''}")
    return m.group(0).rstrip(".,);") if m else ""


def _cached_serve_link(principal: str, url: str) -> str:
    path = cached_advert_path(url)
    if path is None:
        return ""
    return f"/principals/{principal}/cache/{path.name}"


def lead_page(entry: WikiEntry, *, principal: str = "lj", advert_text: str = "",
              observations: int = 1, latest: str = "") -> str:
    """The background check: what we gathered, and what we concluded."""
    cached = _cached_serve_link(principal, entry.original_url or entry.url)
    form = _form_link(entry, advert_text)
    lines = [
        f"# {entry.org_name} — {entry.title}",
        "",
        f"- **Decision**: {entry.decision} ({entry.score:.1f}/10) · "
        f"{entry.recommended_action or 'no action recorded'}"
        + (f" · {entry.location}" if entry.location else ""),
        f"- **Observed**: {observations} time(s) on file"
        + (f", first {entry.first_seen}" if entry.first_seen else "")
        + (f", latest judgement {latest}" if latest and latest != entry.first_seen else ""),
        "",
        "## 1. The advert",
        "",
        (f"- **Link**: {entry.url}" if entry.url else "- **Link**: _not retained_"),
    ]
    if entry.original_url and entry.original_url != entry.url:
        lines.append(f"- **Preferred link**: {entry.original_url}")
    if entry.discovery_url:
        lines.append(f"- **Found via**: [{entry.source}]({entry.discovery_url})"
                     + (f" ({entry.discovery_date})" if entry.discovery_date else ""))
    lines.append(f"- **Fetched**: {entry.primary_fetched_at or 'not retained'}"
                 f" via {entry.primary_fetch_method or 'unknown'}")
    if cached:
        lines.append(f"- **Cached copy** (the text we judged): {cached}")
    lines.extend(["", "### Advert text we hold", ""])
    text = (advert_text or entry.advert_excerpt or "").strip()
    if text:
        # Quoted, not embedded raw: the advert's own markdown carries H1s and H2s
        # (measured: a second `<h1>` on the page), and a blockquote says "this is
        # the source, not our prose".
        body = text[:4000].rstrip()
        lines.append("\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
                     + ("\n>\n> _…truncated; the full extract is the cached copy above._"
                        if len(text) > 4000 else ""))
    else:
        lines.append("_No advert text was retained for this lead. The decision was "
                     "made from the source line and the org page._")
    lines.extend(["", "## 2. Applying", ""])
    lines.append(f"- **Application form**: {form}" if form else
                 "- **Application form**: not named in the advert")
    lines.extend(["", f"## 3. Our analysis — why {entry.decision}", ""])
    if entry.reasoning:
        lines.append(entry.reasoning)
    for label, items in (("Evidence alignment", entry.alignment_matrix),
                         ("Gaps", entry.gaps)):
        if items:
            lines.extend(["", f"**{label}**", ""])
            lines.extend(f"- {item}" for item in items)
    for label, value in (("Fit / reach / flourish",
                          " / ".join(str(v) for v in (entry.fit_score,
                                                      entry.reach_score,
                                                      entry.flourish_score)
                                     if v is not None)),
                         ("Application angle", entry.application_angle),
                         ("Screening story", entry.screening_story),
                         ("Likely day-to-day", entry.likely_day_to_day),
                         ("Prestige-trap risk", entry.prestige_trap_risk)):
        if value:
            lines.extend(["", f"**{label}**: {value}"])
    lines.extend(["", "## 4. The company", ""])
    try:
        from wiki_store import WikiStore

        store = WikiStore(root=config.STATE_DIR / principal / "wiki")
        stem = store._org_stem(entry.org_name)
        lines.append(f"- **Wiki page** (the org's whole history): "
                     f"[/{principal}/wiki/orgs/{stem}.md]"
                     f"(/principals/{principal}/wiki/orgs/{stem}.md)")
    except Exception:
        pass
    lines.extend(["", "---", "",
                  "_A lead page is the evidence behind a judgement. No tailored "
                  "material is prepared for a non-GO: that is what a GO pack adds._",
                  ""])
    return "\n".join(lines)


def write_lead(entry: WikiEntry, *, principal: str = "lj", advert_text: str = "",
               observations: int = 1, when: str = "", latest: str = "") -> Path:
    # The identity disambiguates two adverts that sanitise to one slug; the page
    # itself is keyed by the advert, so it can never be shared.
    identity = _job_hash(entry.org_name, entry.title, entry.url or "")
    directory = lead_dir(principal, entry.org_name, entry.title,
                         when or entry.first_seen or entry.date or "", identity)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LEAD_PAGE_NAME
    path.write_text(lead_page(entry, principal=principal, advert_text=advert_text,
                              observations=observations, latest=latest),
                    encoding="utf-8")
    return path


def lead_index(principal: str = "lj") -> dict:
    """advert identity → lead directory, read once (for linking from the wiki).

    The directory name is the lead's own date+org+title, so the mapping has to be
    recorded rather than derived: an advert observed on twenty days is one lead
    page, filed under its first observation.
    """
    root = leads_root(principal)
    index: dict = {}
    if not root.is_dir():
        return index
    for directory in root.iterdir():
        page = directory / LEAD_PAGE_NAME
        if not page.is_file():
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"(?m)^- \*\*Link\*\*: (\S+)", text)
        link = m.group(1) if m and m.group(1).startswith("http") else ""
        pref = re.search(r"(?m)^- \*\*Preferred link\*\*: (\S+)", text)
        if pref:
            link = pref.group(1)
        title = text.split("\n", 1)[0].removeprefix("# ").split(" — ", 1)
        if len(title) == 2:
            index.setdefault(_job_hash(title[0].strip(), title[1].strip(), link),
                             directory)
            index.setdefault(_title_key(title[0].strip(), title[1].strip()), directory)
    return index


def _title_key(org: str, title: str) -> str:
    from application_packs import pack_title_key

    return pack_title_key(org, title)


def prepare_missing_leads(principal: str = "lj", *,
                          decisions=DEFAULT_DECISIONS,
                          dry_run: bool = False,
                          refresh: bool = False) -> dict:
    """Write the lead page for every judged advert that has none.

    The entries come from the org pages — they carry the decision, the scores and
    the reasoning, which IS the analysis the page must not lose. The advert text
    comes from the fetch cache when we have it, and says so when we do not: the
    page is a record of what we knew, never a claim to more.
    """
    from wiki_store import WikiStore

    # A retired advert is closed and in its pile: rebuilding its lead page here
    # would resurrect it (the builder wanted to recreate 534 retired vj leads —
    # measured 2026-10-14). Retirement is a one-way door, and this is the lock.
    import retire

    retired = retire.retired_keys(principal)
    wiki_root = config.STATE_DIR / principal / "wiki"
    if not wiki_root.is_dir():
        return {"principal": principal, "error": "no wiki"}
    store = WikiStore(root=wiki_root)
    wanted = {d.upper() for d in decisions}
    existing = lead_index(principal)
    stats = {"principal": principal, "judged": 0, "created": 0, "existing": 0,
             "by_decision": {}}
    seen: set = set()
    # DROP included: the analysis behind a drop is exactly what must not be lost.
    # The NEWEST observation of an advert is its current judgement (an advert
    # re-assessed from DROP to MAYBE must not keep a DROP lead page), so the
    # entries are ordered by date and the first per advert wins.
    entries = sorted(
        store._entries_from_org_pages(decisions=("GO", "MAYBE", "FLAG", "DROP")),
        key=lambda e: str(e.date or ""), reverse=True)
    # How many times we have seen each advert, and the newest date: the page says
    # both, so a reader can tell a fresh judgement from a two-week-old one.
    seen_count: dict = {}
    latest: dict = {}
    first: dict = {}
    for entry in entries:
        identity = _job_hash(entry.org_name, entry.title, entry.url or "")
        seen_count[identity] = seen_count.get(identity, 0) + 1
        day = str(entry.date or "")
        if day > latest.get(identity, ""):
            latest[identity] = day
        if day and (identity not in first or day < first[identity]):
            first[identity] = day
    for entry in entries:
        decision = (entry.decision or "").upper()
        if decision not in wanted:
            continue
        if retire.is_retired(entry.org_name, entry.title, retired):
            stats["retired_skipped"] = stats.get("retired_skipped", 0) + 1
            continue
        identity = _job_hash(entry.org_name, entry.title, entry.url or "")
        stats["by_decision"][decision] = stats["by_decision"].get(decision, 0) + 1
        if identity in seen:
            continue
        seen.add(identity)
        stats["judged"] += 1
        if identity in existing and not refresh:
            stats["existing"] += 1
            continue
        if dry_run:
            stats["created"] += 1
            continue
        path = cached_advert_path(entry.original_url or entry.url)
        text = ""
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
        # The page carries the NEWEST judgement; the directory keeps the date we
        # first saw the lead, so a lead's home does not move as it ages.
        write_lead(entry, principal=principal, advert_text=text,
                   when=first.get(identity) or entry.first_seen or entry.date,
                   observations=seen_count.get(identity, 1),
                   latest=latest.get(identity, ""))
        stats["created"] += 1
    return stats


def _main(argv=None) -> int:                    # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--prepare-missing", action="store_true")
    parser.add_argument("--decisions", default=",".join(DEFAULT_DECISIONS),
                        help="comma-separated decisions to cover")
    parser.add_argument("--refresh", action="store_true",
                        help="rewrite every lead page, not only the missing ones")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.prepare_missing:
        print(args.profile, prepare_missing_leads(
            args.profile,
            decisions=[d.strip() for d in args.decisions.split(",") if d.strip()],
            dry_run=args.dry_run, refresh=args.refresh))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

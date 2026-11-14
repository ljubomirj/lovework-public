"""
The Jack & Jill letter — a periodic report to them on their own offerings.

Jack & Jill are an agent on the employer side: "Jack is fiduciary to the
candidate; Jill works for companies." LoveWork runs for a principal, so the
letter is a note from one agent to another across the market: here is what we
saw of your featured roles in the last week or two, and what our principal's
profile made of them.

Two rules shape it:

1. **Short.** It names at most `LOVEWORK_JNJ_LETTER_MAX_POSITIONS` roles, chosen
   by our own verdict first (a role our matcher flagged matters more to the
   reader than one it dropped), then by recency. Everything else in the window
   is reported as a count, not a list.
The letter answers **the drop** — the hand-picked leads Jack emails this
principal (4-5 a week normally), not the public featured-role feed. The drop is
the relationship: a principal who never answers reads as uninterested, decays
off the recruiter's radar, and the leads thin out and stop (docs/24 records this
happening, in Jack's own words, while the principal was on holiday).

2. **Never repeat a position.** Identity is the lead's own id — the feed's
   `<referencenumber>`, or the `app.jackandjill.ai/jobs/<uuid>/post` uuid for a
   dropped lead.
   Every position the window covers — named in the table or reported as a
   count — is written to `letters/jackandjill-lettered.jsonl`, so no position
   is reported twice in any form. A later letter says how many it held back
   for that reason. (A position too low-ranked to name cannot rise later: the
   ranking is our verdict then recency, and recency only decays.)

Attribution is a permission condition, not a courtesy: `ai.txt` on
jackandjill.ai states `Attribution-Required: yes`, and `llms.txt` asks that the
full name "Jack & Jill" and https://www.jackandjill.ai be cited. Both appear in
every letter and on each offering line's source link.

The letter is *written*, never sent: it lands in `state/<principal>/letters/`
for the principal to review and forward. Sending to a third party is the
principal's decision, not the pipeline's.
"""

import json
import jsonl_io
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "jackandjill-lettered.jsonl"
OFFERINGS_FILENAME = "offerings.jsonl"
SOURCE_DIRNAME = "jackandjill"

# "the past 1 or 2 weeks"
WINDOW_DAYS = int(os.getenv("LOVEWORK_JNJ_LETTER_WINDOW_DAYS", "14"))
# the letter is weekly even though LJ's full crawl runs weekly (Sat 10:00)
MIN_INTERVAL_DAYS = int(os.getenv("LOVEWORK_JNJ_LETTER_MIN_INTERVAL_DAYS", "7"))
# Answered leads per letter. The drop is 4-5 a week and the format is two or
# three lines each, so this is what keeps the letter to one page.
MAX_POSITIONS = int(os.getenv("LOVEWORK_JNJ_LETTER_MAX_POSITIONS", "6"))

CONTACT_EMAIL = "user@example.invalid"
SITE = "https://www.jackandjill.ai"

# Verdicts first (our principal's interest), then unassessed, then dropped.
_VERDICT_ORDER = {"GO": 0, "MAYBE": 1, "FLAG": 2, "": 3, "DROP": 4}


@dataclass
class LetterResult:
    written: bool
    reason: str
    path: Optional[Path] = None
    positions: int = 0          # named in the letter
    covered: int = 0            # all positions the letter accounts for
    skipped_already_lettered: int = 0
    window_days: int = WINDOW_DAYS


def _read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    if not path.is_file():
        return rows
    for line in jsonl_io.lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_offerings(cache_dir: Path) -> Dict[str, dict]:
    """Fold the source's append-only offer log (last row per reference wins)."""
    offerings: Dict[str, dict] = {}
    for row in _read_jsonl(Path(cache_dir) / SOURCE_DIRNAME / OFFERINGS_FILENAME):
        reference = str(row.get("referencenumber") or "")
        if not reference:
            continue
        merged = dict(offerings.get(reference) or {})
        merged.update(row)
        offerings[reference] = merged
    return offerings


def load_lettered(letters_dir: Path) -> Dict[str, dict]:
    """Every position already covered by an earlier letter."""
    lettered: Dict[str, dict] = {}
    for row in _read_jsonl(Path(letters_dir) / LEDGER_FILENAME):
        reference = str(row.get("referencenumber") or "")
        if reference:
            lettered[reference] = row
    return lettered


def _as_day(value: object) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def offerings_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / SOURCE_DIRNAME / OFFERINGS_FILENAME


def last_letter_day(lettered: Dict[str, dict]) -> Optional[date]:
    days = [_as_day(row.get("lettered_at")) for row in lettered.values()]
    return max((d for d in days if d is not None), default=None)


def due(lettered: Dict[str, dict], today: date, *, min_interval_days: int = MIN_INTERVAL_DAYS) -> bool:
    """Weekly cadence: a full crawl may run more often than the letter should."""
    last = last_letter_day(lettered)
    if last is None:
        return True
    return (today - last).days >= min_interval_days


def select_window(
    offerings: Dict[str, dict],
    lettered: Dict[str, dict],
    today: date,
    *,
    window_days: int = WINDOW_DAYS,
    channel: Optional[str] = None,
) -> Tuple[List[dict], int]:
    """Positions first seen within the window and never covered before.

    Returns (fresh, skipped) where `skipped` counts window positions that an
    earlier letter already covered — the number this letter deliberately does
    not repeat.
    """
    cutoff = today - timedelta(days=window_days)
    fresh: List[dict] = []
    skipped = 0
    for reference, row in offerings.items():
        if channel is not None and str(row.get("channel") or "feed") != channel:
            continue
        # "Their offerings in the past 1-2 weeks" means the feed's own
        # publication date; first_seen is only a fallback for a row whose
        # publication date is unreadable.
        seen = _as_day(row.get("date")) or _as_day(row.get("first_seen"))
        if seen is None or seen < cutoff:
            continue
        if reference in lettered:
            skipped += 1
            continue
        fresh.append(row)
    fresh.sort(
        key=lambda row: (
            _VERDICT_ORDER.get(str(row.get("decision") or "").upper(), 3),
            -(_as_day(row.get("date")) or date.min).toordinal(),
            str(row.get("title") or "").lower(),
        )
    )
    return fresh, skipped


def _location_cell(row: dict, *, max_alternates: int = 4) -> str:
    """Primary location plus the extra countries a role is open in.

    Multi-country roles are common in this feed (56/342 on 2026-09-12) and the
    primary location alone can read as a market the principal cannot work in.
    """
    primary = ", ".join(
        p for p in (row.get("city"), row.get("location"), row.get("country")) if p
    )
    alternates = [str(a) for a in (row.get("alternate_locations") or []) if str(a).strip()]
    if alternates:
        shown = ", ".join(alternates[:max_alternates])
        if len(alternates) > max_alternates:
            shown += f" +{len(alternates) - max_alternates}"
        suffix = f" · also {shown}"
    else:
        suffix = ""
    return (primary + suffix) or "—"


def _first_sentence(text: str, limit: int = 240) -> str:
    """Our read on a lead, trimmed to one sentence the reader can act on.

    When no sentence ends inside the limit the cut lands mid-thought, so it
    lands on a word boundary and says so — a lead whose rationale read
    "high-agency, s." reads as broken prose rather than as a trimmed sentence.
    """
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    cut = re.search(r"(?<=[.!?])\s", clean[:limit])
    if cut:
        return clean[: cut.end()].strip().rstrip(".") + "."
    trimmed = clean[:limit].rstrip()
    if len(clean) > len(trimmed):
        space = trimmed.rfind(" ")
        trimmed = (trimmed[:space] if space > 0 else trimmed).rstrip(" ,;:-") + "…"
        return trimmed
    return trimmed.rstrip(".") + "."


# A lead we cannot act on at all: the pre-LLM work-auth/reapply hard-kills
# report themselves in the reasoning. This is the most useful thing the letter
# carries — "stop sending poolside, they rejected us in April" — so it is
# stated as its own verdict rather than dressed up as an assessment.
_CLOSED_MARKERS = ("auto-drop", "cooldown", "reapply", "work auth", "work-auth")


def _closed_reason(row: dict) -> str:
    """Why this lead is closed to us, in words a recruiter can act on.

    The assessment's own string is internal machinery ("AUTO-DROP: Org-level
    cooldown: …"), and the letter is read by a third party. So it is translated:
    the facts survive, the jargon does not. Saying *why* we cannot act is the
    most useful thing this letter carries — it tells Jack what to send instead
    of this — so a translated reason is kept, never blanked.
    """
    reasoning = re.sub(r"\s+", " ", str(row.get("reasoning") or "")).strip()
    if not reasoning or not any(m in reasoning.lower() for m in _CLOSED_MARKERS):
        return ""
    body = re.sub(r"^\s*AUTO-DROP\s*:\s*", "", reasoning, flags=re.I)
    body = re.sub(r"^\s*Org-level cooldown\s*:\s*", "", body, flags=re.I)

    company = str(row.get("company") or "this company")
    rejected = re.search(
        r"(?P<org>[\w.&'-]+)\s+rejected\s+(?:LJ|us)\s+on\s+(?P<date>[\d-]{8,10})"
        r"(?:\s*\(role\s*'(?P<role>[^']+)'\))?",
        body, re.I,
    )
    if rejected:
        when = rejected.group("date")
        return (
            f"We applied to {rejected.group('org')} and were turned down on {when}. "
            "Our standing rule is not to approach the same company again for eighteen "
            "months, so there is nothing for you to do here — please spend the effort elsewhere."
        )
    if "work-auth" in body.lower() or "us-only" in body.lower() or "us only" in body.lower():
        return (
            "This one needs US work authorisation, which this principal does not have. "
            "Roles like this are the ones to filter out before they reach us."
        )
    if "reapply" in body.lower():
        return (
            f"We have an application already open with {company} for a closely related "
            "role, so our rule is to wait rather than duplicate it."
        )
    return re.sub(r"\s+", " ", body)[:300].rstrip(".") + "."


_CAVEAT_MARKERS = (
    "caveat", "but ", "however", "on-site", "onsite", "hybrid", "travel",
    "meetup", "requires", "work-auth", "visa", "us-only", "us only",
    "clearance", "hard-kill", "no fit", "mismatch", "drop",
)


def _caveat(row: dict) -> str:
    """Their/our reservation about the lead, if the assessment stated one.

    Never invented: if the reasoning carries no reservation, the letter says
    nothing rather than manufacturing a criticism.
    """
    reasoning = re.sub(r"\s+", " ", str(row.get("reasoning") or "")).strip()
    if not reasoning:
        return ""
    for sentence in re.split(r"(?<=[.!?])\s", reasoning):
        if any(marker in sentence.lower() for marker in _CAVEAT_MARKERS):
            return sentence.strip() if sentence.endswith((".", "!", "?")) else sentence.strip() + "."
    decision = str(row.get("decision") or "").upper()
    if decision in ("DROP", "FLAG"):
        return "Not a fit for this principal's brief."
    return ""


_ROLE_STOPWORDS = {
    "senior", "staff", "principal", "lead", "engineer", "engineering", "member",
    "of", "and", "the", "ai", "ml", "at", "for", "role", "jobs", "job", "full",
    "time", "remote", "london", "uk", "us",
}


def _role_tokens(text: str) -> set:
    words = re.findall(r"[a-z]{4,}", str(text or "").lower())
    return {w for w in words if w not in _ROLE_STOPWORDS}


def _shares_role_token(lead_title: str, case_name: str) -> bool:
    """True when the case's role slug looks like the same role as the lead."""
    return bool(_role_tokens(lead_title) & _role_tokens(case_name))


def _what_we_did(row: dict, applications_dir: Optional[Path]) -> str:
    """What LoveWork actually did with the lead — the part they cannot see.

    Derived from ground truth, in order of strength: an opened case (we put
    work in), then the matcher's verdict, then nothing yet.
    """
    company = str(row.get("company") or "")
    lead_day = _as_day(row.get("date")) or _as_day(row.get("first_seen"))
    if applications_dir is not None and company:
        wanted = re.sub(r"[^a-z0-9]", "", company.lower())
        if wanted:
            try:
                for path in sorted(Path(applications_dir).iterdir()):
                    if not path.is_dir():
                        continue
                    if wanted not in re.sub(r"[^a-z0-9]", "", path.name.lower()):
                        continue
                    # A case opened *before* the lead is prior history, not our
                    # response to it (poolside's April rejection is why the
                    # current lead is closed, not something we did about it).
                    case_day = _as_day(path.name[:8])
                    if lead_day and case_day and case_day < lead_day:
                        continue
                    # Same company is not the same role: poolside's April
                    # "Evaluations" application is not our response to a
                    # "Compute" lead, so require a shared significant token.
                    if not _shares_role_token(row.get("title"), path.name):
                        continue
                    return f"opened a case — `{path.name}`"
            except OSError:
                pass
    if _closed_reason(row):
        return "nothing — we cannot act on it"
    decision = str(row.get("decision") or "").upper()
    if decision == "GO":
        return "shortlisted as a GO for the principal"
    if decision == "MAYBE":
        return "kept as a MAYBE for the principal"
    if decision in ("DROP", "FLAG"):
        return "passed on it"
    return "not assessed yet"


def compose_letter(
    *,
    principal: str,
    role: str,
    fresh: List[dict],
    window_days: int,
    today: date,
    run_id: str,
    skipped_already_lettered: int,
    letters_to_date: int,
    max_positions: int = MAX_POSITIONS,
    applications_dir: Optional[Path] = None,
    feed_new: int = 0,
    last_letter: Optional[date] = None,
) -> str:
    """Answer the drop: a sentence or two per lead, and never the same lead twice."""
    named = fresh[:max_positions]
    remainder = len(fresh) - len(named)
    start = today - timedelta(days=window_days)
    # A subject line must not overclaim: with no drops in the window, the letter
    # is about the feed, and says so.
    if fresh:
        title = f"# Jack & Jill — our read on the leads you sent, {start.isoformat()} to {today.isoformat()}"
    else:
        since = (last_letter or start).isoformat()
        title = f"# Jack & Jill — no new drops since {since}, and what your feed showed"
    lines = [
        title,
        "",
        f"**To:** Jack & Jill · {CONTACT_EMAIL} · {SITE}  ",
        # The principal's id, upper-cased: the convention every other LoveWork
        # surface uses (notify.py), and how Jack himself addresses him
        # ("Hi LJ" — 21 of his drops say exactly that).
        f"**From:** LoveWork, the agent working for {principal.upper()}"
        + (f" (role: {role})" if role and role != "general" else "")
        + "  ",
        f"**Window:** {window_days} days · **Run:** {run_id or 'n/a'}",
        "",
    ]
    # The opening must promise only what follows: in a week with no drops there
    # are no readings to deliver, and saying otherwise reads as a form letter.
    if last_letter is not None and fresh:
        lines.append(
            f"Jack — apologies for the quiet stretch since {last_letter.isoformat()}. "
            "We read every drop; below is what we made of the ones since then."
        )
    elif last_letter is not None:
        lines.append(
            f"Jack — apologies for the quiet stretch since {last_letter.isoformat()}. "
            "No new drops reached us in that time; what follows is what we saw on "
            "your public feed."
        )
    elif fresh:
        lines.append(
            "Jack — thanks for these. Below is what we made of each one, including "
            "what we did with it. Keep them coming."
        )
    else:
        lines.append(
            "Jack — nothing new has reached us yet. Here is what your public feed shows."
        )
    # A feed-only week still has something to say, so the heading counts leads
    # only when there are leads — "0 leads" beside a moving feed would be the
    # letter contradicting itself.
    heading = "## What we made of them"
    if fresh:
        heading += f" ({len(fresh)} lead{'s' if len(fresh) != 1 else ''})"
    lines += ["", heading, ""]

    if not named:
        lines += [
            "_No new drops since our last letter — your featured feed moved instead._"
            if feed_new
            else "_Nothing new since our last letter._",
            "",
        ]
    for row in named:
        title = row.get("title") or "Untitled role"
        company = row.get("company") or "—"
        url = row.get("url") or SITE
        location = _location_cell(row)
        bits = [b for b in (location, row.get("salary")) if b and b != "—"]
        lines += [f"### [{title}]({url}) — {company}", ""]
        if bits:
            lines += [f"_{' · '.join(bits)}_", ""]
        closed = _closed_reason(row)
        if closed:
            lines.append(f"- **Closed to us:** {closed}")
        else:
            interesting = _first_sentence(row.get("reasoning") or "")
            if interesting:
                lines.append(f"- **Interesting:** {interesting}")
            caveat = _caveat(row)
            # Never print the same sentence twice: when the assessment's one
            # sentence is also its reservation, it is a caveat, not a reason.
            if caveat and caveat != interesting:
                lines.append(f"- **Off:** {caveat}")
        lines.append(f"- **We did:** {_what_we_did(row, applications_dir)}")
        lines.append("")

    if remainder:
        lines += [
            f"Plus **{remainder} more** lead(s) in this window, held back to keep this to a page.",
            "",
        ]

    # The restraint statement is the letter's signature, but a "0 lead(s) were
    # already answered" line in a week with no leads is noise about nothing.
    lines += ["## Not repeated", ""]
    if skipped_already_lettered:
        lines.append(
            f"- {skipped_already_lettered} lead(s) in this window were already answered in an "
            "earlier letter and are deliberately absent here."
        )
    lines.append(f"- {letters_to_date} lead(s) covered across all letters so far.")
    if feed_new:
        when = (
            "since our last letter" if last_letter is not None
            else f"in the last {window_days} days"
        )
        lines += [
            "",
            f"We also watch your public featured-role feed: **{feed_new}** new role(s) "
            f"appeared there {when}.",
        ]
    lines += [
        "",
        "---",
        "",
        f"_Leads read from your mail ({SITE}); market data from the Jack & Jill "
        f"featured-role feed ({SITE}/jobs/feed.xml). Jack is the network and the "
        f"candidate-facing agent; Jill is the recruiter and the company-facing "
        f"agent. Source: {SITE}._",
        "",
    ]
    return "\n".join(lines)


def write_letter(
    runtime,
    *,
    principal: str,
    role: str = "general",
    run_id: str = "",
    today: Optional[date] = None,
    force: bool = False,
    min_interval_days: int = MIN_INTERVAL_DAYS,
    window_days: int = WINDOW_DAYS,
    max_positions: int = MAX_POSITIONS,
) -> LetterResult:
    """Write this week's letter, if it is due and has something to say.

    A letter is only ledgered once written: positions it does not mention stay
    eligible for the next one. Returns a result describing what happened; a
    caller-visible failure is never raised — letters must not break a crawl.
    """
    today = today or date.today()
    letters_dir = Path(runtime.letters_dir)
    lettered = load_lettered(letters_dir)

    if not force and not due(lettered, today, min_interval_days=min_interval_days):
        last = last_letter_day(lettered)
        return LetterResult(
            written=False,
            reason=f"not due until {last + timedelta(days=min_interval_days)}" if last else "not due",
            window_days=window_days,
        )

    offerings = load_offerings(runtime.cache_dir)
    if not offerings:
        return LetterResult(written=False, reason="no offerings recorded yet", window_days=window_days)

    fresh, skipped = select_window(
        offerings, lettered, today, window_days=window_days, channel="drop"
    )
    # The feed is counted since our last letter, not by the nominal window: a
    # count Jack has already been told is not news, and the ledger holds drop
    # references only, so the feed needs its own clock.
    last_letter = last_letter_day(lettered)
    feed_window = window_days if last_letter is None else max(1, (today - last_letter).days)
    feed_new, _ = select_window(
        offerings, lettered, today, window_days=feed_window, channel="feed"
    )
    # Every channel the letter reports can be the reason to write it. The
    # employer feed moves most weeks even when no drop arrives, and 19 days of
    # silence on a live feed reads as "the agent stopped looking" (LJ's DUE
    # clock, 2026-10-09: last letter 2026-09-20, 0 fresh drops, 19 fresh feed
    # roles). feed_new is a rolling window count, so it decays on its own and a
    # genuinely quiet week still writes nothing.
    if not fresh and not feed_new:
        return LetterResult(
            written=False,
            reason="nothing new in the window",
            skipped_already_lettered=skipped,
            window_days=window_days,
        )

    text = compose_letter(
        principal=principal,
        role=role,
        fresh=fresh,
        window_days=window_days,
        today=today,
        run_id=run_id,
        skipped_already_lettered=skipped,
        letters_to_date=len(lettered) + len(fresh),
        max_positions=max_positions,
        applications_dir=getattr(runtime, "applications_dir", None),
        feed_new=len(feed_new),
        last_letter=last_letter_day(lettered),
    )
    letters_dir.mkdir(parents=True, exist_ok=True)
    path = letters_dir / f"jackandjill-{today.isoformat()}.md"
    path.write_text(text, encoding="utf-8")

    named = fresh[:max_positions]
    named_refs = {str(row.get("referencenumber")) for row in named}
    _append_ledger(letters_dir, [
        {
            "referencenumber": row.get("referencenumber"),
            "title": row.get("title"),
            "company": row.get("company"),
            "lettered_at": today.isoformat(),
            "run_id": run_id,
            "mentioned": str(row.get("referencenumber")) in named_refs,
            "letter": path.name,
        }
        for row in fresh
    ])
    logger.info(
        f"[jackandjill] Letter written: {path} "
        f"({len(named)} named, {len(fresh)} covered, {skipped} already lettered)"
    )
    # narrative mirror (D22, PLAN.md T3.1): the peer agent's memory carries
    # the relationship — what was sent, when, at what size
    try:
        import mem as _mem
        _mem.note_if_exists(
            _mem.memory_dir(principal, agent="Jack and Jill"),
            "letter: sent %s (%d named / %d covered, %d already lettered)"
            % (today.isoformat(), len(named), len(fresh), skipped))
    except Exception:
        pass  # the letters ledger is the authority; the mirror is best-effort
    return LetterResult(
        written=True,
        reason="written",
        path=path,
        positions=len(named),
        covered=len(fresh),
        skipped_already_lettered=skipped,
        window_days=window_days,
    )


def _append_ledger(letters_dir: Path, rows: List[dict]) -> None:
    with (letters_dir / LEDGER_FILENAME).open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _cli() -> None:
    """Standalone entry for cron: write the letter if it is due.

    Idempotent against the ledger, so a Monday cron job and the end-of-sweep
    hook can coexist: whoever runs first this week writes, the other no-ops.
    """
    import argparse

    from principal_runtime import resolve_principal_runtime

    parser = argparse.ArgumentParser(description="Write the Jack & Jill letter, if due.")
    parser.add_argument("--profile", required=True, help="principal the letter speaks for (e.g. lj)")
    parser.add_argument("--role", default="general")
    parser.add_argument("--run-id", default="", help="optional crawl run the letter reports on")
    parser.add_argument("--force", action="store_true", help="ignore the weekly due gate")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = write_letter(
        resolve_principal_runtime(args.profile),
        principal=args.profile,
        role=args.role,
        run_id=args.run_id,
        force=args.force,
    )
    if result.written:
        print(
            f"[jnj-letter] {args.profile} status=written path={result.path} "
            f"named={result.positions} covered={result.covered} "
            f"skipped={result.skipped_already_lettered}"
        )
    else:
        print(f"[jnj-letter] {args.profile} status=skipped reason={result.reason}")


if __name__ == "__main__":
    _cli()

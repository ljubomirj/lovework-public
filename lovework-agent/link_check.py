"""Re-check the links a crawl recommended, a few hours after it finished.

This is quality control on LoveWork's own output: it plays the principal,
opens every advert in the report's reader-facing sections (GO, MAYBE, FLAG,
New Listings — deduped, so an ad listed in two sections is opened once), and
records whether the link is still reachable, still live, and still the same
role. A recommendation that is already closed two hours after we made it is a
defect in the recommendation, not just in the market. Re-running the check on
later days appends further dated blocks, so link rot over a report's first
days is visible (2026-09-14: the only ads that die fast sit outside GO —
1-day-old LinkedIn adverts expired while every GO link was still fine).

Not the persona simulator of chapters 13/17 — that models a life; this models a
click.

Deterministic by design (no LLM): fetch, expiry markers, token overlap. The
verdicts are appended to the crawl's own report as a dated follow-up block, and
anything not `ok` is printed for Telegram delivery.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import config
from wiki_store import is_no_url_placeholder
from writer_boundary import WriterBusy, standalone_writer_guard

from enrichment import (
    BROWSER_UA,
    _expiry_marker,
    extract_html_text,
    is_linkedin_authwall,
    linkedin_job_id,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 25
MAX_CHARS = 20_000
FOLLOWUP_MARKER = "<!-- lovework-link-recheck -->"
TITLE_MATCH_THRESHOLD = 0.6
LINKEDIN_GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

#: ATS boards that answer a dead advert with a **server-side redirect whose
#: URL says so**. Greenhouse's own convention: the advert URL lands on the
#: board root with `?error=true` (measured 2026-10-18, Together AI — the
#: "this job is no longer open" banner is client-side and invisible to a text
#: fetch, but the redirect is server-side and final). One row per ATS,
#: extensible as the conventions are met: (host fragment, query marker).
ATS_CLOSED_REDIRECTS: tuple[tuple[str, str], ...] = (
    ("greenhouse.io", "error=true"),
)


def closed_redirect(url: str, *, timeout: float = 10.0, fetch=None) -> str:
    """The board URL a dead advert redirects to, or "".

    Cheap on purpose: a streamed GET that reads only enough to know where it
    landed. Server-side redirects only (`response.history`) — a client-side
    hop is not a claim the server made. `fetch(url) -> final_url_or_empty` is
    the injection seam that keeps tests off the network.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        final = fetch(url) if fetch is not None else _final_url(url, timeout)
    except Exception:
        return ""
    if not final:
        return ""
    parts = urlsplit(final)
    query = (parts.query or "").lower()
    host = (parts.netloc or "").lower()
    for fragment, marker in ATS_CLOSED_REDIRECTS:
        if fragment in host and marker in query:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return ""


def _final_url(url: str, timeout: float) -> str:
    import requests

    response = requests.get(url, timeout=timeout, allow_redirects=True,
                            stream=True, headers={"User-Agent": BROWSER_UA})
    try:
        return response.url if response.history else ""
    finally:
        response.close()

_ENTRY_RE = re.compile(r"^###\s+(?:\d+\.\s+)?(?P<org>.+?)\s+—\s+(?P<title>.+?)\s*$", re.M)
_URL_LINE_RE = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)", re.M)
_ALL_HEADERS_RE = re.compile(r"^##\s+", re.M)
_SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s+\((?P<count>\d+)\)\s*$", re.M)
_BLOCK_DATE_RE = re.compile(r"^## Link re-check — (\d{4}-\d{2}-\d{2})", re.M)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Every section whose adverts a reader can act on. DROP is excluded (already
# killed, 5x the link count for verdicts nobody acts on); Long-Lasting is
# excluded because its age signal is the registry's job, not the link's.
CHECK_SECTIONS = ("GO", "MAYBE", "FLAG", "New Listings")


@dataclass
class LinkVerdict:
    """One link's post-crawl verdict."""
    org: str
    title: str
    url: str
    verdict: str = "unknown"          # ok | expired | unreachable | authwall | live_unmatched | skipped | work_mode_conflict | location_ineligible
    status: Optional[int] = None
    method: str = ""
    title_match: Optional[bool] = None
    evidence: str = ""
    detail: str = ""
    page_excerpt: str = ""            # first KB of the fetched page, for later checks

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


def _section_slices(report_text: str) -> list[tuple[str, str]]:

    """(section name, block) for every `## Name (n)` header, block ends at the
    next `## ` of any kind (the trailing Link re-check header has no count)."""
    starts = [m.start() for m in _ALL_HEADERS_RE.finditer(report_text)]
    slices = []
    for i, start in enumerate(starts):
        m = _SECTION_RE.match(report_text, start)
        if not m:
            continue
        end = starts[i + 1] if i + 1 < len(starts) else len(report_text)
        slices.append((m.group("name"), report_text[start:end]))
    return slices


def _parse_section(block: str) -> list[dict]:
    """Entries (`### Org — Title` + `- **URL**:`) inside one section slice."""
    entries: list[dict] = []
    for entry_match in _ENTRY_RE.finditer(block):
        tail = block[entry_match.end():]
        url_match = _URL_LINE_RE.search(tail)
        url = url_match.group("url").strip() if url_match else ""
        if is_no_url_placeholder(url):
            url = ""
        entries.append({
            "org": entry_match.group("org").strip(),
            "title": re.sub(r"\s*[🆕🚩⭐]+$", "", entry_match.group("title").strip()),
            "url": url,
        })
    return entries


def parse_go_entries(report_text: str, section: str = "GO") -> list[dict]:
    """Extract (org, title, url) for one section of a crawl report.

    The report is written by `wiki_store._format_entry`, so an entry starts a
    `### Org — Title` heading and carries its link on a `- **URL**:` line.
    A resolved lead's line carries "<preferred> (via <slug>)"; the preferred
    URL is the first token.
    """
    return [e for name, block in _section_slices(report_text) if name == section
            for e in _parse_section(block)]


def parse_report_entries(report_text: str) -> list[dict]:
    """Every advert link a reader can open, deduped by URL.

    The same ad may sit in GO and again under New Listings; the reader gets
    one verdict per link, with the sections it appeared in recorded.
    """
    entries: list[dict] = []
    by_key: dict[str, dict] = {}
    for name, block in _section_slices(report_text):
        if name not in CHECK_SECTIONS:
            continue
        for entry in _parse_section(block):
            key = entry["url"] or f'{entry["org"].lower()}\t{entry["title"].lower()}'
            prior = by_key.get(key)
            if prior is None:
                entry["sections"] = [name]
                by_key[key] = entry
                entries.append(entry)
            elif name not in prior["sections"]:
                prior["sections"].append(name)
    return entries


def _fetch(url: str) -> tuple[str, Optional[int], str]:
    """Return (text, status, error). LinkedIn goes through the guest endpoint."""
    import httpx

    target = url
    job_id = linkedin_job_id(url)
    if job_id:
        target = LINKEDIN_GUEST_URL.format(job_id=job_id)
    try:
        response = httpx.get(
            target,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-GB,en;q=0.9"},
        )
    except Exception as exc:
        return "", None, f"{type(exc).__name__}: {exc}"[:200]
    text = extract_html_text(response.text)[:MAX_CHARS]
    if response.status_code >= 400:
        return text, response.status_code, f"HTTP {response.status_code}"
    return text, response.status_code, ""


def _title_matches(title: str, text: str) -> bool:
    """Token overlap, ignoring stopword-ish short tokens."""
    wanted = {token for token in _TOKEN_RE.findall(title.lower()) if len(token) > 3}
    if not wanted:
        return False
    found = {token for token in _TOKEN_RE.findall((text or "").lower()) if len(token) > 3}
    return len(wanted & found) / len(wanted) >= TITLE_MATCH_THRESHOLD


_JS_SHELL_MIN_CHARS = 500


def _rendered_fetch(url: str,
                    *, use_cache: bool = False) -> tuple[str, Optional[int], str]:
    """The crawler's provider chain (exa/firecrawl/http); cache on request."""
    try:
        from crawler import fetch_page

        md, _provider = fetch_page(url, use_cache=use_cache)
        if md:
            return md, 200, ""
        return "", None, "rendered fetch empty"
    except Exception as exc:  # a fetch failure is data, not a crash
        return "", None, f"{type(exc).__name__}: {exc}"[:200]


def _apply_work_mode(verdict: "LinkVerdict", requires_remote: bool) -> None:
    """Flip a live verdict to work_mode_conflict when a remote-only principal
    faces an on-site-only page (the Callosum lesson, caught the second time —
    LJ, 2026-10-06). Uses the matcher's own patterns, so crawl and re-check
    can never disagree on the rule."""
    if not requires_remote or verdict.verdict not in ("ok", "live_unmatched"):
        return
    from matcher import (_ONSITE_ONLY_RE, _WORK_MODE_OK_RE,
                         _sanitize_work_mode_text)

    text = _sanitize_work_mode_text(verdict.page_excerpt)
    if not text or _WORK_MODE_OK_RE.search(text):
        return
    m = _ONSITE_ONLY_RE.search(text)
    if m:
        verdict.verdict = "work_mode_conflict"
        verdict.detail = (f"page states '{m.group(0).strip()}' with no "
                          f"remote/hybrid signal (profile is remote-only)")


def _apply_work_location(verdict: "LinkVerdict", profile: str) -> None:
    """Flip a live verdict to `location_ineligible` when the page names a
    workplace outside the principal's allowed places.

    **Why the re-check needs this.** Eligibility was judged at assessment time,
    on the text the crawl had — and an advert whose location the source did not
    state passes every check ("Location: unspecified"): measured 2026-10-14, lj
    held a GO pack for a Poetiq role whose live page reads *"Los Altos · Full
    time · On-site · $150K–$500K"*. The 06:00 pass fetches that page every day
    and had no rule to notice. It uses the matcher's own function, so assessment
    and re-check can never disagree about eligibility (the same discipline as
    `_apply_work_mode`).
    """
    if verdict.verdict not in ("ok", "live_unmatched"):
        return
    try:
        from matcher import _check_work_location_kill

        reason = _check_work_location_kill("", verdict.page_excerpt, profile)
    except Exception:
        return
    if reason:
        verdict.verdict = "location_ineligible"
        verdict.detail = reason[:300]


def _retain_advert_page(url: str, text: str) -> None:
    """Store the freshly fetched advert for whoever reads content, not liveness.

    Write-only: the check itself never reads it back (see the call site).
    """
    try:
        from crawler import _cache_path

        path = _cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except Exception as exc:                       # retention is best-effort
        logger.debug("[link-check] could not retain %s: %s", url, exc)


def check_url(
    url: str,
    title: str = "",
    *,
    org: str = "",
    fetcher: Callable[[str], tuple[str, Optional[int], str]] = _fetch,
) -> LinkVerdict:
    """Open one recommended link and judge it.

    `fetcher` is the injection seam: it returns `(text, status, error)` and
    defaults to the real HTTP path, so tests never touch the network.
    """
    # The verdict records the CANONICAL link, not the tracked one we fetched:
    # a LinkedIn digest URL carries ~1,000 characters of trackingId/midToken/
    # otpToken, and this string is printed in the report's follow-up block, the
    # brief and the machine record (seen in the 2026-10-08 brief). One cleaner,
    # `wiki_store.clean_url`, already used by the wiki writer.
    from wiki_store import clean_url

    verdict = LinkVerdict(org=org, title=title, url=clean_url(url or ""))
    if not url or not url.lower().startswith("http"):
        verdict.verdict = "skipped"
        verdict.detail = "no link in the report"
        return verdict

    if linkedin_job_id(url):
        verdict.method = "linkedin-guest"
    text, status, error = fetcher(url)
    if not error and len((text or "").strip()) < _JS_SHELL_MIN_CHARS:
        # A JS app served its shell: plain HTTP sees 60 bytes where the
        # browser sees the advert (Ashby boards — LJ, 2026-10-06). Ask the
        # crawler's provider chain for the rendered page before judging.
        # **A liveness check must read a live page** (2026-10-14). This pass
        # cached the advert page it fetched (for the email detector and the
        # report's "Cached advert" line) and then, on the next run, *read* that
        # cache — so its verdict described the page as it was the first time we
        # looked, for ever. LJ clicked a GO whose page says "The page you are
        # looking for doesn't exist" while our verdict said `ok` for four days.
        #
        # The retention is kept, the reading is not: fetch live, then store the
        # text as evidence for the consumers that want content rather than
        # liveness.
        r_text, r_status, r_error = _rendered_fetch(url, use_cache=False)
        if r_text:
            _retain_advert_page(url, r_text)
        # **A closure page is legitimately short.** `_JS_SHELL_MIN_CHARS` asks "is
        # this a JavaScript shell rather than the page?", and it was applied to a
        # body that answers the question the check is asking: Workday's 404 is 381
        # characters reading *"The page you are looking for doesn't exist"* — under
        # the gate, so it was discarded, the empty plain-HTTP body was judged
        # instead, and the advert came out `live_unmatched` ("the page no longer
        # carries the role") rather than `expired` (2026-10-14, EMBL-EBI, kj).
        # A short body carrying a closure marker is evidence, not a shell.
        if r_text and not r_error and (
                len(r_text.strip()) >= _JS_SHELL_MIN_CHARS
                or _expiry_marker(r_text)):
            text, status, error = r_text, r_status or 200, ""
            verdict.method = "rendered"
        else:
            # Last honest basis: the crawl-time cached page (sealed evidence).
            #
            # **It is not a liveness verdict, so it must not read like one.** The
            # first version judged the cached text and returned `ok`, recording
            # "cache" only in the `method` field — which no reader consults. So a
            # page we could no longer open was reported live: LJ clicked a GO whose
            # advert says *"The page you are looking for doesn't exist"*, and our
            # verdict said `ok` for four days running (2026-10-14).
            #
            # A page we could not read is `unverified`: every consumer must treat
            # it as unknown, not as alive. The distinction already existed in the
            # method field; now it is in the verdict, where the decisions are made.
            c_text, c_status, c_error = _rendered_fetch(url, use_cache=True)
            if c_text and not c_error and len(c_text.strip()) >= _JS_SHELL_MIN_CHARS:
                text, status, error = c_text, c_status or 200, ""
                verdict.method = "cache"
                verdict.verdict = "unverified"
                verdict.detail = ("the live page could not be read; this is the "
                                  "crawl-time copy, so the advert is not verified "
                                  "as open")
                verdict.page_excerpt = (text or "")[:4000]
                return verdict
    verdict.status = status
    verdict.page_excerpt = (text or "")[:4000]
    if not verdict.method:
        verdict.method = "http"

    if error and not text:
        verdict.verdict = "unreachable"
        verdict.detail = error
        return verdict
    if error and status and status >= 400:
        verdict.verdict = "unreachable"
        verdict.detail = error
        return verdict
    if is_linkedin_authwall(text):
        verdict.verdict = "authwall"
        verdict.detail = "LinkedIn served the sign-in page instead of the advert"
        return verdict

    # A server-side closed-redirect is the strongest closure signal there is:
    # the employer's own ATS moved the advert URL onto its board root with the
    # error in the query. The board page itself is alive and job-rich, so the
    # content checks below would call it `live_unmatched` at best — and the
    # "no longer open" banner is client-side, invisible to any text fetch
    # (2026-10-18, Together AI).
    board = closed_redirect(verdict.url or url)
    if board:
        verdict.verdict = "expired"
        verdict.evidence = board
        verdict.detail = f"redirected to the employer's board: {board}"
        return verdict

    marker = _expiry_marker(text)
    if marker:
        verdict.verdict = "expired"
        verdict.evidence = marker
        verdict.detail = f"advert closed: {marker!r}"
        return verdict

    matched = _title_matches(title, text)
    verdict.title_match = matched
    if matched:
        verdict.verdict = "ok"
    else:
        verdict.verdict = "live_unmatched"
        verdict.detail = "page live but the role title was not found on it"
    return verdict


def recheck(
    report_path: Path,
    *,
    limit: Optional[int] = None,
    sections: Optional[set[str]] = None,
    checker=check_url,
    principal: str = "lj",
) -> dict:
    """Check every advert link a reader can open (GO, MAYBE, FLAG, New
    Listings, deduped by URL). Returns a summary dict.

    Problem verdicts mirror into the org's memory (D22, PLAN.md T4.1),
    note-if-exists — the dead-advert backlog becomes a per-org narrative
    instead of shell archaeology."""
    report_path = Path(report_path)
    text = report_path.read_text(encoding="utf-8")
    entries = parse_report_entries(text)
    # The daily pass checks the ACTIONABLE section only (GO): a closed GO is
    # the expensive mistake — an evening spent on a pack for an advert that
    # ended — while a dead FLAG is noise. The twice-weekly pass checks
    # everything. Filtering is by the sections an entry was listed under, so
    # an advert that is GO *and* New Listings is checked once, as GO.
    if sections:
        wanted = {s.upper() for s in sections}
        entries = [e for e in entries
                   if wanted & {s.upper() for s in (e.get("sections") or [])}]
    if limit is not None:
        entries = entries[:limit]

    verdicts = [
        checker(entry["url"], entry["title"], org=entry["org"]) for entry in entries
    ]
    requires_remote = False
    try:
        import config as _config
        from matcher import WORK_MODE_MARKER
        requires_remote = WORK_MODE_MARKER in _config.load_profile_text(
            principal).lower()
    except Exception:
        requires_remote = False  # no profile, no marker — the check is inert
    profile_text = ""
    try:
        import config as _config

        profile_text = _config.load_profile_text(principal)
    except Exception:
        profile_text = ""
    for verdict in verdicts:
        _apply_work_mode(verdict, requires_remote)
        _apply_work_location(verdict, profile_text)
    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
    try:
        import mem as _mem
        for v in verdicts:
            if v.ok or not v.org:
                continue
            status = " %d" % v.status if v.status else ""
            memdir = _mem.memory_dir(principal, org=v.org)
            if v.verdict == "work_mode_conflict":
                # a defect this loud may mint: the org is GO-live in a report
                # (bounded by GO volume), and the morning brief's org-memory
                # context needs this memory to exist to quote it
                if not (memdir / "LOG.txt").exists():
                    _mem.run_memo(memdir, ["init"], quiet=True)
            _mem.note_if_exists(
                memdir,
                "source: advert %s%s — %s" % (
                    v.verdict, status, (v.detail or "")[:80]))
    except Exception:
        pass  # link verdicts are the authority; the mirror is best-effort
    return {
        "report": str(report_path),
        "checked": len(verdicts),
        "counts": counts,
        "problems": [v for v in verdicts if not v.ok],
        "verdicts": verdicts,
        # `sections` = what the pass actually looked at (the block's prose);
        # `scope` = what it was ASKED to look at. The one-per-day guard needs
        # the second: a GO-only pass whose GO section happened to hold no links
        # would otherwise declare an empty scope (2026-10-08).
        "sections": sorted({s for e in entries for s in e["sections"]}),
        "scope": sorted({s.upper() for s in sections}) if sections
                 else list(CHECK_SECTIONS),
    }

def _run_record(runtime, run_id: str) -> dict:
    path = Path(runtime.cache_dir) / "runs" / f"{run_id}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def followup_markdown(
    result: dict, *, run_id: str = "", notification: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> str:
    """The dated block appended to the crawl report.

    `now` is injectable so the block's date always agrees with the
    one-per-day append guard (tests and replays pin it).
    """
    now = now or datetime.now().astimezone()
    origin = f" from run `{run_id}`" if run_id else ""
    lines = [
        FOLLOWUP_MARKER,
        "",
        f"## Link re-check — {now.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        (
            f"A simulated principal opened all {result['checked']} advert"
            f"{'' if result['checked'] == 1 else 's'}{origin} across "
            # observed first, then asked-for: a pass that found no link in its
            # scope must not print "across ," (2026-10-08)
            f"{', '.join(result.get('sections') or result.get('scope') or CHECK_SECTIONS)}, "
            "to see whether "
            "the recommendation still holds up after the crawl."
        ),
        "",
        # A DECLARED scope, not prose: the one-per-day guard has to know whether
        # today's check covered at least the sections this run wants, or the
        # 06:00 GO-only pass would suppress the 14:00 full pass (and with it
        # every MAYBE/FLAG/New Listings link) — the defect found 2026-10-08.
        f"- Sections checked: **{', '.join(result.get('scope') or result.get('sections', CHECK_SECTIONS))}**",
        f"- Checked: **{result['checked']}** link(s)",
    ]

    for name in ("ok", "expired", "unreachable", "authwall", "live_unmatched", "skipped"):
        count = result["counts"].get(name)
        if count:
            lines.append(f"- {name}: **{count}**")
    if notification:
        lines.append(
            f"- Notification: {notification.get('status', 'unknown')}"
            + (f" (id `{notification.get('message_id')}`)" if notification.get("message_id") else "")
        )
    lines.append("")

    if not result["problems"]:
        lines.extend(["_Every checked advert link resolved to a live advert for the role._", ""])
        return "\n".join(lines)

    lines.extend(["### Not holding up", "", "| Verdict | Org — Title | Evidence |", "|---|---|---|"])
    for verdict in result["problems"]:
        lines.append(
            f"| {verdict.verdict} | {verdict.org} — {verdict.title} | "
            f"{verdict.detail or verdict.evidence or ''} |"
        )
    lines.append("")
    return "\n".join(lines)


#: A block's declared scope line (see `followup_markdown`).
_SECTIONS_LINE_RE = re.compile(r"(?m)^- Sections checked: \*\*(.+?)\*\*\s*$")


def _block_sections(report_text: str, day: str) -> list[set[str]]:
    """The declared scope of every block dated `day`, newest first.

    A block written before the scope line existed declares nothing: it counts as
    covering everything, which is the conservative reading (it suppresses a
    narrower re-run rather than allowing a duplicate block).
    """
    text = report_text
    scopes: list[set[str]] = []
    for match in re.finditer(r"(?m)^## Link re-check — " + re.escape(day), text):
        tail = text[match.end():]
        nxt = re.search(r"(?m)^## ", tail)
        body = tail[:nxt.start()] if nxt else tail
        declared = _SECTIONS_LINE_RE.search(body)
        scopes.append({s.strip().upper() for s in declared.group(1).split(",")}
                      if declared else {s.upper() for s in CHECK_SECTIONS})
    return scopes


def _parse_sections(value: Optional[str], parser=None) -> Optional[set[str]]:
    """The requested sections, validated.

    A typo must not silently check nothing: `--sections GOs` would match no
    entry, record an empty verdict and leave the watch quiet forever — a filter
    that matches nothing is a failure, not an empty result (2026-10-08).
    """
    if not value:
        return None
    wanted = {s.strip().upper() for s in value.split(",") if s.strip()}
    known = {s.upper() for s in CHECK_SECTIONS}
    unknown = sorted(wanted - known)
    if unknown:
        message = (f"unknown section(s): {', '.join(unknown)} — "
                   f"known: {', '.join(CHECK_SECTIONS)}")
        if parser is not None:
            parser.error(message)
        raise ValueError(message)
    return wanted


def already_checked_today(report_text: str, today: Optional[str] = None,
                          sections: Optional[set[str]] = None) -> bool:
    """True when today's record already covers the sections this run wants.

    Blocks accumulate one per day (a report's rot is re-measured daily), so only
    a same-day block suppresses the append — and it suppresses it only when it
    is at least as wide. A GO-only pass at 06:00 must not stop the full pass at
    14:00 from re-opening the MAYBE/FLAG/New Listings links, and a full pass
    must not be followed by a narrower duplicate (2026-10-08).
    """
    day = today or datetime.now().strftime("%Y-%m-%d")
    scopes = _block_sections(report_text, day)
    if not scopes:
        return False
    if not sections:
        return True
    wanted = {s.upper() for s in sections}
    return any(wanted <= scope for scope in scopes)


def append_followup(report_path: Path, block: str, *, today: Optional[str] = None,
                    sections: Optional[set[str]] = None) -> bool:
    """Append one dated follow-up block per day. Returns True when written."""
    report_path = Path(report_path)
    text = report_path.read_text(encoding="utf-8")
    if already_checked_today(text, today, sections):
        logger.info("Link re-check already recorded today in %s", report_path)
        return False
    if not text.endswith("\n"):
        text += "\n"
    report_path.write_text(f"{text}\n{block}\n", encoding="utf-8")
    logger.info("Link re-check appended to %s", report_path)
    return True


def save_machine_readable(runtime, report_path: Path, result: dict) -> Path:
    """Keep the verdicts next to the run's other evidence."""
    out_dir = Path(runtime.cache_dir) / "link_checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{Path(report_path).stem}.json"
    if path.exists():
        # Re-checks append dated blocks to the report; their machine records
        # must not overwrite the earlier day's verdicts.
        path = out_dir / f"{Path(report_path).stem}-{datetime.now():%H%M%S}.json"
    payload = {
        "report": str(report_path),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": result["counts"],
        "verdicts": [
            {key: value for key, value in asdict(v).items()} for v in result["verdicts"]
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def newest_report(wiki_root: Path, run_type: str = "full") -> Optional[Path]:
    """The newest crawl report for a principal (not a reassessment/consume)."""
    reports = sorted(
        (Path(wiki_root) / "reports").glob(f"*-{run_type}-report.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def run_for_report(runtime, report_path: Path) -> dict:
    """The run-ledger record whose report this is (for id + notification)."""
    runs_dir = Path(runtime.cache_dir) / "runs"
    if not runs_dir.is_dir():
        return {}
    target = str(Path(report_path))
    for path in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("report_file") == target:
            return record
    return {}


def main() -> None:
    """Direct invocation: hold the writer lease, then re-check the links.

    `link_check` runs from cron ~3 h after a sweep and *edits the report*, so it
    is a writer like any other: it must not append to a report while another
    operation is sealing that principal's state (docs/28 §WP1b box ①).
    """
    import sys as _sys

    principal = config.principal_from_argv(_sys.argv[1:])
    try:
        with standalone_writer_guard(principal, mode="direct",
                                     state_root=config.STATE_DIR / principal):
            return _link_check_body()
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)


def _link_check_body() -> None:
    import argparse
    import sys

    from principal_runtime import resolve_principal_runtime

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="Principal whose report to re-check")
    parser.add_argument("--role", default="general")
    parser.add_argument("--report", help="Explicit report path (default: newest full report)")
    parser.add_argument("--limit", type=int, help="Only check the first N entries")
    parser.add_argument(
        "--sections",
        help="Comma-separated sections to check (default: every reader-facing "
             "one). The daily pass uses `GO`.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Skip silently when the newest report is older than this (the crawl has not finished)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check without writing anything")
    args = parser.parse_args()

    runtime = resolve_principal_runtime(args.profile)
    report = Path(args.report) if args.report else newest_report(runtime.wiki_root)
    if report is None or not report.is_file():
        logger.info("No full report to re-check for %s", args.profile)
        return
    age_hours = (datetime.now().timestamp() - report.stat().st_mtime) / 3600
    if age_hours > args.max_age_hours:
        # The crawl has not produced a fresh report (still running, or failed —
        # the watchdog owns that case). Stay silent.
        logger.info(
            "Newest report for %s is %.1fh old (> %.1fh); nothing to re-check",
            args.profile, age_hours, args.max_age_hours,
        )
        return
    wanted = _parse_sections(args.sections, parser)
    # The guard compares the REQUESTED scope: no `--sections` means every
    # reader-facing section, so a 06:00 GO-only record does not stop the 14:00
    # full pass (and a full record does stop a narrower one).
    scope = wanted or {s.upper() for s in CHECK_SECTIONS}
    if already_checked_today(report.read_text(encoding="utf-8"), sections=scope):
        logger.info("Re-check already recorded today for %s", report)
        return

    result = recheck(report, limit=args.limit, sections=wanted)
    record = run_for_report(runtime, report)
    counts = result["counts"]
    if args.dry_run:
        print(
            f"[link-check] {args.profile} DRY RUN {report.name}: checked={result['checked']} "
            + " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        )
        for verdict in result["problems"]:
            print(f"  ! {verdict.verdict}: {verdict.org} — {verdict.title} ({verdict.url})")
        return

    append_followup(report, followup_markdown(result, run_id=record.get("run_id", ""),
                                              notification=record.get("notification")),
                    sections=scope)
    saved = save_machine_readable(runtime, report, result)
    logger.info("Verdicts saved to %s", saved)
    # The re-check is a late fact about an already-sealed run, so it becomes a
    # CHILD operation of that run rather than an edit to its bundle (docs/28
    # §WP1b box ⑥). Best-effort: a re-check that cannot be sealed still stands.
    parent_run_id = str(record.get("run_id") or "")
    if parent_run_id:
        try:
            from receipt_operation import seal_receipt

            bundle = seal_receipt(
                principal=args.profile, parent_run_id=parent_run_id,
                kind="link_recheck",
                facts={"report": report.name, "checked": result["checked"],
                       "counts": dict(counts)},
                state_root=runtime.cache_dir.parent,
            )
            if bundle is not None:
                logger.info("Re-check sealed as child operation %s", bundle.name)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Re-check receipt not sealed: %s", e)
    print(
        f"[link-check] {args.profile} {report.name}: checked={result['checked']} "
        + " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    )
    for verdict in result["problems"]:
        print(f"  ! {verdict.verdict}: {verdict.org} — {verdict.title} — {verdict.detail}")


if __name__ == "__main__":
    main()

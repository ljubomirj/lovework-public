#!/usr/bin/env python3
"""
LoveWork success notification — sends an email summary after a completed run.

Called from the cron worker after the crawl succeeds:
    python3 notify.py --report <path> --log <path>
    python3 notify.py --report <path> --run-id <id> --runs-dir <dir> --record-only
                                   # no email: record the retirement, honestly

Post-crawl emails were retired on 2026-10-18 (LJ: *"the emails we send after a
crawl — stop sending them. No longer relevant. We don't care about a crawl. We
care about a state"*). The crawl wrapper now calls `--record-only`, so a run's
record keeps a terminal notification state (`suppressed` + reason) instead of a
delivery — the Monday state email (`state_email.py`) carries the state, not the
delta. `--record-only` never touches Gmail and never seals a delivery receipt.

Sends the worker's final results summary via Gmail.  The final log block is
authoritative: it contains the decisions, lifecycle counts, and curated
new/GO/MAYBE listings calculated by the pipeline itself.
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
from html import escape
from urllib.parse import urlsplit
from datetime import datetime
from pathlib import Path
from hermes_context import hermes_home_or_none, identity_line
from report_header import parse_section_counts, parse_legacy_counts
from run_ledger import record_notification

logger = logging.getLogger(__name__)

# NOTE: no logging.basicConfig() at import time. `doc_serve` imports
# `human_slug` from this module for the dashboard's link rendering, and a
# library module must not reconfigure the importing application's logging —
# it would silently override the dashboard's own format and level.

# Decoupling plan A5 (2026-10-21): Hermes is optional. The Gmail script is
# ours (gmail_accessor resolves it ours-first, A7), and the token home follows
# the plan's chain — it may be None on a host with no credentials yet, and
# then the notifier *says so* instead of its import crashing the crawl that
# needs it (it used to resolve the Hermes profile at import and raise).
def _token_home() -> Path | None:
    explicit = os.environ.get("LOVEWORK_GOOGLE_HOME", "").strip()
    if explicit:
        return Path(explicit)
    target = Path(os.getenv("LOVEWORK_HOME") or Path.home() / ".lovework") / "credentials" / "gmail"
    if (target / "google_token.json").is_file():
        return target
    return hermes_home_or_none()

TO_EMAIL = "user@example.invalid"

# Who hears about a run (LJ, 2026-09-20; VJ added to his own list 2026-10-01). The
# principal's own inbox comes first where he has one; the operators who review
# every crawl follow — LJ (the agent's owner) and PK (second-opinion reader). All
# three principals now receive their own reports; an unknown principal falls back
# to LJ alone. The list becomes the message's `To:` header, so one run is one
# email with one delivery receipt.
RECIPIENTS = {
    "lj": ("user@example.invalid", "user@example.invalid"),
    "vj": (
        "user@example.invalid",
        "user@example.invalid",
        "user@example.invalid",
    ),
    "kj": (
        "user@example.invalid",
        "user@example.invalid",
        "user@example.invalid",
    ),
}


def recipients_for(principal: str) -> tuple[str, ...]:
    """Every address that should hear about this principal's run."""
    return RECIPIENTS.get(str(principal or "").lower(), (TO_EMAIL,))


def recipient_for(principal: str, override: str | None = None) -> str:
    """The `To:` value: an explicit override, else the configured list.

    Several people review each run, so this is an RFC 5322 address list rather
    than one address; the Gmail helper assigns it to the To header verbatim.

    The override exists for operator review (read a principal's notification
    before it reaches them) and for re-sends after a delivery failure; it
    replaces the whole list.
    """
    return override or ", ".join(recipients_for(principal))


# ── Readable links ──────────────────────────────────────────────────────────
# Job boards attach per-recipient tracking blobs, so an advert link runs to
# hundreds of characters of base64. The notification shows a readable slug as
# the link text and keeps the full URL in the link itself.

_OPAQUE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+=/-]{24,}$")


def _short_segment(segment: str) -> str:
    """Shorten one opaque path segment; meaningful words are left alone."""
    if _OPAQUE_UUID_RE.match(segment):
        return segment[:8] + "…"
    if (
        len(segment) >= 24
        and len(re.findall(r"[-_]", segment)) <= 1
        and _OPAQUE_TOKEN_RE.match(segment)
    ):
        return segment[:6] + "…"
    return segment


def human_slug(url: str, max_len: int = 64) -> str:
    """Return a short, human-readable stand-in for a URL.

    A URL that is already short and free of tracking parameters is returned
    unchanged, so the reader sees exactly what it is. A long or parameterised
    one becomes `host/meaningful/path`, with opaque segments elided and the
    middle dropped when it still does not fit.
    """
    parts = urlsplit(str(url))
    if not parts.netloc:
        return str(url)
    if not parts.query and not parts.fragment and len(url) <= max_len:
        return str(url)
    host = parts.netloc.replace("www.", "")
    shown = [_short_segment(s) for s in parts.path.split("/") if s]
    slug = "/".join([host] + shown) if shown else host
    if len(slug) > max_len and len(shown) > 1:
        slug = f"{host}/…/{shown[-1]}"
    if len(slug) > max_len:
        slug = host
    return slug if len(slug) <= max_len else slug[: max_len - 1] + "…"


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_HTML_BODY_STYLE = (
    "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "white-space:pre-wrap;font-size:13px;line-height:1.45"
)


def render_html(body: str) -> str:
    """Render a plain-text notification body as HTML with readable link text.

    Clicking shows the reader where a link goes; the tracking blob itself only
    exists in the anchor's href, never as visible text.
    """
    out, last = [], 0
    for match in _URL_RE.finditer(body):
        out.append(escape(body[last:match.start()]))
        url = match.group(0)
        out.append(
            f'<a href="{escape(url, quote=True)}">{escape(human_slug(url))}</a>'
        )
        last = match.end()
    out.append(escape(body[last:]))
    return f'<div style="{_HTML_BODY_STYLE}">{"".join(out)}</div>'

# Most widely reachable view of a finished crawl (LAN dashboard, item 6 of
# the 2026-09-10 notification feedback).
DASHBOARD_BASE_URL = os.getenv("LOVEWORK_DASHBOARD_URL", "http://192.0.2.10:8765")

# google_api.py accepts the email body ONLY as a CLI argument.  Linux rejects
# a single argument longer than MAX_ARG_STRLEN (32 * PAGE_SIZE = 128 KiB) with
# E2BIG before the child even starts.  A heavy run's curated listing easily
# exceeds that once render_html() wraps every URL (observed 2026-09-12: 161,614
# bytes for a 226-GO run with the jackandjill source; the completion email was
# never sent).  Cap the body with headroom for the subject + helper argv and
# point the reader at the report link that already sits at the top.
MAX_ARGV_BODY_BYTES = 120_000
#: The mail always carries the complete GO set; if that is thinner than this,
#: the whole MAYBE section follows, and if the two together are still thinner,
#: DROP's first N are listed as a floor. Rules and rationale: format_email.
EMAIL_MIN_LISTINGS = int(os.getenv("LOVEWORK_NOTIFY_MIN_LISTINGS", "20"))

_TRUNCATION_MARKER = "\n\n[…] body truncated to fit the mail transport limit; full listing in the report link above."


def _fit_body_for_argv(body: str, limit: int = MAX_ARGV_BODY_BYTES) -> str:
    """Bound the notification body to what execve can carry as one --body arg.

    Never splits a multi-byte UTF-8 sequence; the marker keeps the reader
    pointed at the report.  The first lines (report web/disk paths, decision
    counts) are always preserved.
    """
    data = body.encode("utf-8")
    if len(data) <= limit:
        return body
    marker = _TRUNCATION_MARKER.encode("utf-8")
    cut = data[: max(0, limit - len(marker))]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _find_gapi() -> Path:
    """The google_api.py script — ours first (vendored, A7), the skill after."""
    from gmail_accessor import gapi_path

    return gapi_path()


def _gapi_env() -> dict[str, str]:
    """Run the Google script against the resolved token home (both env keys:
    our vendored script reads LOVEWORK_GOOGLE_HOME, the skill-era one HERMES_HOME)."""
    env = os.environ.copy()
    home = _token_home()
    if home is not None:
        env["LOVEWORK_GOOGLE_HOME"] = str(home)
        env["HERMES_HOME"] = str(home)
    return env


# --- Gmail OAuth token pre-flight check -------------------------------------
# A revoked token cannot send the completion email, but the failure only
# surfaces after a multi-hour crawl.  This read-only check attempts a token
# refresh without writing anything back, so the wrapper can detect a dead
# credential before the crawl starts (LEARNINGS 2026-07-25 prevention).

def _token_file_age(token_path: Path) -> str:
    """Human-readable age of the token file for blast-radius sizing.

    A revoked token is usually dead for days before a full-crawl incident
    surfaces it; the file mtime shows when it last refreshed (or was
    re-consented).  Returning it in the check detail lets the incident
    packet answer "how long have crawls been skipping?" without digging
    into run ledgers.
    """
    try:
        mtime = datetime.fromtimestamp(token_path.stat().st_mtime)
        age_days = (datetime.now() - mtime).total_seconds() / 86400
        return f" (token file mtime {mtime:%Y-%m-%d %H:%M}, age {age_days:.1f}d)"
    except OSError:
        return ""


def _load_credentials(token_path: Path):
    """Load an authorized-user credential from the Hermes profile token file."""
    from google.oauth2.credentials import Credentials
    return Credentials.from_authorized_user_file(str(token_path))


def _refresh_credentials(creds) -> None:
    """Attempt a read-only token refresh; never persists the refreshed token."""
    from google.auth.transport.requests import Request
    creds.refresh(Request())


def check_token(hermes_home: Path | None = None) -> dict[str, str | bool]:
    """Classify Gmail OAuth token health without mutating the token file.

    Returns ``ok`` plus a ``status`` and human-readable ``detail``.  Used as
    a pre-flight check in the crawl wrapper so a revoked token is detected
    at crawl start instead of as a post-crawl notification failure.
    """
    hermes_home = hermes_home if hermes_home is not None else _token_home()
    if hermes_home is None:
        return {"ok": False, "status": "missing",
                "detail": "no Gmail token home: neither LOVEWORK_GOOGLE_HOME, "
                          "~/.lovework/credentials/gmail nor a Hermes profile"}
    token_path = hermes_home / "google_token.json"
    if not token_path.exists():
        return {
            "ok": False,
            "status": "missing",
            "detail": f"no Gmail token file at {token_path}",
        }
    try:
        creds = _load_credentials(token_path)
        if not creds.refresh_token:
            return {
                "ok": False,
                "status": "missing_refresh_token",
                "detail": "Gmail token file has no refresh_token",
            }
        _refresh_credentials(creds)
    except Exception as exc:
        message = str(exc)
        if "invalid_grant" in message:
            return {
                "ok": False,
                "status": "revoked",
                "detail": "Gmail OAuth token expired or revoked (invalid_grant); "
                          "re-run the Google OAuth setup flow for the HermeL profile"
                          + _token_file_age(token_path),
            }
        return {"ok": False, "status": "error", "detail": message[:300]}
    return {
        "ok": True,
        "status": "authenticated",
        "detail": "Gmail OAuth token can refresh",
    }


def classify_notification_error(stderr: str) -> str:
    """Prefix a notification failure with its actionable cause when known.

    Keeps the full traceback for evidence while making the root cause the
    first thing a watchdog, incident, or operator reads.
    """
    if "invalid_grant" in stderr:
        return (
            "GMAIL_OAUTH_TOKEN_REVOKED: Gmail OAuth token expired or revoked — "
            "re-run the Google OAuth setup flow (google-oauth-renewal skill).\n"
            + stderr.strip()[:2500]
        )
    return stderr.strip()[:2500]


def parse_report(report_path: Path) -> dict:
    """Extract summary info from a LoveWork report file."""
    text = report_path.read_text(encoding="utf-8", errors="ignore")

    # Run type from H1 ("# FULL SWEEP — …", "# FULL — …", "# INCREMENTAL — …").
    run_type = ""
    m = re.search(r"^# (INCREMENTAL|FULL(?:\s+SWEEP)?)\b", text, re.MULTILINE)
    if m:
        run_type = "Incremental" if m.group(1).upper().startswith("INC") else "Full"
    if not run_type:
        # KJ's 2026-09-10 report H1 is "# FULL — …" and produced the infamous
        # "LoveWork Unknown" subject; the filename knows the run type too.
        name_lower = report_path.name.lower()
        if "reassessment" in name_lower:
            # A reassessment is a registry replay, not a crawl — the email must
            # not call it one (see _MODE_LABELS).
            run_type = "Reassessment"
        elif "incremental" in name_lower:
            run_type = "Incremental"
        elif "full" in name_lower:
            run_type = "Full"
        else:
            run_type = "Crawl"

    # Principal from the report filename: …-<principal>-<type>-report.md
    # (reassessment reports end …-<principal>-<role>-reassessment.md).
    pm = re.search(r"-(lj|kj|vj|pk)-[a-z-]+-(?:report|reassessment)\.md$", report_path.name)
    principal = pm.group(1) if pm else ""

    # Date from H1
    date = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}", text)
    if m:
        date = m.group(1)

    # Decision counts — the section headers are authoritative for the
    # current format (wiki_store emits `## GO (31)` and lists each ad once
    # per section): counting `Action:` lines over the whole report would
    # double-count every GO ad that also appears under New Listings (the
    # 2026-09-13 email claimed 57 GOs; the report had 31).
    #
    # Read through the writer's own reader (`wiki_store.parse_section_counts`),
    # not a local regex: the local copy required the heading to end at the count,
    # so a heading carrying the report's annotation (`## DROP (692) — 53
    # ineligible hidden (…)`) lost its count — in *this* function the listing
    # parser below already tolerated the annotation, so the same email listed
    # every DROP advert while reporting no DROP total. A count the report does
    # not state stays absent (rendered as absent, never as 0).
    stated = parse_section_counts(text)
    if not stated:
        stated = parse_legacy_counts(text)
    decisions = {k: v for k, v in stated.items()
                 if k in ("GO", "MAYBE", "FLAG", "DROP")}
    # Newer reports expose the operational action per listing rather than an
    # aggregate GO/MAYBE/FLAG/DROP line. Derive the familiar summary only
    # when the aggregate is absent, so notification email remains useful
    # across both report formats.
    if not decisions:
        action_to_decision = {
            "APPLY_NOW": "GO",
            "MONITOR": "MAYBE",
            "WARM_INTRO_ONLY": "MAYBE",
            "USE_AS_GAP_SIGNAL": "FLAG",
            "WATCH": "FLAG",
            "DROP": "DROP",
        }
        for action in re.findall(r"\*\*Action\*\*:\s*([A-Z_]+)", text):
            decision = action_to_decision.get(action)
            if decision:
                decisions[decision] = decisions.get(decision, 0) + 1

    # New / Still open / Disappeared counts. The first three are report
    # sections, so they come from the same reader as the decisions; the old
    # unanchored `\b{k}[:\s]+(\d+)` search could read a sentence. A count the
    # report does not state stays absent rather than becoming 0.
    lifecycle = {}
    for key, label in (("new", "New"), ("still_open", "Still open"),
                       ("long_lasting", "Long-lasting")):
        if key in stated:
            lifecycle[label] = stated[key]

    # Total entries
    total = 0
    m = re.search(r"Total entries\*?: (\d+)", text)
    if m:
        total = int(m.group(1))

    # Top GOs — extract from the GO section only, deduped by (org, title).
    # Scanning the whole report picked up every New Listings block too, so
    # an ad listed in GO and again as "new" appeared twice in the email's
    # top picks (2026-09-13: Shawbrook ×2).
    def _section_listings(section_name: str) -> list[dict]:
        """Every scored listing in a section, best first, with its advert URL.

        The notification lists *all* GO adverts (LJ, 2026-09-21: "would prefer
        at least all the GO ads in the mail"), so the parser must hand over the
        whole section rather than a truncated top-N. The URL is carried so the
        reader can act on a GO without opening the report.
        """
        # The header may carry an annotation ("## DROP (156) — 35 ineligible
        # hidden (…)"), so match the count and ignore whatever follows it.
        m = re.search(rf"^## {section_name} \(\d+\)[^\n]*$", text, re.M)
        if not m:
            return []
        nxt = re.search(r"^## ", text[m.end():], re.M)
        section = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]
        found, seen = [], set()
        for block in re.split(r"### ", section):
            score_m = re.search(r"\*\*Score\*\*:\s*(\d+(?:\.\d+)?)/10", block)
            lines = block.strip().split("\n")
            header = re.sub(r"^\d+\.\s*", "", lines[0] if lines else "")
            if not score_m or " — " not in header:
                continue
            org, title = (part.strip() for part in header.split(" — ", 1))
            if (org, title) in seen:
                continue
            seen.add((org, title))
            url_m = re.search(r"- \*\*URL\*\*:\s*(https?://\S+)", block)
            # A listing with no advert URL still needs something to click, and the
            # report already carries the org page. Two defects this fixes (LJ,
            # 2026-10-01): the loose `(\S+)` captured the literal placeholder from
            # `- **URL**: _not available_`, so the email printed `_not` and offered no
            # link; and where the advert URL genuinely is missing, the reader had
            # nothing at all.
            org_m = re.search(r"- \*\*Org wiki\*\*:\s*\[[^\]]*\]\(<?([^)>\s]+)>?\)", block)
            found.append({
                "score": float(score_m.group(1)),
                "org": org,
                "title": title,
                "url": url_m.group(1) if url_m else (
                    f"{DASHBOARD_BASE_URL}{org_m.group(1)}" if org_m else ""
                ),
            })
        found.sort(key=lambda item: -item["score"])
        return found

    go_listings = _section_listings("GO")
    maybe_listings = _section_listings("MAYBE")
    drop_listings = _section_listings("DROP")

    # The header names the exact profile+role the run used ("Profile: **VJ-data-
    # statistics-pricing**"); the filename does not always carry the role.
    label_match = re.search(r"^Profile:\s*\**([A-Za-z0-9_.-]+)\**", text, re.M)
    profile_label = label_match.group(1) if label_match else (
        f"{principal}-general" if principal else "")

    return {
        "run_type": run_type,
        "principal": principal,
        "profile_label": profile_label,
        # Absolute: the notification prints this as the reader's disk path
        # (checklist item 9), and a relative path depends on the caller's cwd.
        "report_path": str(report_path.resolve()),
        "date": date,
        "decisions": decisions,
        "lifecycle": lifecycle,
        "total": total,
        "go_listings": go_listings,
        "maybe_listings": maybe_listings,
        "drop_listings": drop_listings,
        "report_name": report_path.name,
    }


def extract_log_summary(log_path: Path) -> str | None:
    """Extract the final human summary emitted by a full or incremental run.

    This intentionally carries the pipeline's own curated list, including
    URLs.  Reconstructing it from the markdown report loses decisions and can
    accidentally mix new, GO, and MAYBE listings.
    """
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    # `[^\n]*$` (not `.+$`): with DOTALL, `.` crosses newlines and `$` matches at
    # EOF, so `.+$` swallowed the whole rest of the log — every notification
    # carried "[CRAWL COMPLETE]", the run-record JSON and the notifier's own
    # lines after the results block.
    full_matches = list(re.finditer(
        r"^={60}\nLoveWork Results — .+?\n={60}\n(?P<body>.*?^Wiki: [^\n]*$)",
        text,
        re.MULTILINE | re.DOTALL,
    ))
    if full_matches:
        return full_matches[-1].group(0).strip()

    incremental_matches = list(re.finditer(
        r"^={70}\nLoveWork — Incremental Crawl — .+?\n={70}\n(?P<body>.*?^Report: [^\n]*$)",
        text,
        re.MULTILINE | re.DOTALL,
    ))
    if incremental_matches:
        return incremental_matches[-1].group(0).strip()
    return None


# What a notification may claim (LEARNINGS 2026-09-12: an email that claims a
# crawl which never happened is its own defect class). Each mode is a (subject
# tag, body claim) pair, so adding a mode cannot silently inherit "completed".
_MODE_LABELS = {
    "live": ("completed", "LoveWork {p} {rt} crawl completed."),
    "replay": (
        "archived re-process",
        "LoveWork {p} {rt} results re-processed from the local email archive "
        "(no new crawl).",
    ),
    "reassess": (
        "reassessment",
        "LoveWork {p}: registry replayed against retained advert evidence — "
        "no crawl, no fetch.",
    ),
    "consume": (
        "cross-principal ingest",
        "LoveWork {p}: LJ's sealed corpus ingested — no crawl of {p}'s own "
        "sources, no fetch.",
    ),
    "brief": (
        "watch brief",
        "LoveWork {p}: the Shoulder's brief — what changed since the last "
        "one. No crawl, no fetch; deterministic watches plus one summarising "
        "pass. Replies are read (W3).",
    ),
}


def _append_listings(lines: list[str], items: list[dict]) -> None:
    """One line per advert, its advert link indented under it.

    Same shape for every tier — a listing without its link reads as broken
    (LJ, 2026-09-21).
    """
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. [{item['score']}/10] {item['org']} — {item['title']}")
        if item.get("url"):
            lines.append(f"      {item['url']}")


def format_email(
    info: dict, log_summary: str | None = None, mode: str = "live"
) -> tuple[str, str]:
    """Format subject and body from parsed report info.

    Notification feedback (2026-09-10): name the principal instead of
    "Unknown", put the dashboard link in the subject, give the report both a
    web and a disk path, and demote the Hermes profile line to the footer.

    `mode` says what actually produced the report: `live` a crawl, `replay` a
    re-process of the locally archived emails, `reassess` a registry replay
    against retained evidence. Only `live` may claim a crawl.
    """
    tag, claim = _MODE_LABELS.get(mode, _MODE_LABELS["live"])
    rt = info["run_type"]
    dt = info["date"] or "unknown date"
    principal = info.get("principal", "")
    p_label = principal.upper() if principal else "LoveWork"
    report_name = info["report_name"]
    report_url = (
        f"{DASHBOARD_BASE_URL}/principals/{principal}/wiki/reports/{report_name}"
        if principal else ""
    )
    subject = f"LoveWork {p_label} — {dt} — {tag}"
    if report_url:
        subject += f" — {report_url}"

    lines = [claim.format(p=p_label, rt=rt.lower())]
    if principal:
        lines.append(f"Principal: {p_label} ({principal})")
        # The wiki is the state of the world and stays interesting all week;
        # the report below is a point-in-time log that stales within days.
        # So the wiki is the first link of every crawl email (LJ, 2026-10-06).
        lines.append(f"Wiki (state of the world): "
                     f"{DASHBOARD_BASE_URL}/principals/{principal}/wiki")
    context = info.get("context") or {}
    if context.get("gmail_label"):
        lines.append(f"Lead mailbox: Gmail label {context['gmail_label']}")
    lines.append(f"Date: {dt}")
    lines.append(f"Report: {report_name}")
    if report_url:
        lines.append(f"  Web:  {report_url}")
    if info.get("report_path"):
        lines.append(f"  Disk: {info['report_path']}")

    # What the matcher scored against — so "why didn't this advert rank higher?"
    # is one file-open away, and the reader is reminded the verdict is a profile
    # judgement, not a property of the advert (LJ, 2026-09-23).
    if principal:
        from report_header import profile_reference_lines

        lines.append("")
        lines.extend(profile_reference_lines(
            info.get("profile_label") or f"{principal}-general"))
    lines.append("")

    if log_summary:
        lines.append(log_summary)
    elif info["decisions"]:
        # Only the counts the report actually stated. `GO` used to default to
        # 0 while the others were omitted, so an unparsed report claimed "GO: 0"
        # — a claim the report never made (docs/27 §2b: not-found is not zero).
        dec = info["decisions"]
        parts = [f"{k}: {dec[k]}" for k in ("GO", "MAYBE", "FLAG", "DROP") if k in dec]
        lines.append("Decisions: " + (" · ".join(parts) if parts
                                      else "not stated in this report"))

    if not log_summary and info["lifecycle"]:
        lc = info["lifecycle"]
        lc_parts = []
        for k in ("New", "Still open", "Disappeared"):
            if k in lc:
                lc_parts.append(f"{k}: {lc[k]}")
        if lc_parts:
            lines.append("Lifecycle: " + " · ".join(lc_parts))

    if not log_summary and info["total"]:
        lines.append(f"Total entries: {info['total']}")

    # What the mail lists (LJ, 2026-09-23 — the empty-list and flapping rules):
    #   1. every GO advert, always, with its link;
    #   2. if fewer than EMAIL_MIN_LISTINGS are shown, the whole MAYBE section —
    #      so an advert oscillating between GO and MAYBE from run to run does not
    #      appear and disappear, and a thin week is not a blank mail;
    #   3. if still fewer, the top EMAIL_MIN_LISTINGS of DROP as a floor;
    #   4. the rest of DROP is skipped.
    # This runs for *both* kinds of mail: a crawl's log summary carries the
    # pipeline's own curated list of the newest few, and the complete set
    # follows it. If the body must be cut for the mail argv limit,
    # _fit_body_for_argv drops the tail — the order below decides what survives.
    lines.append("")
    go = info.get("go_listings") or []
    maybe = info.get("maybe_listings") or []
    drop = info.get("drop_listings") or []

    lines.append(f"★ GO ({len(go)})")
    if go:
        _append_listings(lines, go)
    else:
        lines.append("  (none)")

    shown = len(go)
    if shown < EMAIL_MIN_LISTINGS:
        lines.append("")
        lines.append(f"MAYBE ({len(maybe)})" if maybe else "MAYBE (none)")
        _append_listings(lines, maybe)
        shown += len(maybe)

    if shown < EMAIL_MIN_LISTINGS:
        top_drop = drop[:EMAIL_MIN_LISTINGS]
        if top_drop:
            lines.append("")
            lines.append(f"DROP — top {len(top_drop)} of {len(drop)} "
                         f"(listed only because the tiers above are thin)")
            _append_listings(lines, top_drop)

    lines.append("")
    # Hermes profile is operator plumbing, not reader information — footer.
    lines.append(f"— LoveWork bot on {socket.gethostname()}")
    lines.append(f"({identity_line()})")

    body = "\n".join(lines)
    return subject, body


def send_email_result(
    subject: str, body: str, to_email: str = TO_EMAIL, html: bool = False
) -> dict[str, str | bool]:
    """Send through the active Hermes Gmail profile and return delivery proof.

    Local Postfix only proves acceptance by a local queue; it cannot prove that
    Gmail accepted delivery and is known to be rejected from this host.  A
    successful Gmail API response includes the sent message id, which becomes
    the durable notification evidence for the run watchdog.
    """
    # Cap the body before building argv: google_api.py takes --body as a CLI
    # argument and the kernel kills a single arg over MAX_ARG_STRLEN (128 KiB)
    # with E2BIG before the child starts (2026-09-12 observation).
    capped = _fit_body_for_argv(body)
    if capped != body:
        logger.warning(
            "Notification body truncated from %d to %d bytes to fit the mail argv limit",
            len(body.encode("utf-8")),
            len(capped.encode("utf-8")),
        )
    body = capped
    gapi = _find_gapi()
    if not gapi.exists():
        return {"ok": False, "error": f"Gmail helper not found: {gapi}"}
    cmd = [
        sys.executable,
        str(gapi),
        "gmail",
        "send",
        "--to",
        to_email,
        "--subject",
        subject,
        "--body",
        body,
    ]
    if html:
        cmd.append("--html")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=_gapi_env(),
        )
    except Exception as exc:
        return {"ok": False, "error": f"Gmail API error: {exc}"}
    if result.returncode != 0:
        return {"ok": False, "error": classify_notification_error(result.stderr or "")}
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Gmail API returned no parseable delivery receipt"}
    message_id = str(response.get("id") or "").strip()
    if response.get("status") != "sent" or not message_id:
        return {"ok": False, "error": "Gmail API did not return a sent message id"}
    logger.info("Email sent via Gmail API: %s", subject)
    return {"ok": True, "provider": "gmail_api", "message_id": message_id}


def send_email(subject: str, body: str) -> bool:
    """Compatibility wrapper for callers that only need success/failure."""
    return bool(send_email_result(subject, body).get("ok"))


def _seal_delivery_receipt(args, principal: str, facts: dict,
                           state_root: Path | None = None) -> None:
    """Seal the delivery as a CHILD operation of the run it belongs to.

    The receipt arrives after the run's stratum is sealed, so it cannot live in
    the parent bundle (docs/28 §WP1b box ⑥). Best-effort: a receipt that cannot
    be sealed is logged, never allowed to turn a delivered email into a failure —
    and never allowed to turn a failure into a success. The run ledger keeps the
    watchdog-visible record it already had.
    """
    if not args.run_id:
        return
    owner = principal or ""
    if not owner:
        # `full-<principal>-<ts>-<pid>` — the wrapper's run id names it.
        parts = args.run_id.split("-")
        owner = parts[1] if len(parts) > 1 else ""
    if owner not in ("lj", "vj", "kj", "pk"):
        logger.info("No principal for %s; delivery receipt not sealed", args.run_id)
        return
    try:
        from receipt_operation import seal_receipt

        bundle = seal_receipt(principal=owner, parent_run_id=args.run_id,
                              kind="email", facts=facts,
                              state_root=state_root)
        if bundle is None:
            logger.warning("Delivery receipt for %s was not sealed (see above)", args.run_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Delivery receipt sealing failed: %s", e)


def main():
    # Only the CLI entry point configures logging (see the note at the top:
    # importing this module must have no logging side effects).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Send LoveWork success notification email")
    ap.add_argument("--report", "-r", help="Path to the report file")
    ap.add_argument("--log", type=Path, help="Crawl log containing the final results summary")
    ap.add_argument("--run-id", help="Run ledger record to update with Gmail delivery evidence")
    ap.add_argument("--runs-dir", type=Path, help="Principal-owned run-ledger directory")
    ap.add_argument(
        "--to",
        help="Override the recipient (default: the principal's own inbox) — for "
             "operator review or a re-send after a delivery failure",
    )
    ap.add_argument(
        "--mode",
        choices=["live", "replay", "reassess", "consume"],
        default="live",
        help="What produced the report, so the email cannot claim a crawl that "
             "never happened: 'live' a crawl, 'replay' a re-process of the "
             "archived emails, 'reassess' a registry replay against retained "
             "evidence",
    )
    ap.add_argument(
        "--check-token",
        action="store_true",
        help="Read-only Gmail OAuth token pre-flight check; exits 0 when sendable",
    )
    ap.add_argument(
        "--record-only",
        action="store_true",
        help="Do not email: record the run's notification as 'suppressed' with a "
             "reason and exit 0 (post-crawl emails retired 2026-10-18)",
    )
    args = ap.parse_args()

    if args.check_token:
        result = check_token()
        if result["ok"]:
            print(f"TOKEN_OK: {result['detail']}")
            return
        status = str(result["status"]).upper()
        print(f"TOKEN_{status}: {result['detail']}", file=sys.stderr)
        sys.exit(2)

    if not args.report:
        ap.error("--report is required unless --check-token is used")

    # Delivery evidence is written into the run's record (the watchdog reads it).
    ledger_kwargs = {"runs_dir": args.runs_dir} if args.runs_dir else {}

    report_path = Path(args.report)
    if not report_path.exists():
        logger.error(f"Report not found: {report_path}")
        sys.exit(1)

    info = parse_report(report_path)
    principal = str(info.get("principal", ""))

    # Crawl context written by run_pipeline (lead mailbox etc.).
    ctx_path = report_path.parent.parent.parent / "cache" / "last_run_context.json"
    if ctx_path.exists():
        try:
            info["context"] = json.loads(ctx_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read crawl context %s: %s", ctx_path, exc)

    if args.record_only:
        reason = (
            "post-crawl emails retired 2026-10-18; the Monday state email "
            "(state_email.py) carries the principal's state, not the delta"
        )
        if args.run_id:
            record_notification(
                args.run_id,
                status="suppressed",
                provider="none",
                reason=reason,
                **ledger_kwargs,
            )
        logger.info("No email sent (--record-only): %s", reason)
        print(f"[notify] suppressed for {info['run_type']} run on {info['date']} — {reason}")
        return

    log_summary = extract_log_summary(args.log) if args.log and args.log.exists() else None
    if args.log and not log_summary:
        logger.warning("No final result summary found in crawl log: %s", args.log)
    subject, body = format_email(info, log_summary, mode=args.mode)
    to_email = recipient_for(principal, args.to)
    logger.info("Recipient: %s (principal: %s)", to_email, principal or "unknown")

    logger.info(f"Sending email for {info['run_type']} run on {info['date']}")
    go = info.get("go_listings") or []
    # `unknown`, not 0: the report may not state a GO count at all, and the log
    # is evidence a later reader will believe (docs/27 §2b).
    logger.info(f"GOs: {info['decisions'].get('GO', 'unknown')}, "
                f"Top: {go[0]['score'] if go else 'none'}")

    result = send_email_result(subject, render_html(body), to_email=to_email, html=True)
    if result.get("ok"):
        if args.run_id:
            record_notification(
                args.run_id,
                status="sent",
                provider=str(result["provider"]),
                message_id=str(result["message_id"]),
                **ledger_kwargs,
            )
        logger.info("Notification sent successfully: Gmail message %s", result["message_id"])
        _seal_delivery_receipt(args, principal, {
            "status": "sent",
            "provider": str(result["provider"]),
            "message_id": str(result["message_id"]),
            "to": to_email,
        })
        return

    error = str(result.get("error") or "unknown Gmail notification failure")
    if args.run_id:
        record_notification(
            args.run_id,
            status="failed",
            provider="gmail_api",
            error=error,
            **ledger_kwargs,
        )
    _seal_delivery_receipt(args, principal, {
        "status": "failed",
        "provider": "gmail_api",
        "error": error,
        "to": to_email,
    })
    logger.error("Email notification failed: %s", error)
    sys.exit(1)


if __name__ == "__main__":
    main()

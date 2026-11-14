#!/usr/bin/env python3
"""Monday state email — the wiki index, as it is served, per principal.

LJ, 2026-10-18: *"The emails we send after a crawl — stop sending them. No
longer relevant. We don't care about a crawl. We care about a state. So after we
have crawls Sat/Sun, we should send an email with the CURRENT STATE — not only
what's new, the delta, but the current state. Just take the .md file we all have
for that that we serve on the wiki. And let us send it over on Monday
afternoon/evening. We should send e.g. the entire .md file in email."*

So: `state/<principal>/wiki/index.md` — the file the dashboard serves at
`/principals/<p>/wiki/` — rendered with the *dashboard's own* markdown pipeline
(so the email and the served page agree), with every relative link rewritten to
the dashboard URL, and mailed to the principal's recipient list.

Design notes:

* The body is the whole index (counts, GO/MAYBE/FLAG/DROP listings, agencies,
  recents) — not a delta. Nothing here reads the run ledger; a state email is
  about the state, and it must be sendable even when a sweep failed.
* HTML first (the index is markdown with long tables and links): links stay
  clickable and readable via the same `human_slug` the crawl email used. If the
  rendered HTML would exceed the argv limit, the raw markdown is sent instead,
  and if even that is too large it is fitted like `notify.py` does — with the
  live dashboard link at the top, so the reader can always reach the whole file.
* Delivery is receipted in `state/<p>/cache/state-email-<date>.json`: the Gmail
  message id is the durable evidence that the principal was told.

CLI:  ../venv/bin/python3 state_email.py --profile lj|vj|kj|all
                              [--dry-run] [--to <address>] [--out <dir>]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import notify  # noqa: E402
from run_ledger import utc_now  # noqa: E402
from principal_runtime import resolve_principal_runtime  # noqa: E402

logger = logging.getLogger("state_email")

def principals_with_state() -> tuple:
    """The principals with state, in **LJ's priority order** — one authority.

    This constant used to be `("lj", "vj", "kj")`, a fourth copy of the pre-2026-10-19
    order. The email's drain loop reads it, so on the first Monday under the new rule
    the job would have spent its 120 minutes on lj first — the principal LJ had just
    moved to *third* — and reached vj and kj with whatever was left.
    """
    return tuple(config.principal_names(config.STATE_DIR))

#: The file the dashboard serves as `/principals/<p>/wiki/`.
INDEX_NAME = "index.md"


def served_url(principal: str) -> str:
    """The live page this email mirrors."""
    base = notify.DASHBOARD_BASE_URL.rstrip("/")
    return f"{base}/principals/{principal}/wiki/"


def render_body(markdown_text: str, principal: str) -> tuple[str, bool]:
    """Render the index for email. Returns `(body, is_html)`.

    The HTML path mirrors `doc_serve._render_markdown_page` minus its page
    chrome: render markdown → rewrite relative `.md` links to served paths →
    turn bare URLs into readable-slug anchors → make every root-relative link
    absolute against the dashboard base. Falls back to the raw markdown when
    the HTML would not fit the mail transport's argv limit (`notify._fit_body_for_argv`).
    """
    import doc_serve

    request_path = f"/principals/{principal}/wiki/{INDEX_NAME}"
    html = doc_serve.render_markdown(markdown_text)
    html = doc_serve.rewrite_md_links(html, request_path)
    html = doc_serve.auto_link_urls(html)

    # Root-relative links (`/principals/...`) only work in a browser pointed at
    # the dashboard; in Gmail they must be absolute.
    base = notify.DASHBOARD_BASE_URL.rstrip("/")
    html = html.replace('href="/', f'href="{base}/')

    head = (
        f'<p style="margin:0 0 14px 0;color:#555">LoveWork &mdash; '
        f'<strong>{principal}</strong> state, {date.today().isoformat()}. '
        f'This is the whole index, not a delta; the live page is '
        f'<a href="{served_url(principal)}">{served_url(principal)}</a>.</p>'
    )
    body = f'<div style="{notify._HTML_BODY_STYLE}">{head}{html}</div>'
    if len(body.encode("utf-8")) <= notify.MAX_ARGV_BODY_BYTES:
        return body, True
    logger.warning(
        "State email HTML for %s is %d bytes (cap %d) — sending the markdown instead",
        principal, len(body.encode("utf-8")), notify.MAX_ARGV_BODY_BYTES,
    )
    plain = (
        f"LoveWork — {principal} state, {date.today().isoformat()} (whole index, "
        f"not a delta). Live page: {served_url(principal)}\n\n{markdown_text}"
    )
    return notify._fit_body_for_argv(plain), False


def send_state_email(
    principal: str,
    *,
    dry_run: bool = False,
    to_override: str | None = None,
    out_dir: Path | None = None,
    gap_lines: list[dict] | None = None,
) -> dict:
    """Send one principal's state email; return a small, honest result dict."""
    runtime = resolve_principal_runtime(principal)
    index_path = runtime.wiki_root / INDEX_NAME
    if not index_path.is_file():
        return {"principal": principal, "ok": False,
                "error": f"no wiki index at {index_path}"}
    markdown_text = index_path.read_text(encoding="utf-8")
    if gap_lines:
        # Prepended, not appended: a reader must meet the exception before the rows
        # it qualifies. Rendered through the same Markdown path as the index.
        markdown_text = gap_section(principal, gap_lines) + markdown_text
    body, is_html = render_body(markdown_text, principal)
    subject = f"LoveWork state — {principal} — {date.today().isoformat()}"
    to_email = notify.recipient_for(principal, to_override)

    result: dict = {
        "principal": principal,
        "ok": True,
        "dry_run": bool(dry_run),
        "to": to_email,
        "subject": subject,
        "source": str(index_path),
        "source_bytes": len(markdown_text.encode("utf-8")),
        "body_bytes": len(body.encode("utf-8")),
        "html": is_html,
        "url": served_url(principal),
    }

    if dry_run:
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"state-email-{principal}{'.html' if is_html else '.txt'}"
            target.write_text(body, encoding="utf-8")
            result["written"] = str(target)
        return result

    sent = notify.send_email_result(subject, body, to_email=to_email, html=is_html)
    result["ok"] = bool(sent.get("ok"))
    if result["ok"]:
        result["message_id"] = str(sent["message_id"])
        result["provider"] = str(sent["provider"])
        receipt = runtime.cache_dir / f"state-email-{date.today().isoformat()}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps({**result, "sent_at": utc_now()}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["receipt"] = str(receipt)
        logger.info("State email sent for %s: Gmail message %s", principal, result["message_id"])
    else:
        result["error"] = str(sent.get("error") or "unknown Gmail failure")
        logger.error("State email failed for %s: %s", principal, result["error"])
    return result


#: Exit code when the Monday invariant does not hold: the email goes out (a
#: principal who is owed their state must get it) but the run is *not* clean, so
#: the job's own record and the watchdog can see that a rule was broken.
GAP_EXIT = 3


def go_pack_gap(principal: str) -> list[dict]:
    """The Monday invariant, read through the pack authority (`application_packs`)."""
    import application_packs

    return application_packs.go_pack_gap(principal)


def gap_section(principal: str, rows: list[dict]) -> str:
    """The honest preface: what the email's GO rows cannot yet be applied to.

    LJ, 2026-10-19: *"all GO-s should have application packs before the Monday 19:00
    wiki state email."* When that does not hold, the email says so **at the top**
    rather than presenting GO rows as ready work — a state email that lists a GO
    whose pack has no cover letter is the lying-artefact class this project keeps
    finding.
    """
    lines = [f"## ⚠ {len(rows)} GO(s) in this state have no prepared pack", "",
             "The rule is that every GO carries material the principal can apply "
             "with. These do not yet — the loop is working them, and any that no "
             "longer has an advert to work from moves to the lead pile.", ""]
    for row in rows:
        flag = "" if row["has_advert"] else "  — **no advert left to prepare from**"
        lines.append(f"- **{row['org']}** — {row['role']} — {row['why']}{flag}")
    lines += ["", "---", ""]
    return "\n".join(lines)


def drain(principal: str, minutes: int, *, dry_run: bool = False) -> dict:
    """Give the loop its last chance before the state goes out (bounded, honest).

    One bounded tick per principal, with the writer lease respected like any other
    caller: a crawl or another tick holding it means this returns deferred and the
    gap is reported instead of being papered over.
    """
    import subprocess

    if dry_run or minutes <= 0:
        return {"principal": principal, "skipped": True}
    env = dict(os.environ)
    env.setdefault("LOVEWORK_LOOP_DAILY_CALLS", "1200")
    env.setdefault("LOVEWORK_LOOP_DAILY_MINUTES", "1200")
    argv = [sys.executable, "tick.py", "--profile", principal, "--mode", "act",
            "--apply", "--run-minutes", str(minutes), "--max-actions", "0"]
    try:
        done = subprocess.run(argv, cwd=str(Path(__file__).resolve().parent),
                              env=env, capture_output=True, text=True,
                              timeout=minutes * 60 + 120)
    except subprocess.TimeoutExpired:
        return {"principal": principal, "timeout": True}
    return {"principal": principal, "returncode": done.returncode,
            "tail": (done.stdout or done.stderr or "").strip().splitlines()[-3:]}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", "--principal", dest="profile", default="all",
                    help="lj, vj, kj, or all (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="render and report, write the body under --out when given, send nothing")
    ap.add_argument("--to", help="Override the recipient (operator review, or a re-send)")
    ap.add_argument("--out", type=Path, help="Directory for --dry-run bodies")
    ap.add_argument("--drain-minutes", type=int, default=0,
                    help="give the loop this many minutes per principal to prepare "
                         "outstanding GO packs before sending (the Monday rule); "
                         "0 = do not drain, only report the gap")
    args = ap.parse_args()

    principals = principals_with_state() if args.profile == "all" else (args.profile,)
    for principal in principals:
        if principal not in config.PRINCIPAL_ORDER:
            print(f"[state-email] unknown principal: {principal}", file=sys.stderr)
            return 2
    # **The Monday rule, enforced where it is read** (LJ, 2026-10-19): every GO must
    # have a pack the principal can apply with before this email. Drain first (the
    # loop's own action), then measure again and say plainly what is left.
    # **The drain shares one budget, because the job has one clock** (2026-10-19).
    # `--drain-minutes 120` per principal is 360 minutes for three of them, and the
    # scheduler kills this script at 3600 s — so a night with real work to do would
    # have died before sending anything. Each principal now gets the smaller of its
    # share and what is left, and the last one cannot start with minutes it does not
    # have. (Tonight it did not bite only because the gaps were nearly empty.)
    total_budget = max(0, args.drain_minutes) * 60
    spent = 0.0
    started = time.monotonic()
    gaps: dict[str, list[dict]] = {}
    for index, principal in enumerate(principals):
        minutes = 0
        if args.drain_minutes:
            left = total_budget - (time.monotonic() - started)
            share = total_budget / max(1, len(principals))
            minutes = int(max(0, min(args.drain_minutes, share, left)) // 60)
            if minutes < 1:
                print(f"[state-email] {principal}: no drain budget left — reporting "
                      f"the gap as found", file=sys.stderr)
        if minutes:
            drained = drain(principal, minutes, dry_run=args.dry_run)
            if not drained.get("skipped"):
                print(f"[state-email] {principal}: drained with tick.py "
                      f"({drained.get('returncode', drained)})", file=sys.stderr)
        gaps[principal] = go_pack_gap(principal)
        if gaps[principal]:
            print(f"[state-email] {principal}: {len(gaps[principal])} GO(s) without a "
                  f"prepared pack — the email will name them", file=sys.stderr)

    results = []
    for principal in principals:
        row = send_state_email(principal, dry_run=args.dry_run, to_override=args.to,
                               out_dir=args.out, gap_lines=gaps[principal])
        row["gap"] = len(gaps[principal])
        results.append(row)

    failed = 0
    for r in results:
        if r.get("ok"):
            if r.get("dry_run"):
                print(f"[state-email] {r['principal']}: dry-run "
                      f"{r['source_bytes']} B source -> {r['body_bytes']} B body "
                      f"({'html' if r['html'] else 'text'}) to {r['to']}"
                      + (f"; wrote {r['written']}" if r.get("written") else ""))
            else:
                print(f"[state-email] {r['principal']}: sent to {r['to']} "
                      f"(message {r['message_id']}, {r['body_bytes']} B "
                      f"{'html' if r['html'] else 'text'} from {r['source']})")
        else:
            failed += 1
            print(f"[state-email] {r['principal']}: FAILED — {r.get('error')}", file=sys.stderr)
    if failed:
        return 1
    with_gap = [r for r in results if r.get("gap")]
    if with_gap:
        # Sent, deliberately — and not clean. The distinction matters: exit 1 means
        # nobody was told, exit 3 means the principal was told and the rule broke.
        return GAP_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

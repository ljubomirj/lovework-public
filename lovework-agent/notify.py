#!/usr/bin/env python3
"""
LoveWork success notification — sends an email summary after a completed run.

Called from the cron worker after the crawl succeeds:
    python3 notify.py --report <path> --log <path>

Sends the worker's final results summary via Gmail.  The final log block is
authoritative: it contains the decisions, lifecycle counts, and curated
new/GO/MAYBE listings calculated by the pipeline itself.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from hermes_context import resolve_hermes_home, identity_line
from run_ledger import record_notification

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Resolve Hermes google_api.py (same logic as gmail_accessor.py).
_HERMES_HOME = resolve_hermes_home()
_GAPI = _HERMES_HOME / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"

TO_EMAIL = "LjubomirJosifovski@gmail.com"


def _find_gapi() -> Path:
    """Find the Hermes google_api.py script."""
    if _GAPI.exists():
        return _GAPI
    return _GAPI  # return path anyway, caller will handle error


def _gapi_env() -> dict[str, str]:
    """Run standalone Google scripts against LoveWork's active Hermes profile."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(_HERMES_HOME)
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


def check_token(hermes_home: Path = _HERMES_HOME) -> dict[str, str | bool]:
    """Classify Gmail OAuth token health without mutating the token file.

    Returns ``ok`` plus a ``status`` and human-readable ``detail``.  Used as
    a pre-flight check in the crawl wrapper so a revoked token is detected
    at crawl start instead of as a post-crawl notification failure.
    """
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

    # Run type from H1
    run_type = "Unknown"
    m = re.search(r"^# (INCREMENTAL|FULL SWEEP)", text, re.MULTILINE)
    if m:
        run_type = m.group(1).title()

    # Date from H1
    date = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}", text)
    if m:
        date = m.group(1)

    # Decision counts
    decisions = {}
    for k in ("GO", "MAYBE", "FLAG", "DROP"):
        m = re.search(rf"\b{k}:\s*(\d+)", text)
        if m:
            decisions[k] = int(m.group(1))

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

    # New / Still open / Disappeared counts
    lifecycle = {}
    for k in ("New", "Still open", "Long-lasting", "Disappeared"):
        m = re.search(rf"\b{k}[:\s]+(\d+)", text)
        if m:
            lifecycle[k] = int(m.group(1))

    # Total entries
    total = 0
    m = re.search(r"Total entries\*?: (\d+)", text)
    if m:
        total = int(m.group(1))

    # Top GOs — extract from ### Org — Title with Score
    top_gos = []
    for block in re.split(r"### ", text):
        score_m = re.search(r"\*\*Score\*\*:\s*(\d+(?:\.\d+)?)/10", block)
        if not score_m:
            continue
        score = float(score_m.group(1))
        # Check if in GO section (by looking for context before this block)
        # Simple approach: find Org — Title in first line
        lines = block.strip().split("\n")
        header = lines[0] if lines else ""
        if " — " not in header:
            continue
        parts = header.split(" — ", 1)
        org, title = parts[0].strip(), parts[1].strip()
        top_gos.append((score, org, title))

    top_gos.sort(key=lambda x: -x[0])

    return {
        "run_type": run_type,
        "date": date,
        "decisions": decisions,
        "lifecycle": lifecycle,
        "total": total,
        "top_gos": top_gos[:5],
        "report_name": report_path.name,
    }


def extract_log_summary(log_path: Path) -> str | None:
    """Extract the final human summary emitted by a full or incremental run.

    This intentionally carries the pipeline's own curated list, including
    URLs.  Reconstructing it from the markdown report loses decisions and can
    accidentally mix new, GO, and MAYBE listings.
    """
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    full_matches = list(re.finditer(
        r"^={60}\nLoveWork Results — .+?\n={60}\n(?P<body>.*?^Wiki: .+$)",
        text,
        re.MULTILINE | re.DOTALL,
    ))
    if full_matches:
        return full_matches[-1].group(0).strip()

    incremental_matches = list(re.finditer(
        r"^={70}\nLoveWork — Incremental Crawl — .+?\n={70}\n(?P<body>.*?^Report: .+$)",
        text,
        re.MULTILINE | re.DOTALL,
    ))
    if incremental_matches:
        return incremental_matches[-1].group(0).strip()
    return None


def format_email(info: dict, log_summary: str | None = None) -> tuple[str, str]:
    """Format subject and body from parsed report info."""
    rt = info["run_type"]
    dt = info["date"] or "unknown date"
    subject = f"LoveWork {rt} — {dt} — completed"

    lines = [f"LoveWork {rt} completed.", identity_line()]
    lines.append(f"Date: {dt}")
    lines.append(f"Report: {info['report_name']}")
    lines.append("")

    if log_summary:
        lines.append(log_summary)
    elif info["decisions"]:
        dec = info["decisions"]
        parts = [f"GO: {dec.get('GO', 0)}"]
        if "MAYBE" in dec: parts.append(f"MAYBE: {dec['MAYBE']}")
        if "FLAG" in dec: parts.append(f"FLAG: {dec['FLAG']}")
        if "DROP" in dec: parts.append(f"DROP: {dec['DROP']}")
        lines.append("Decisions: " + " · ".join(parts))

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

    if not log_summary:
        lines.append("")
        lines.append("Top picks:")

        if info["top_gos"]:
            for i, (score, org, title) in enumerate(info["top_gos"], 1):
                lines.append(f"  {i}. [{score}/10] {org} — {title}")
        else:
            lines.append("  (none)")

    lines.append("")
    lines.append("— LoveWork bot")

    body = "\n".join(lines)
    return subject, body


def send_email_result(subject: str, body: str) -> dict[str, str | bool]:
    """Send through the active Hermes Gmail profile and return delivery proof.

    Local Postfix only proves acceptance by a local queue; it cannot prove that
    Gmail accepted delivery and is known to be rejected from this host.  A
    successful Gmail API response includes the sent message id, which becomes
    the durable notification evidence for the run watchdog.
    """
    gapi = _find_gapi()
    if not gapi.exists():
        return {"ok": False, "error": f"Gmail helper not found: {gapi}"}
    cmd = [
        sys.executable,
        str(gapi),
        "gmail",
        "send",
        "--to",
        TO_EMAIL,
        "--subject",
        subject,
        "--body",
        body,
    ]
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


def main():
    ap = argparse.ArgumentParser(description="Send LoveWork success notification email")
    ap.add_argument("--report", "-r", help="Path to the report file")
    ap.add_argument("--log", type=Path, help="Crawl log containing the final results summary")
    ap.add_argument("--run-id", help="Run ledger record to update with Gmail delivery evidence")
    ap.add_argument("--runs-dir", type=Path, help="Principal-owned run-ledger directory")
    ap.add_argument(
        "--check-token",
        action="store_true",
        help="Read-only Gmail OAuth token pre-flight check; exits 0 when sendable",
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

    report_path = Path(args.report)
    if not report_path.exists():
        logger.error(f"Report not found: {report_path}")
        sys.exit(1)

    info = parse_report(report_path)
    log_summary = extract_log_summary(args.log) if args.log and args.log.exists() else None
    if args.log and not log_summary:
        logger.warning("No final result summary found in crawl log: %s", args.log)
    subject, body = format_email(info, log_summary)

    logger.info(f"Sending email for {info['run_type']} run on {info['date']}")
    logger.info(f"GOs: {info['decisions'].get('GO', 0)}, Top: {info['top_gos'][0][0] if info['top_gos'] else 'none'}")

    result = send_email_result(subject, body)
    ledger_kwargs = {"runs_dir": args.runs_dir} if args.runs_dir is not None else {}
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
    logger.error("Email notification failed: %s", error)
    sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
LoveWork success notification — sends an email summary after a completed run.

Called from cron shell scripts after the crawl succeeds:
    incremental_crawl.py && python3 notify.py --report <path>

Sends a brief executive summary via Gmail: run type, date, quantities, top 3-5 GOs.
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from hermes_context import resolve_hermes_home, identity_line

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


def format_email(info: dict) -> tuple[str, str]:
    """Format subject and body from parsed report info."""
    rt = info["run_type"]
    dt = info["date"] or "unknown date"
    subject = f"LoveWork {rt} — {dt} — completed"

    lines = [f"LoveWork {rt} completed.", identity_line()]
    lines.append(f"Date: {dt}")
    lines.append(f"Report: {info['report_name']}")
    lines.append("")

    if info["decisions"]:
        dec = info["decisions"]
        parts = [f"GO: {dec.get('GO', 0)}"]
        if "MAYBE" in dec: parts.append(f"MAYBE: {dec['MAYBE']}")
        if "FLAG" in dec: parts.append(f"FLAG: {dec['FLAG']}")
        if "DROP" in dec: parts.append(f"DROP: {dec['DROP']}")
        lines.append("Decisions: " + " · ".join(parts))

    if info["lifecycle"]:
        lc = info["lifecycle"]
        lc_parts = []
        for k in ("New", "Still open", "Disappeared"):
            if k in lc:
                lc_parts.append(f"{k}: {lc[k]}")
        if lc_parts:
            lines.append("Lifecycle: " + " · ".join(lc_parts))

    if info["total"]:
        lines.append(f"Total entries: {info['total']}")

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


def send_email(subject: str, body: str) -> bool:
    """Send email via system `mail` command.

    Falls back to Hermes google_api.py if `mail` is unavailable.
    """
    # Try system `mail` first (simple, no OAuth needed)
    mail_cmd = shutil.which("mail")
    if mail_cmd:
        try:
            proc = subprocess.Popen(
                [mail_cmd, "-s", subject, TO_EMAIL],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            stdout, stderr = proc.communicate(input=body, timeout=30)
            if proc.returncode == 0:
                logger.info(f"Email sent via mail: {subject}")
                return True
            logger.warning(f"mail returned {proc.returncode}: {stderr.strip()}")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.warning("mail timed out")
        except Exception as e:
            logger.warning(f"mail failed: {e}")

    # Fallback: Hermes google_api.py
    gapi = _find_gapi()
    if gapi.exists():
        cmd = [
            sys.executable, str(gapi),
            "gmail", "send",
            "--to", TO_EMAIL,
            "--subject", subject,
            "--body", body,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info(f"Email sent via Gmail API: {subject}")
                return True
            logger.warning(f"Gmail API failed: {result.stderr.strip()[:200]}")
        except Exception as e:
            logger.warning(f"Gmail API error: {e}")

    logger.error("All email methods failed")
    return False


def main():
    ap = argparse.ArgumentParser(description="Send LoveWork success notification email")
    ap.add_argument("--report", "-r", required=True, help="Path to the report file")
    args = ap.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        logger.error(f"Report not found: {report_path}")
        sys.exit(1)

    info = parse_report(report_path)
    subject, body = format_email(info)

    logger.info(f"Sending email for {info['run_type']} run on {info['date']}")
    logger.info(f"GOs: {info['decisions'].get('GO', 0)}, Top: {info['top_gos'][0][0] if info['top_gos'] else 'none'}")

    if send_email(subject, body):
        logger.info("Notification sent successfully")
    else:
        logger.warning("Email notification failed (non-fatal)")


if __name__ == "__main__":
    main()

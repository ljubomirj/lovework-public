"""
Shared Gmail accessor for LoveWork.

`google_api.py` (from the Hermes google-workspace skill) is the single Gmail entrypoint.
It uses the `gws` binary if present (then any Python works) or falls back to the
`googleapiclient` library (then it must run under a Python that has the Google client
libs — on macOS that's typically /usr/local/bin/python3, NOT the project venv python).

This module resolves the correct interpreter once (cached, env-overridable) and exposes
`run_gapi()` for both `sources/gmail_lj_jobs.py` and `history.scan_gmail`, so they share
one robust path to Gmail.

Gmail is optional: if `google_api.py` is missing or not OAuth-authenticated, `run_gapi`
returns None and callers gracefully no-op. An empty result set returns `[]` — callers
must not treat "no mail" and "no Gmail" as the same thing.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_context import hermes_home_or_none

logger = logging.getLogger(__name__)

# google_api.py prints this for a successful but empty result set. It is plain
# text, not JSON, so it must be mapped before the JSON decode: `[]` means "the
# mailbox/search is genuinely empty", `None` means "Gmail could not be reached".
EMPTY_RESULT_SENTINEL = "no messages found."

# google_api.py is VENDORED into the repo (2026-10-20, decoupling plan A7):
# `lovework-agent/google_api.py`, its one Hermes dependency removed (the token
# path — it resolves LOVEWORK_GOOGLE_HOME / HERMES_HOME /
# ~/.lovework/credentials/gmail itself). The skill copy stays as a fallback
# only while the transition settles; LOVEWORK_GAPI_SCRIPT overrides both.
def gapi_path() -> Path:
    """Path to the google_api.py script: ours first, the skill as fallback.

    Ours wins on existence, so a repo checkout works on a host with no Hermes
    at all — the whole point of A7.
    """
    ours = Path(__file__).resolve().parent / "google_api.py"
    if ours.is_file():
        return ours
    override = os.getenv("LOVEWORK_GAPI_SCRIPT", "").strip()
    if override:
        return Path(override)
    hermes_home = hermes_home_or_none()
    if hermes_home is None:
        return ours                       # no Hermes anywhere: ours, absent or not
    gapi_script = hermes_home / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
    if gapi_script.exists():
        return gapi_script
    # Fallback: check the parent of the profile's HERMES_HOME (root install)
    # e.g. ~/.hermes-host-a/skills/... when running under profiles/profile-a/
    fallback = hermes_home.parent / "skills" / "productivity" \
        / "google-workspace" / "scripts" / "google_api.py"
    if not fallback.exists() and hermes_home.parent != hermes_home:
        # Try grandparent if the parent isn't root either
        fallback2 = hermes_home.parent.parent / "skills" / "productivity" \
            / "google-workspace" / "scripts" / "google_api.py"
        if fallback2.exists():
            return fallback2
        return fallback
    return fallback

_PY_CACHE: Optional[str] = None


def _has_google_libs(py: str) -> bool:
    try:
        r = subprocess.run(
            [py, "-c", "import googleapiclient, google.auth"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def gapi_python() -> str:
    """Resolve a Python interpreter that can run google_api.py.

    Prefers a Python that has googleapiclient (so the fallback path works without `gws`).
    Override with LOVEWORK_GMAIL_PYTHON. Falls back to bare `python3` (works only if
    `gws` is installed). Cached after first resolution.
    """
    global _PY_CACHE
    if _PY_CACHE is not None:
        return _PY_CACHE

    for py in (
        os.getenv("LOVEWORK_GMAIL_PYTHON"),
        sys.executable,                    # the venv — has the Google libs since A7
        "/usr/local/bin/python3",          # macOS: pre-A7 fallback
        shutil.which("python3") or "python3",
    ):
        if py and _has_google_libs(py):
            _PY_CACHE = py
            logger.debug(f"[gmail] gapi python resolved: {py}")
            return py

    _PY_CACHE = shutil.which("python3") or "python3"
    logger.debug("[gmail] no python with googleapiclient found; falling back to python3 "
                 "(works only if the `gws` binary is installed)")
    return _PY_CACHE


def run_gapi(*args, credential_home: Optional[Path] = None) -> Optional[object]:
    """Run google_api.py with the resolved interpreter.

    Returns parsed JSON stdout, `[]` for the helper's empty-result sentinel
    ("No messages found."), or None when Gmail is unavailable (script missing,
    non-zero exit, non-JSON output, timeout) so callers can degrade gracefully
    without conflating "nothing to read" with "could not read".
    """
    # Selecting a profile-specific script is not enough: standalone skill
    # scripts resolve their token from $HERMES_HOME and otherwise default to
    # ~/.hermes (the root/default profile). Propagate LoveWork's resolved
    # profile so the script reads that profile's OAuth token too.
    runtime_hermes_home = hermes_home_or_none()
    script = gapi_path()
    if not script.exists():
        logger.debug(f"[gmail] google_api.py not found: {script}")
        return None
    env = os.environ.copy()
    # The Hermes profile selects the helper script. A principal's approved
    # mailbox credential may be elsewhere, so only the helper subprocess gets
    # its token home overridden. The parent LoveWork/Hermes runtime identity is
    # unchanged.
    # The token home, in the decoupling plan's order (A7): the principal's
    # credential_home, then docs/15's target ~/.lovework/credentials/gmail,
    # then — only while the transition settles — the Hermes profile home.
    # Both env keys are set: our vendored script reads LOVEWORK_GOOGLE_HOME
    # first; the skill-era fallback script still reads HERMES_HOME.
    import config  # local: one authority for credential paths
    default_home = config.GMAIL_CREDENTIALS_DIR
    if not (default_home / "google_token.json").is_file():
        default_home = runtime_hermes_home
    token_home = credential_home if credential_home is not None else default_home
    if token_home is not None:
        env["LOVEWORK_GOOGLE_HOME"] = str(token_home)
        env["HERMES_HOME"] = str(token_home)
    try:
        result = subprocess.run(
            [gapi_python(), str(script), *args],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except Exception as e:
        logger.debug(f"[gmail] gapi call failed ({args}): {e}")
        return None
    if result.returncode != 0:
        logger.debug(f"[gmail] gapi non-zero ({args}): {result.stderr.strip()[:200]}")
        return None
    stdout = result.stdout.strip()
    # The helper prints this plain-text sentinel (not JSON) for a successful but
    # empty result. Without this branch, an empty mailbox and a broken credential
    # both surface as None: the ambiguity that hid a dead OAuth token from
    # 2026-07-23 to 2026-09-10 and made "no mail today" indistinguishable from
    # "Gmail is down" in every log line.
    if stdout.lower() == EMPTY_RESULT_SENTINEL:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.debug(f"[gmail] gapi non-JSON output ({args}): {stdout[:120]!r}")
        return None

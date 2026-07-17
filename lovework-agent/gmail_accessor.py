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
returns None and callers gracefully no-op.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_context import resolve_hermes_home

logger = logging.getLogger(__name__)

# google_api.py lives in the Hermes google-workspace skill.
# Resolve HERMES_HOME at runtime (env var first, then ~/.hermes symlink).
def gapi_path() -> Path:
    """Path to the Hermes google_api.py script.

    Checks the active profile's skills first, then falls back to the
    root HERMES_HOME (supports profile-based setups where the google-
    workspace skill lives in the root installation, not per-profile).
    """
    hermes_home = resolve_hermes_home()
    gapi_script = hermes_home / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"
    if gapi_script.exists():
        return gapi_script
    # Fallback: check the parent of the profile's HERMES_HOME (root install)
    # e.g. ~/.hermes-gigul2/skills/... when running under profiles/hermel/
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
        "/usr/local/bin/python3",          # macOS: typically has the Google libs
        sys.executable,
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


def run_gapi(*args) -> Optional[object]:
    """Run google_api.py with the resolved interpreter; return parsed JSON stdout or None.

    Returns None on any failure (script missing, non-zero exit, bad JSON, timeout) so
    callers can treat Gmail as unavailable and degrade gracefully.
    """
    script = gapi_path()
    if not script.exists():
        logger.debug(f"[gmail] google_api.py not found: {script}")
        return None
    try:
        result = subprocess.run(
            [gapi_python(), str(script), *args],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        logger.debug(f"[gmail] gapi call failed ({args}): {e}")
        return None
    if result.returncode != 0:
        logger.debug(f"[gmail] gapi non-zero ({args}): {result.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

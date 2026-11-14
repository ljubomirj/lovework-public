"""Resolve and identify the Hermes profile LoveWork is running under.

The profile is part of LoveWork's runtime identity: it determines credentials,
models, cron state and notification channels.  An explicit environment value
always wins; otherwise the two homelab hosts use their configured profiles.
Unknown hosts must opt in to a profile rather than silently using a root Hermes
installation.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

HOST_PROFILES = {"host-a": "profile-a", "host-b": "profile-b"}


def resolve_hermes_home() -> Path:
    """Return the active *profile* directory, never a Hermes root directory."""
    explicit_home = os.environ.get("LOVEWORK_HERMES_HOME")
    if explicit_home:
        home = Path(explicit_home).expanduser()
        if (home / "profiles").is_dir() and (home / "config.yaml").exists():
            raise RuntimeError(f"HERMES_HOME is a Hermes root, not a profile: {home}")
        return home

    # Hermes itself may export its installation root as HERMES_HOME. LoveWork
    # must not inherit that ambiguity: ignore a root value and select the
    # configured profile below. A profile-valued HERMES_HOME remains valid.
    inherited_home = os.environ.get("HERMES_HOME")
    if inherited_home:
        home = Path(inherited_home).expanduser()
        if not ((home / "profiles").is_dir() and (home / "config.yaml").exists()):
            return home

    host = socket.gethostname().split(".", 1)[0].lower()
    profile = os.environ.get("LOVEWORK_HERMES_PROFILE") or HOST_PROFILES.get(host)
    base = Path(os.environ.get("LOVEWORK_HERMES_BASE", f"{Path.home()}/.hermes-{host}"))
    if not profile:
        available_profiles = sorted(p for p in (base / "profiles").glob("*") if p.is_dir())
        if len(available_profiles) == 1:
            profile = available_profiles[0].name
        else:
            raise RuntimeError(
                f"No Hermes profile configured for host {host!r}; set LOVEWORK_HERMES_PROFILE"
            )
    home = base / "profiles" / profile
    if not home.is_dir():
        raise RuntimeError(f"Configured Hermes profile does not exist: {home}")
    return home


def profile_name(home: Path | None = None) -> str:
    """Return the profile name for logs, reports and notifications."""
    resolved = home or resolve_hermes_home()
    return resolved.name


def hermes_home_or_none() -> Path | None:
    """The Hermes profile home if one resolves, else None — never raises.

    Decoupling plan A5 (2026-10-21): Hermes is optional. `resolve_hermes_home`
    keeps its contract (a caller that *needs* a profile gets an error that
    names the fix); every LoveWork caller that merely *prefers* one — the
    report header, the notifier's legacy token home, the dashboard's
    schedule view — asks this instead, and degrades to the crontab runtime.
    """
    try:
        home = resolve_hermes_home()
    except (RuntimeError, OSError):
        return None
    return home if home.is_dir() else None


def runtime_identity() -> str:
    """Who ran this: the crontab job (post-severance) or the Hermes profile.

    `LOVEWORK_JOB` is set by every launcher in `scripts/cron/`; with it the
    identity line no longer needs Hermes at all.
    """
    job = os.environ.get("LOVEWORK_JOB", "").strip()
    home = hermes_home_or_none()
    if job and home is None:
        return f"crontab job: {job}"
    if job and home is not None:
        return f"crontab job: {job} (Hermes profile {home.name} present)"
    if home is not None:
        return f"Hermes profile: {home.name} ({home})"
    return "no scheduler context (direct invocation)"


def identity_line() -> str:
    """Human-readable runtime identity suitable for logs and messages."""
    return runtime_identity()

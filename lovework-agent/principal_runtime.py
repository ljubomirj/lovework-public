"""Principal-scoped operational paths and optional Gmail source settings.

Profiles describe a person.  This module resolves the mutable state that belongs
to that person, without changing LJ's established legacy locations during the
staged migration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterable

import config


_CREDENTIAL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CREDENTIAL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class GmailMailbox:
    """A mailbox source approved for one principal."""

    label: str
    credential_key: str
    credential_home: Path
    source_name: str


@dataclass(frozen=True)
class PrincipalRuntime:
    """All mutable locations used by one pipeline invocation."""

    profile_name: str
    cache_dir: Path
    wiki_root: Path
    dataset_dir: Path
    applications_dir: Path
    sources_dir: Path
    gmail_mailbox: Optional[GmailMailbox] = None

    @property
    def state_root(self) -> Path:
        """The principal's state directory — the parent of every path here.

        Exposed rather than recomputed: callers that need it (the cockpit, the
        strata engine) should not be doing `.parent` arithmetic on a path whose
        shape is this class's business.
        """
        return self.cache_dir.parent

    @property
    def letters_dir(self) -> Path:
        """Where periodic letters to third parties are written.

        Derived rather than stored: `cache_dir` already encodes the
        principal's state root (LJ's legacy `state/example/cache`, everyone else's
        `state/<principal>/cache`), so this needs no new configuration.
        """
        return self.cache_dir.parent / "letters"


def _credential_home(credential_key: str) -> Path:
    """Resolve a source token below the explicit local host boundary."""
    if not _CREDENTIAL_KEY_RE.fullmatch(credential_key):
        raise ValueError(f"Invalid Gmail credential key: {credential_key!r}")
    credential_host = config.GMAIL_CREDENTIAL_HOST
    if not _CREDENTIAL_HOST_RE.fullmatch(credential_host):
        raise ValueError(f"Invalid Gmail credential host: {credential_host!r}")
    return config.GMAIL_CREDENTIALS_DIR / credential_host / credential_key


def _load_gmail_mailbox(profile_name: str) -> Optional[GmailMailbox]:
    """Load small, non-secret Gmail source policy from the principal profile."""
    settings_path = config.PROFILES_DIR / profile_name / "gmail-source.json"
    if not settings_path.is_file():
        return None

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Gmail source settings: {settings_path}") from exc

    label = str(settings.get("label", "")).strip()
    credential_key = str(settings.get("credential_key", "")).strip()
    if not label or not credential_key:
        raise ValueError(
            f"Gmail source settings require label and credential_key: {settings_path}"
        )
    return GmailMailbox(
        label=label,
        credential_key=credential_key,
        credential_home=_credential_home(credential_key),
        source_name=f"gmail_{profile_name}_jobs",
    )


def known_principals() -> list[str]:
    """Principals with a state tree on this machine.

    The single authority for "who exists": a binding must not invent a principal
    (that would read a path nobody owns) nor refuse a real one. Simple names
    only, so a crafted name cannot traverse out of the state root.
    """
    root = config.STATE_DIR
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir()
                  if entry.is_dir() and re.fullmatch(r"[a-z][a-z0-9_-]*", entry.name))


class PrincipalRefused(PermissionError):
    """A binding asked for a principal it is not authorized to read."""


def authorize_principal(requested: str, *, caller: str,
                        allowed: Iterable[str] = ()) -> str:
    """Validate and authorize a principal name for a read binding.

    Two separate questions, answered separately: *is this a real principal* (so
    the read resolves to state somebody owns) and *may this caller read it* (so
    one principal's binding cannot serve another's data). Collapsing them is how
    a "helpful" binding ends up exposing cross-principal state.
    """
    name = (requested or "").lower().strip()
    if not name:
        raise PrincipalRefused("no principal given: a read binding must name one")
    known = known_principals()
    if known and name not in known:
        raise PrincipalRefused(f"unknown_principal: {name!r} has no state tree")
    if name != caller and name not in set(allowed):
        raise PrincipalRefused(
            f"cross_principal_denied: this binding serves {caller!r}; reading "
            f"{name!r} requires it to be explicitly allowed")
    return name


def resolve_principal_runtime(profile_name: str) -> PrincipalRuntime:
    """Return the state boundary for a principal.

    LJ deliberately remains on the documented legacy paths until a separate,
    audited migration.  Every other principal gets a visible state tree from
    the first run, so no run can share LJ's operational data by default.
    """
    name = profile_name.lower().strip()
    if not name:
        raise ValueError("Principal profile name must not be empty")

    if name == "lj":
        return PrincipalRuntime(
            profile_name=name,
            cache_dir=config.CACHE_DIR,
            wiki_root=config.WIKI_ROOT,
            dataset_dir=config.DATASET_DIR,
            applications_dir=config.manual_root(name) or config.APPLICATIONS_DIR,
            sources_dir=config.LJ_STATE_DIR / "sources",
            gmail_mailbox=_load_gmail_mailbox(name),
        )

    state_root = config.STATE_DIR / name
    return PrincipalRuntime(
        profile_name=name,
        cache_dir=state_root / "cache",
        wiki_root=state_root / "wiki",
        dataset_dir=state_root / "dataset",
        applications_dir=state_root / "applications",
        sources_dir=state_root / "sources",
        gmail_mailbox=_load_gmail_mailbox(name),
    )

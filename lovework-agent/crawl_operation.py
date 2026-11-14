#!/usr/bin/env python3
"""Crawl operations under the writer boundary (WP1b wiring slice).

The entry the wrapper calls instead of launching ``main.py`` /
``incremental_crawl.py`` directly. One command = one outer operation:

    WriterBoundary (flock + attempt records + ambient lease)
      → pinned non-secret profile inputs (soul / CV / possibilities /
        work_auth / role file / source policies — never .env, never
        credentials)
      → the existing crawl entry point as a SUBPROCESS (full: main.py;
        incremental: incremental_crawl.py; replay: main.py
        --replay-archive; reassess: reassess.py — no fetch at all, the
        registry re-scored against retained advert evidence), stdout streamed
        through for the wrapper log,
        with ONE run id threaded in (``--run-id``) and the EXACT report
        path parsed back out (``[REPORT_PATH]`` marker — no mtime
        guessing)
      → legacy ``cache/crawl.lock`` written while the flock is held
        (dashboard compatibility; the flock is the real lock)
      → seal; ``[SEAL]`` marker for the wrapper log

Exit codes: 0 = crawled AND sealed; 1 = crawl failed (failed bundle is
still sealed — evidence, not silence) or the seal itself failed after a
successful crawl (no misleading success is delivered); 2 = the notification
token pre-flight failed deterministically inside the boundary (failed bundle
sealed, crawl not started); 75 = writer busy (EX_TEMPFAIL — cron retries), a
non-mutating attempt record is left.

The pre-flight runs *inside* the operation, after the attempt record exists:
a revoked Gmail token used to abort in the wrapper before any operation began,
so the abort left no evidence at all (docs/28 §WP1b, "attempt IDs exist before
credential preflight"). Transient token-check errors never abort.

Not yet wired: direct ``main.py`` runs and MCP ``run_pipeline`` still run
without sealing (documented gap in docs/28 §WP1b — they do not corrupt
history, they just don't add any).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import strata_contract as sc  # noqa: E402
from operation import OperationOutcome, run_operation  # noqa: E402
from run_ledger import RUN_TYPES  # noqa: E402
from writer_boundary import OUTER_RUN_ENV, WriterBusy  # noqa: E402

EX_TEMPFAIL = 75

DEFAULT_ROLES = {"lj": "general", "vj": "data-statistics-pricing",
                 "kj": "general", "pk": "general"}

# Non-secret effective inputs pinned at start. Deliberately excludes
# gmail-source.json (points into credential storage) and anything under
# credentials/.
PINNABLE_PROFILE_FILES = (
    # Every non-secret profile file the matcher chain reads (config
    # load_profile_text / load_bio): the pin set must cover the loaders, or a
    # run silently mixes pinned and live bytes (docs/28 WP1b box ③).
    "soul.md", "cv-short.md", "possibilities.md", "work_auth.md",
    "market-position.md", "bio-long.md",
    "sources.yaml", "digitaljobs_searches.yaml",
)


def build_run_id(mode: str, principal: str) -> str:
    """Wrapper-style id: type-principal-ts-pid (schema-valid, sortable)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{mode}-{principal}-{ts}-{os.getpid()}"


def pin_set(profiles_dir: Path, principal: str, role: str) -> dict[str, Path]:
    """The non-secret effective inputs for this operation, by pinned role."""
    pins: dict[str, Path] = {}
    root = profiles_dir / principal
    for name in PINNABLE_PROFILE_FILES:
        candidate = root / name
        if candidate.is_file():
            pins[f"file:{name}"] = candidate
    role_file = root / "roles" / f"{role}.md"
    if role_file.is_file():
        pins[f"role:{role}"] = role_file
    return pins


class CrawlLockCompat:
    """Write the legacy cache/crawl.lock while the real flock is held, so
    the dashboard's live-progress reader keeps working. Metadata only."""

    def __init__(self, cache_dir: Path, mode: str, principal: str):
        self.path = cache_dir / "crawl.lock"
        self.mode = mode
        self.principal = principal

    def __enter__(self) -> "CrawlLockCompat":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.write_text(
            f"pid={os.getpid()}\ntype={self.mode}\n"
            f"principal={self.principal}\nstart={started}\nstatus=running\n",
            encoding="utf-8")
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


#: Token-check statuses that make the completion email impossible: no amount of
#: crawling fixes them, so the operation aborts before spending hours on it.
FATAL_TOKEN_STATUSES = frozenset({"revoked", "missing", "missing_refresh_token"})


def _notify_preflight() -> tuple[str, str]:
    """Check the notification token in-process; return ``(text, kind)``.

    ``kind`` is ``"ok"``, ``"fatal"`` (the email can never be sent) or
    ``"transient"``. Best effort by construction: a failure of the check itself
    is transient, because a broken guard must never be the reason a crawl is
    skipped.
    """
    try:
        import notify

        result = notify.check_token()
    except Exception as exc:  # pragma: no cover - defensive
        return f"token check unavailable: {exc}", "transient"
    detail = str(result.get("detail", ""))
    if result.get("ok"):
        return f"TOKEN_OK: {detail}", "ok"
    status = str(result.get("status", "error"))
    return f"TOKEN_{status.upper()}: {detail}", (
        "fatal" if status in FATAL_TOKEN_STATUSES else "transient"
    )


def _stream_subprocess(cmd: list[str]) -> tuple[int, Path | None]:
    """Run the crawl subprocess, streaming stdout through, returning
    (returncode, exact_report_path_or_None)."""
    report_path: Path | None = None
    process = subprocess.Popen(
        cmd, cwd=str(Path(__file__).resolve().parent),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if line.startswith("[REPORT_PATH] "):
            report_path = Path(line.split("[REPORT_PATH] ", 1)[1].strip())
    returncode = process.wait()
    return returncode, report_path


def crawl_command(mode: str, principal: str, role: str,
                  source: str | None, run_id: str, python: str) -> list[str]:
    if mode == "full":
        return [python, "main.py", "--profile", principal, "--role", role,
                "--source", "all", "--report", "--run-id", run_id]
    if mode == "incremental":
        return [python, "incremental_crawl.py", "--profile", principal,
                "--role", role]
    if mode == "replay":
        replay_source = source or f"gmail_{principal}_jobs"
        return [python, "main.py", "--profile", principal, "--role", role,
                "--source", replay_source, "--replay-archive", "--report",
                "--run-id", run_id]
    if mode == "reassess":
        # No crawl: re-score the principal's registry against retained advert
        # evidence and the current profile, then seal the result like any other
        # operation. This is how a partial or missed run is completed without
        # re-fetching the web (LJ 2026-09-20: "avoid the crawl repeat").
        return [python, "reassess.py", "--profile", principal, "--role", role]
    if mode == "consume":
        # Cross-principal ingest, sealed like any other operation. The corpus it
        # reads is the OWNER's newest sealed bundle (consume.resolve_owner_corpus)
        # and a missing owner seal defers the ingest — it never falls back to the
        # owner's live state (docs/28 §WP1b box ⑤).
        return [python, "consume.py", "--profile", principal, "--role", role]
    raise ValueError(f"unknown mode: {mode}")


def declared_complete_scopes(state_root: Path, run_id: str) -> dict[str, str]:
    """Scopes a source declared complete *during this run*, from the sidecar.

    Only entries stamped with this run id count: a claim made by an earlier run
    must not qualify this run's absences (docs/28 §WP4b).
    """
    sidecar = state_root / "cache" / "complete-scopes.json"
    if not sidecar.is_file():
        return {}
    try:
        recorded = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: (entry.get("scope") or name)
            for name, entry in recorded.items()
            if entry.get("run_id") == run_id}


def run_crawl_operation(mode: str, principal: str, role: str | None,
                        source: str | None, run_id: str | None,
                        *, python: str | None = None,
                        state_root: Path | None = None,
                        profiles_dir: Path | None = None,
                        streamer=_stream_subprocess,
                        preflight: bool = False,
                        preflight_runner=_notify_preflight) -> int:
    """One outer crawl operation.

    ``preflight`` is off by default so programmatic callers (tests, future
    integrations) stay hermetic; the CLI turns it on, so every real crawl —
    wrapper, cron or by hand — checks the notification token inside the
    boundary first.
    """
    import config

    python = python or sys.executable
    state_root = state_root or config.STATE_DIR / principal
    profiles_dir = profiles_dir or config.LOVEWORK_ROOT / "profiles"
    role = role or DEFAULT_ROLES.get(principal, "general")
    run_id = run_id or build_run_id(mode, principal)
    cmd = crawl_command(mode, principal, role, source, run_id, python)

    pins = pin_set(profiles_dir, principal, role)
    shared = ("manual",) if principal == "lj" else ()  # MANUAL.md is root-shared
    # crawl type → manifest mode ("full" is a wrapper type; the sealed
    # operation is a live crawl)
    manifest_mode = "live" if mode == "full" else mode
    # Set when the token pre-flight aborts: the bundle is sealed either way, but
    # the process exit code has to tell the wrapper "no crawl was attempted".
    aborted = {"preflight": False}

    def work(ctx) -> OperationOutcome:
        with CrawlLockCompat(state_root / "cache", mode, principal):
            # Pre-flight lives inside the boundary so its failure is evidence:
            # the attempt record below exists before any credential is read.
            if preflight:
                token_text, token_kind = preflight_runner()
                print(f"[TOKEN] {token_text}", flush=True)
                ctx.record_attempt(
                    source="preflight:notify-token",
                    scope=f"{principal}/notification",
                    outcome="failed" if token_kind == "fatal" else "complete",
                    notes=token_text[:300])
                if token_kind == "fatal":
                    print("[TOKEN] Notification channel deterministically dead "
                          "— aborting before crawl.", flush=True)
                    aborted["preflight"] = True
                    return OperationOutcome(
                        execution="failed",
                        capture="complete",   # nothing ran; the state is intact
                        report_path=None,
                        note=f"preflight: {token_text[:300]}")
            ctx.record_attempt(
                source=f"crawl:{mode}", scope=f"{principal}/{role}"
                      + (f"/{source}" if source else ""),
                outcome="complete", notes=" ".join(cmd))
            # Hand the pinned tree to the crawl subprocess so its profile loads
            # read the bytes this operation captured, not whatever a live edit
            # made them since (docs/28 WP1b box ③). Scoped to the spawn and
            # restored afterwards: an in-process caller must not inherit it.
            pinned_dir = ctx.staging / "inputs"
            prior_pinned = os.environ.get(config.PINNED_INPUTS_ENV)
            prior_outer = os.environ.get(OUTER_RUN_ENV)
            os.environ[config.PINNED_INPUTS_ENV] = str(pinned_dir)
            # Same mechanism, same scope: the child is told it belongs to this
            # operation, so its entry-point guard must NOT re-acquire the lease
            # its parent already holds (docs/28 §WP1b box ①).
            os.environ[OUTER_RUN_ENV] = run_id
            try:
                returncode, report = streamer(cmd)
            finally:
                if prior_pinned is None:
                    os.environ.pop(config.PINNED_INPUTS_ENV, None)
                else:
                    os.environ[config.PINNED_INPUTS_ENV] = prior_pinned
                if prior_outer is None:
                    os.environ.pop(OUTER_RUN_ENV, None)
                else:
                    os.environ[OUTER_RUN_ENV] = prior_outer
            ctx.record_attempt(
                source=f"crawl:{mode}",
                scope=f"{principal}/{role}:exit",
                outcome="complete" if returncode == 0 else "failed",
                notes=f"subprocess exit {returncode}")
            # A source that enumerated its whole scope declared that *inside the
            # subprocess*; without this the seal would record only the crawl and
            # a later reader could never qualify an absence (docs/28 §WP4b).
            for name, scope in declared_complete_scopes(state_root, run_id).items():
                ctx.record_attempt(
                    source=name, scope=scope, outcome="complete",
                    notes="whole-scope enumeration declared by the source")
            execution = "succeeded" if returncode == 0 else "failed"
            return OperationOutcome(
                execution=execution,
                capture="complete" if returncode == 0 else "partial",
                report_path=report,
                note=None if returncode == 0
                else f"crawl subprocess exited {returncode}")

    try:
        result = run_operation(
            state_root,
            sc.RunIdentity(run_id=run_id, principal=principal, role=role,
                           mode=manifest_mode, host=os.uname().nodename,
                           aliases={"wrapper_run_id": run_id}),
            work,
            pin_inputs=pins,
            shared_resources=shared,
        )
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} "
              f"(attempt recorded at {busy.attempt_path})")
        return EX_TEMPFAIL
    except Exception as seal_error:
        # The crawl may have succeeded, but without a seal there is no
        # honest record — surface the failure instead of delivering a
        # misleading success. Staging is retained for recover().
        print(f"[SEAL_FAILED] {type(seal_error).__name__}: {seal_error}")
        return 1

    manifest = result.manifest
    print(f"[SEAL] {manifest.run.run_id} "
          f"({manifest.status.execution}/{manifest.status.capture}) "
          f"copied={result.copied_files} linked={result.linked_files} "
          f"bytes_copied={result.copied_bytes} "
          f"bundle={result.bundle_path}")
    if aborted["preflight"]:
        # Deterministic token death: the bundle is sealed, no crawl happened.
        # Exit 2 keeps the wrapper's and watchdog's existing contract.
        return 2
    return 0 if manifest.status.execution == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=RUN_TYPES)
    parser.add_argument("principal", choices=["lj", "vj", "kj", "pk"])
    parser.add_argument("role", nargs="?", default=None)
    parser.add_argument("source", nargs="?", default=None,
                        help="replay source override (default gmail_<p>_jobs)")
    parser.add_argument("--run-id", default=None,
                        help="outer id shared with the wrapper run ledger")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the in-boundary notification token check")
    args = parser.parse_args(argv)
    return run_crawl_operation(
        args.mode, args.principal, args.role, args.source, args.run_id,
        preflight=not args.no_preflight)


if __name__ == "__main__":
    raise SystemExit(main())

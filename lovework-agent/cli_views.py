"""Read-only terminal views over principal state and host dashboard metadata."""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


SECTIONS = ("profiles", "sources", "reports", "wiki", "runs", "progress",
            "applications", "entities", "system", "config", "cron")


def _name(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("name must be a single filename, not a path")
    return value


def _child(directory: Path, name: str) -> Path:
    path = directory / _name(name)
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("requested file escapes its owning directory")
    return path


def _file(path: Path) -> dict:
    if not path.is_file():
        return {"available": False, "path": str(path), "reason": "file not present on this host"}
    return {"available": True, "path": str(path),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "text": path.read_text(encoding="utf-8")}


def _files(directory: Path, pattern: str) -> list[Path]:
    return sorted((p for p in directory.glob(pattern)
                   if p.is_file() and p.resolve().is_relative_to(directory.resolve())),
                  key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def _metadata(path: Path) -> dict:
    stat = path.stat()
    return {"name": path.name, "path": str(path), "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}


def _host_view(section: str) -> dict:
    # These existing dashboard readers own host/Hermes observations. Never
    # label their configuration or schedule as principal-specific state.
    try:
        import dashboard_server as dashboard
    except RuntimeError as exc:
        return {"scope": "host", "host": socket.gethostname(),
                "available": False, "reason": str(exc)}
    if section == "config":
        raw = dashboard.fetch_config_summary()
        # Do not forward fallback_models: provider objects may carry credentials.
        data = {key: raw[key] for key in ("available", "path") if key in raw}
        for key in ("default_model", "delegation"):
            if isinstance(raw.get(key), dict):
                data[key] = {k: v for k, v in raw[key].items()
                             if k in {"provider", "default", "model"}}
        data["custom_providers"] = [
            {k: p[k] for k in ("name", "model") if k in p}
            for p in raw.get("custom_providers", []) if isinstance(p, dict)]
        data["fallbacks"] = [
            {k: p[k] for k in ("provider", "model") if k in p}
            for p in raw.get("fallbacks", []) if isinstance(p, dict)]
        if not raw.get("available"):
            data["reason"] = "Hermes configuration missing or unreadable"
    elif section == "cron":
        paths = [dashboard.HERMES_HOME / "cron/jobs.json",
                 dashboard.HERMES_HOME.parent / "cron/jobs.json"]
        path = next((p for p in paths if p.is_file()), paths[0])
        if path.is_file():
            # The dashboard hides malformed JSON as []; inspection must not.
            json.loads(path.read_text(encoding="utf-8"))
        data = {"available": path.is_file(), "path": str(path),
                "jobs": dashboard.fetch_cron_jobs()}
    else:
        data = dashboard.fetch_system()
        # Legacy dashboard paths belong to LJ, not to the selected principal.
        for key in ("wiki_path", "wiki_rel", "jobs_csv", "jobs_csv_rel"):
            data.pop(key, None)
    return {"scope": "host", "host": socket.gethostname(),
            "hermes_home": str(dashboard.HERMES_HOME), "data": data}


def read_view(section: str, principal: str = "lj", options: dict | None = None) -> dict:
    """Project existing evidence without writes, acquisition or closure guesses."""
    options = options or {}
    if section not in SECTIONS:
        raise ValueError(f"unknown section: {section}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", principal):
        raise ValueError("invalid principal name")
    limit = options.get("limit", 20)
    if limit < 1:
        raise ValueError("limit must be positive")
    if section in {"system", "config", "cron"}:
        return _host_view(section)

    import config
    from principal_runtime import resolve_principal_runtime

    if section == "profiles":
        found = config.profile_dirs()
        profiles = [{"name": name, "path": str(path),
                     "soul_exists": (path / "soul.md").is_file(),
                     "roles": sorted(f.stem for f in (path / "roles").glob("*.md"))}
                    for name, path in sorted(found.items())]
        return {"scope": "profiles", "path": str(config.PROFILES_DIR),
                "available": bool(found), "profiles": profiles}

    if not config.profile_dir(principal).is_dir() and not (config.STATE_DIR / principal).is_dir():
        raise ValueError(f"unknown principal: {principal}")
    runtime = resolve_principal_runtime(principal)
    result = {"scope": "principal", "principal": principal, "host": socket.gethostname()}
    if section == "sources":
        from pipeline import describe_sources
        result.update(policy=describe_sources(principal, runtime),
                      reference=_file(runtime.wiki_root / "sources.md"),
                      policy_path=str(config.PROFILES_DIR / principal / "sources.yaml"))
    elif section == "wiki":
        org = options.get("org")
        if org:
            from wiki_store import WikiStore
            _name(org)
            filename = WikiStore._safe_filename(org)
            from wiki_store import org_page_for

            path = (org_page_for(runtime.wiki_root, filename)
                    or _child(runtime.wiki_root / "orgs", filename + ".md"))
        else:
            path = runtime.wiki_root / "index.md"
        result.update(_file(path))
    elif section == "reports":
        directory = runtime.wiki_root / "reports"
        files = _files(directory, "*.md")
        if options.get("name"):
            result.update(_file(_child(directory, options["name"])))
        elif options.get("latest"):
            result.update(_file(files[0]) if files else {
                "available": False, "reason": "no reports on this host", "path": str(directory)})
        else:
            result.update(available=directory.is_dir(), count=len(files),
                          reports=[_metadata(p) for p in files[:limit]])
    elif section in {"runs", "progress"}:
        directory = runtime.cache_dir / "runs"
        records = []
        for path in _files(directory, "*.json")[:limit]:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError(f"run record must be an object: {path}")
            records.append({"path": str(path), "record": record})
        result.update(available=directory.is_dir(), runs=records,
                      note="Recorded execution state on this host; copied records do not prove live process health.")
        if section == "progress":
            logs = _files(runtime.state_root / "logs", "*.log")
            result["latest_log"] = None
            if logs:
                with logs[0].open(encoding="utf-8") as handle:
                    tail = [line.rstrip() for line in deque(handle, maxlen=12)]
                result["latest_log"] = {**_metadata(logs[0]), "last_lines": tail}
    elif section == "entities":
        from wiki_store import org_page_dirs

        directory = runtime.wiki_root / "orgs"
        pages = [p for d in org_page_dirs(runtime.wiki_root)
                 for p in _files(d, "*.md")]
        result.update(available=any(d.is_dir() for d in org_page_dirs(runtime.wiki_root)),
                      count=len(pages),
                      entities=[_metadata(p) for p in pages[:limit]])
    elif section == "applications":
        from dashboard_server import _app_dir_sort_key
        directory = runtime.applications_dir
        entries = sorted((p for p in directory.iterdir() if p.is_dir()),
                         key=_app_dir_sort_key, reverse=True) if directory.is_dir() else []
        result.update(available=directory.is_dir(), path=str(directory), count=len(entries),
                      applications=[{"name": p.name, "path": str(p)} for p in entries[:limit]])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=SECTIONS)
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="read latest report body")
    selection.add_argument("--name", help="read a report by filename")
    parser.add_argument("--org", help="read this organisation's wiki page")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if (args.latest or args.name) and args.section != "reports":
        parser.error("--latest/--name apply only to reports")
    if args.org and args.section != "wiki":
        parser.error("--org applies only to wiki")
    try:
        result = read_view(args.section, args.profile, vars(args))
    except (OSError, ValueError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "principal": args.profile}))
        else:
            print(f"inspect: {exc}", file=sys.stderr)
        return 2
    if not args.json and "text" in result:
        print(result["text"], end="" if result["text"].endswith("\n") else "\n")
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

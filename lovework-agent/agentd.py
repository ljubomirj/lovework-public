#!/usr/bin/env python3
"""agentd.py — LoveWork's Telegram door (the agent plan, W1).

The principal drives LoveWork through Telegram (manifesto: *TG 1st, no
terminal, state is just folders*). This module is the door: a stdlib-only
getUpdates long-poll that turns inbound messages into durable files, and a
burst collector that wakes ONE session per quiet gap.

W1 scope (tasks/2026-10-23-agent-plan.md): the message lands in the
principal's inbox, and the door answers "awake" with pointers. The session
runner (boot → act → close) is W2; read-only conversations are W3.

Design rules this module obeys:

- **Inbound text is data, never instructions.** Every message is mirrored
  verbatim into `state/<p>/loop/inbox/` *before* anything looks at it, and
  nothing in W1 feeds it to a prompt at all.
- **The whitelist is a routing table, not a prompt rule.** `router.json`
  beside the bot token maps chat id → principal; unknown chats are logged
  (id and display name only) and never answered.
- **Two channels, one record.** Telegram carries short answers with
  pointers; the artifacts (inbox, sessions) land in the principal repo.
- **Host-local runtime, not evidence.** The offset and the lock live under
  `state/monitor/` and are gitignored (ch32 §Q1b); the inbox and session
  manifests travel with the principal's repo.

Configuration (env first, then files — same order as tg.py):

- token: `LOVEWORK_TELEGRAM_TOKEN`, else `credentials/telegram/bot.token`
  (repo) or `~/.lovework/credentials/telegram/bot.token`
- router: `LOVEWORK_AGENTD_ROUTER`, else `router.json` beside the token
- offsets/locks: `state/monitor/agentd-offset.json`, `state/monitor/agentd.lock`

Modes:

    agentd.py --serve             # the service: long-poll, bursts, sessions
    agentd.py --once              # drain pending updates, run bursts, exit
    agentd.py --enroll            # print inbound chat ids/names, write nothing
    agentd.py --doctor            # token + router + one getUpdates round-trip

Exit codes follow the house vocabulary: 0 work done (or nothing pending),
1 failure, 2 usage, 75 unconfigured (no token / no router — pending, not
broken).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (path set above, matching the sibling modules)
import tg  # noqa: E402  (the sender; agentd sends through it, never around it)

ROUTER_ENV = "LOVEWORK_AGENTD_ROUTER"
#: Where the unified door log lands. The default is the tracked engine log;
#: a dev door on another host points this at a host-local, gitignored file so
#: two doors never churn one tracked log (ch32 §Q1b's host-local rule).
LOG_ENV = "LOVEWORK_AGENTD_LOG"
WAKE_AFTER_ENV = "LOVEWORK_AGENTD_WAKE_AFTER"
POLL_TIMEOUT_ENV = "LOVEWORK_AGENTD_POLL_TIMEOUT"
WAKE_AFTER_DEFAULT = 45.0        # seconds of silence that close a burst
POLL_TIMEOUT_DEFAULT = 50        # Telegram long-poll seconds
ACK_LIMIT = 4000                 # answers ride tg.chunk; keep replies short


def log(event: str, **fields: object) -> None:
    """One line to the unified door log (state/logs/agentd.log) and stderr."""
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tail = "".join(f" {k}={v}" for k, v in fields.items() if v not in ("", None))
    line = f"{at} {event}{tail}"
    try:
        explicit = os.getenv(LOG_ENV, "").strip()
        path = Path(explicit).expanduser() if explicit else \
            config.STATE_DIR / "logs" / "agentd.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass                       # a log that cannot be written is not a failure
    print(line, file=sys.stderr, flush=True)


def _bot_tag(token: str) -> str:
    """A short stable id for one bot — never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def offset_path(token: str) -> Path:
    """Per-bot: two bots on one host must never share a poll offset."""
    return config.STATE_DIR / "monitor" / f"agentd-offset-{_bot_tag(token)}.json"


def lock_path(token: str) -> Path:
    return config.STATE_DIR / "monitor" / f"agentd.lock-{_bot_tag(token)}"


def load_offset(token: str) -> int:
    """The last processed update_id + 1, or 0 when starting fresh.

    No auto-migration from the original single file: on a two-bot host that
    value belongs to the *other* bot, and adopting it would skip this bot's
    updates forever (measured 2026-10-24 — the dev door booted on the
    production watermark). Migration is a by-hand step per host.
    """
    try:
        return int(json.loads(
            offset_path(token).read_text(encoding="utf-8"))["next"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def save_offset(next_offset: int, token: str) -> None:
    path = offset_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next": int(next_offset)}, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def router_paths() -> list[Path]:
    explicit = os.getenv(ROUTER_ENV, "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    return [d / "router.json" for d in tg._credential_paths()]


def router_file() -> Path | None:
    """The first router.json that exists — presence means "configured"."""
    for path in router_paths():
        if path.is_file():
            return path
    return None


def load_router() -> dict[str, dict]:
    """{chat_id: {"principal": p, ...}} — the whitelist, as a routing table.

    A present-but-empty router is *configured with no chats yet*: the door
    runs and logs unknown chat ids (that is enrollment), it does not exit.
    """
    path = router_file()
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    chats = raw.get("chats", raw) if isinstance(raw, dict) else {}
    return {str(k): dict(v) for k, v in chats.items()
            if isinstance(v, dict) and v.get("principal")}


def _router_mtime() -> float:
    path = router_file()
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def get_updates(token: str, offset: int, timeout: int) -> list[dict]:
    """One getUpdates round-trip; API errors raise, an empty drain is []."""
    status, body = tg._post(token, "getUpdates",
                            {"offset": offset, "timeout": timeout,
                             "allowed_updates": ["message"]},
                            timeout=timeout + 15.0)
    if status != 200 or not body.get("ok"):
        raise RuntimeError(str(body.get("description") or f"HTTP {status}"))
    return list(body.get("result") or [])


def message_of(update: dict) -> dict | None:
    """The message inside an update, or None for anything else."""
    message = update.get("message")
    return message if isinstance(message, dict) else None


def sender_name(message: dict) -> str:
    sender = message.get("from") or {}
    parts = [str(sender.get("first_name") or ""), str(sender.get("last_name") or "")]
    name = " ".join(p for p in parts if p).strip()
    return name or str(sender.get("username") or "unknown")


def write_inbox(principal: str, update: dict, message: dict) -> Path:
    """Mirror one inbound message verbatim into the principal's inbox."""
    at = datetime.now(timezone.utc)
    stamp = at.strftime("%Y%m%d-%H%M%S")
    update_id = int(update.get("update_id") or 0)
    inbox = config.STATE_DIR / principal / "loop" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{stamp}-{update_id}.md"
    n = 1
    while path.exists():                     # two messages in one second
        n += 1
        path = inbox / f"{stamp}-{update_id}-{n}.md"
    text = str(message.get("text") or "")
    header = [
        f"# Inbound — {at.isoformat(timespec='seconds')}",
        "",
        f"- update_id: {update_id}",
        f"- chat_id: {message.get('chat', {}).get('id')}",
        f"- from: {sender_name(message)}",
        f"- message_id: {message.get('message_id')}",
        "",
        "---",
        "",
    ]
    path.write_text("\n".join(header) + text + "\n", encoding="utf-8")
    return path


def session_dir(principal: str) -> Path:
    at = datetime.now(timezone.utc)
    root = config.STATE_DIR / principal / "loop" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / at.strftime("%Y%m%d-%H%M%S")
    n = 1
    while path.exists():
        n += 1
        path = root / f"{at.strftime('%Y%m%d-%H%M%S')}-{n}"
    path.mkdir(parents=True)
    return path


def run_session(principal: str, burst: list[dict], *, send: bool = True) -> Path:
    """One session per burst: W2 when enabled, the W1 ack otherwise.

    The W2 body is session_runner.run (boot → act → close); any failure there
    falls back to the W1 ack — the door never dies with its session.
    """
    if os.getenv("LOVEWORK_AGENTD_SESSIONS", "1") != "0":
        try:
            import session_runner
            row = session_runner.run(principal, burst)
            reply = f"{row['outcome']} · {row['answer']}"
            if row.get("suggestion"):
                reply += f"\nNEXT: {row['suggestion']}"
            reply += (f"\nsession: state/{principal}/loop/sessions/"
                      f"{row['session']}/")
            if send:
                receipt = tg.send(reply, chat=str(burst[0]["chat_id"]))
                if not receipt.get("ok"):
                    log("session reply failed", error=receipt.get("error"))
            log("session", principal=principal, outcome=row["outcome"],
                turns=row["turns"], tools=row["tool_calls"])
            return Path(row["directory"])
        except Exception as exc:                  # noqa: BLE001 — the door lives
            log("session runner failed, w1-ack fallback",
                error=f"{type(exc).__name__}: {exc}"[:200])
    directory = session_dir(principal)
    inbox_paths = [Path(row["inbox"]) for row in burst]
    manifest = [
        f"# Session {directory.name} — {principal}",
        "",
        "- kind: w1-ack",
        f"- messages: {len(burst)}",
        f"- at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Messages",
    ]
    manifest += [f"- {p.parent.name}/{p.name}" for p in inbox_paths]
    (directory / "session.md").write_text("\n".join(manifest) + "\n",
                                          encoding="utf-8")
    first = min(inbox_paths)
    last = max(inbox_paths)
    span = first.name if first == last else f"{first.name} … {last.name}"
    reply = (f"awake · {len(burst)} message(s) mirrored to "
             f"state/{principal}/loop/inbox/ ({span}) · "
             "session runner lands in W2")
    if send:
        receipt = tg.send(reply, chat=str(burst[0]["chat_id"]))
        if not receipt.get("ok"):
            log("session reply failed", error=receipt.get("error"))
    log("session w1-ack", principal=principal, messages=len(burst))
    return directory


class Bursts:
    """One open burst per principal; closed after a quiet gap."""

    def __init__(self, wake_after: float):
        self.wake_after = wake_after
        self.by_principal: dict[str, list[dict]] = {}
        self.deadline: dict[str, float] = {}

    def add(self, principal: str, row: dict, now: float) -> None:
        self.by_principal.setdefault(principal, []).append(row)
        self.deadline[principal] = now + self.wake_after

    def due(self, now: float) -> list[str]:
        return [p for p, t in self.deadline.items() if now >= t]

    def take(self, principal: str) -> list[dict]:
        self.deadline.pop(principal, None)
        return self.by_principal.pop(principal, [])


def process_updates(updates: list[dict], router: dict, bursts: Bursts,
                    *, now: float, send: bool = True) -> int:
    """Route every update; return the next offset. Unauthorized: log, skip."""
    next_offset = 0
    for update in sorted(updates, key=lambda u: int(u.get("update_id") or 0)):
        update_id = int(update.get("update_id") or 0)
        next_offset = max(next_offset, update_id + 1)
        message = message_of(update)
        if message is None:
            continue
        chat_id = str((message.get("chat") or {}).get("id") or "")
        entry = router.get(chat_id)
        if entry is None:
            log("unauthorized chat", chat=chat_id, who=sender_name(message))
            continue
        principal = str(entry["principal"])
        inbox = write_inbox(principal, update, message)
        bursts.add(principal, {"inbox": str(inbox), "chat_id": chat_id,
                               "update_id": update_id}, now)
        log("inbound", principal=principal, file=inbox.name)
    return next_offset


def flush_due(bursts: Bursts, now: float, *, send: bool = True) -> int:
    """Run one session per burst whose quiet gap has passed."""
    ran = 0
    for principal in bursts.due(now):
        burst = bursts.take(principal)
        if burst:
            run_session(principal, burst, send=send)
            ran += 1
    return ran


def single_flight(token: str = ""):
    """One agentd per bot per host; the lock is advisory and host-local.

    Returns the open lock *file object* — the caller must hold it for the
    process lifetime, or the fd closes and the flock releases with it.
    """
    path = lock_path(token or "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def doctor(token: str) -> int:
    """Verify token, router and one round-trip; never sends."""
    if not token:
        log("doctor", error="no token")
        return 75
    router = load_router()
    log("doctor", chats=len(router))
    if router_file() is None:
        return 75
    try:
        updates = get_updates(token, load_offset(token), 0)
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        log("doctor", error=str(exc)[:200])
        return 1
    log("doctor ok", pending=len(updates))
    return 0


def serve(*, token: str, once: bool = False, enroll: bool = False,
          wake_after: float | None = None, poll_timeout: int | None = None,
          send: bool = True) -> int:
    """The door: poll, route, collect bursts, wake sessions."""
    wake_after = wake_after if wake_after is not None else \
        float(os.getenv(WAKE_AFTER_ENV, WAKE_AFTER_DEFAULT))
    poll_timeout = poll_timeout if poll_timeout is not None else \
        int(os.getenv(POLL_TIMEOUT_ENV, POLL_TIMEOUT_DEFAULT))
    if enroll:
        offset = load_offset(token)
        log("enroll start")
        while True:
            updates = get_updates(token, offset, poll_timeout)
            for update in updates:
                message = message_of(update) or {}
                chat = message.get("chat") or {}
                log("enroll", chat=chat.get("id"), who=sender_name(message),
                    type=chat.get("type"))
                offset = int(update.get("update_id") or 0) + 1
                save_offset(offset, token)
        return 0
    lock = single_flight()          # hold for the lifetime of the door
    if lock is None:
        log("another agentd holds the lock")
        return 1
    if router_file() is None:
        log("no router configured",
            tried=",".join(str(p) for p in router_paths()))
        return 75
    router = load_router()
    router_mtime = _router_mtime()
    if not router:
        log("router has no chats yet — enrollment mode (ids are logged)")
    bursts = Bursts(wake_after)
    offset = load_offset(token)
    log("serve start", chats=len(router), offset=offset)
    while True:
        if _router_mtime() != router_mtime:      # live reload, no restart
            router_mtime = _router_mtime()
            router = load_router()
            log("router reloaded", chats=len(router))
        try:
            updates = get_updates(token, offset, poll_timeout)
        except TimeoutError:
            # The 50 s long-poll ending in a read timeout is the protocol
            # working (and laptop sleep does it too) — treat it as an empty
            # drain and fall through, so --once still reaches its exit (a
            # `continue` here would loop forever in once mode).
            updates = []
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            log("poll error", error=str(exc)[:200])
            if once:
                return 1
            time.sleep(min(30.0, poll_timeout))
            continue
        now = time.monotonic()
        if updates:
            offset = process_updates(updates, router, bursts, now=now,
                                     send=send)
            save_offset(offset, token)
        if once:
            flush_due(bursts, now + wake_after + 1.0, send=send)
            return 0
        time.sleep(0.25)
        flush_due(bursts, time.monotonic(), send=send)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="the service loop")
    mode.add_argument("--once", action="store_true",
                      help="drain pending updates, run bursts, exit")
    mode.add_argument("--enroll", action="store_true",
                      help="print inbound chat ids, write nothing else")
    mode.add_argument("--doctor", action="store_true",
                      help="verify token/router/round-trip, never send")
    parser.add_argument("--wake-after", type=float, default=None,
                        help="seconds of silence closing a burst (default 45)")
    parser.add_argument("--poll-timeout", type=int, default=None,
                        help="Telegram long-poll seconds (default 50)")
    parser.add_argument("--no-send", action="store_true",
                        help="suppress replies (tests, dry runs)")
    args = parser.parse_args(argv)

    token = tg.resolve_token()
    if not args.enroll and not token:
        log("no token configured")
        return 75
    try:
        if args.doctor:
            return doctor(token)
        if not (args.serve or args.once or args.enroll):
            parser.error("choose --serve, --once, --enroll or --doctor")
        return serve(token=token, once=args.once, enroll=args.enroll,
                     wake_after=args.wake_after,
                     poll_timeout=args.poll_timeout, send=not args.no_send)
    except KeyboardInterrupt:
        log("interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())

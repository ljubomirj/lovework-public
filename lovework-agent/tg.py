#!/usr/bin/env python3
"""tg.py — LoveWork's one-way Telegram reporting channel.

Part A1 of the decoupling plan (2026-10-20): Hermes cron's stdout delivery is
the *only* place Telegram exists today — our jobs print, Hermes delivers. This
module is the replacement: a stdlib-only sender (no SDK, no Hermes) that jobs
call beside their printing, with receipts the way `notify.py` evidences Gmail.

    tg.py < report.txt                 # send stdin, one receipt line
    tg.py --tee < report.txt           # print AND send (the launcher form)
    tg.py --markdown --file notes.md   # Telegram markdown, plain-text fallback
    tg.py --doctor                     # verify token + chat without sending

Configuration, resolved in this order (env first, then the file the plan names):

- token: LOVEWORK_TELEGRAM_TOKEN, else `credentials/telegram/bot.token`
  (repo) or `~/.lovework/credentials/telegram/bot.token` (the docs/15 target)
- chat: LOVEWORK_TELEGRAM_CHAT, else `chat.id` beside the token file

The token is never printed. Exit codes follow the house vocabulary: 0 sent
(doctor: verified), 1 the send failed, 2 usage, 75 nothing is configured yet —
temporary by construction (the BotFather step), and a cron line that sees 75
knows the channel is pending, not broken.

Long reports are chunked on newline boundaries under Telegram's 4096-char
message limit — a report channel must not silently truncate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (path set above, matching the sibling modules)

API_BASE = "https://api.telegram.org/bot"
#: Telegram's hard message limit is 4096 chars; chunk well under it.
MAX_CHUNK = 4000
TOKEN_ENV = "LOVEWORK_TELEGRAM_TOKEN"
TOKEN_FILE_ENV = "LOVEWORK_TELEGRAM_TOKEN_FILE"
CHAT_ENV = "LOVEWORK_TELEGRAM_CHAT"


def _credential_paths() -> list[Path]:
    """Where the token file may live, most specific first.

    The repo's `credentials/` is the bootstrap root; `~/.lovework/credentials/`
    is docs/15's target — both are checked so the channel works before and
    after the credentials move.
    """
    return [
        config.LOVEWORK_ROOT / "credentials" / "telegram",
        Path(os.getenv("LOVEWORK_HOME") or Path.home() / ".lovework") / "credentials" / "telegram",
    ]


def resolve_token() -> str:
    """The bot token, or "" — never printed, never logged."""
    env = os.getenv(TOKEN_ENV, "").strip()
    if env:
        return env
    explicit = os.getenv(TOKEN_FILE_ENV, "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else \
        [d / "bot.token" for d in _credential_paths()]
    for path in candidates:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return ""


def resolve_chat() -> str:
    """The destination chat id or @channelname, or ""."""
    env = os.getenv(CHAT_ENV, "").strip()
    if env:
        return env
    for d in _credential_paths():
        try:
            chat = (d / "chat.id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if chat:
            return chat
    return ""


def _post(token: str, method: str, payload: dict,
          timeout: float = 20.0) -> tuple[int, dict]:
    """One Bot API call. Returns (http_status, parsed_body). Never raises for
    API-level errors — the receipt carries them; only transport failures raise
    (urllib.error.URLError), which the caller converts to an error receipt."""
    request = urllib.request.Request(
        API_BASE + token + "/" + method,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
            return int(getattr(response, "status", 200)), body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            body = {}
        return int(exc.code), body


def chunk(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split a report on newline boundaries under the limit.

    A single line longer than the limit is split hard — better a mid-word break
    in a reports channel than a dropped tail.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:              # one oversized line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def send(text: str, *, chat: str = "", token: str = "", markdown: bool = False,
         timeout: float = 20.0) -> dict:
    """Send one report (chunked as needed). Returns the receipt dict.

    `ok` is True only if every chunk carried. Telegram's legacy Markdown fails
    the whole message on one unmatched entity, so a 400 with markdown enabled
    falls back to plain text for that chunk and says so — a report must arrive
    even when its asterisks are wrong.
    """
    token = token or resolve_token()
    chat = chat or resolve_chat()
    receipt: dict = {"at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "ok": False, "chat": chat, "chunks": 0,
        "message_ids": [], "error": ""}
    if not token or not chat:
        receipt["error"] = ("channel not configured: "
                            + (f"no token ({TOKEN_ENV} or bot.token)"
                               if not token else f"no chat ({CHAT_ENV} or chat.id)"))
        receipt["unconfigured"] = True
        return receipt

    for piece in chunk(text):
        payload: dict = {"chat_id": chat, "text": piece}
        if markdown:
            payload["parse_mode"] = "Markdown"
        try:
            status, body = _post(token, "sendMessage", payload, timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return receipt
        if status == 400 and markdown:
            # Broken entities, not a broken channel: retry the chunk plain.
            payload.pop("parse_mode", None)
            try:
                status, body = _post(token, "sendMessage", payload, timeout)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                receipt["error"] = f"{type(exc).__name__}: {exc}"[:200]
                return receipt
            receipt.setdefault("fallbacks", 0)
            receipt["fallbacks"] += 1
        if status != 200 or not body.get("ok"):
            description = str(body.get("description") or f"HTTP {status}")
            parameters = body.get("parameters") or {}
            receipt["error"] = description[:200]
            if parameters.get("retry_after"):
                receipt["retry_after"] = parameters["retry_after"]
            return receipt
        receipt["chunks"] += 1
        receipt["message_ids"].append(
            (body.get("result") or {}).get("message_id"))
    receipt["ok"] = True
    receipt["bytes"] = len(text.encode("utf-8"))
    return receipt


def doctor(*, token: str = "", chat: str = "") -> dict:
    """Verify the channel without sending: getMe (token) + getChat (target)."""
    token = token or resolve_token()
    chat = chat or resolve_chat()
    out: dict = {"ok": False, "error": ""}
    if not token:
        out["error"] = f"no token ({TOKEN_ENV} or bot.token)"
        out["unconfigured"] = True
        return out
    try:
        status, me = _post(token, "getMe", {})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = f"getMe: {type(exc).__name__}: {exc}"[:200]
        return out
    if status != 200 or not me.get("ok"):
        out["error"] = f"getMe: {str(me.get('description') or status)[:160]}"
        return out
    out["bot"] = "@" + str((me.get("result") or {}).get("username") or "?")
    if not chat:
        out["error"] = f"no chat ({CHAT_ENV} or chat.id)"
        out["unconfigured"] = True
        return out
    try:
        status, target = _post(token, "getChat", {"chat_id": chat})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = f"getChat: {type(exc).__name__}: {exc}"[:200]
        return out
    if status != 200 or not target.get("ok"):
        out["error"] = f"getChat: {str(target.get('description') or status)[:160]}"
        return out
    result = target.get("result") or {}
    out["chat_type"] = result.get("type", "?")
    out["chat_title"] = result.get("title") or result.get("username") or chat
    out["ok"] = True
    return out


def discover_chats(*, token: str = "") -> list[dict]:
    """The chats that recently talked to this bot, from getUpdates.

    The id of a *private* channel (or a 1:1) is the one thing Telegram gives
    you no menu for — but the moment anyone posts in the channel (or /starts
    the bot), the update carries the chat id. This lists every distinct chat
    in the bot's recent updates: id, type, name — pick the channel's.
    """
    token = token or resolve_token()
    if not token:
        return [{"error": f"no token ({TOKEN_ENV} or bot.token)"}]
    try:
        _status, body = _post(token, "getUpdates", {"limit": 100}, 30.0)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"[:200]}]
    chats: dict[str, dict] = {}
    for update in body.get("result") or []:
        for kind in ("message", "channel_post", "edited_message",
                     "edited_channel_post"):
            event = update.get(kind) or {}
            chat = event.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            if chat_id and chat_id not in chats:
                chats[chat_id] = {
                    "id": chat_id,
                    "type": chat.get("type", "?"),
                    "title": chat.get("title") or chat.get("username")
                    or chat.get("first_name") or "",
                }
    return list(chats.values())


RECEIPTS_ENV = "LOVEWORK_TG_RECEIPTS"


def record_receipt(receipt: dict, *, path: str | os.PathLike | None = None,
                   label: str = "") -> str:
    """Append one receipt line, so "did the channel deliver?" has a file answer.

    This module's docstring has always promised receipts the way `notify.py`
    evidences Gmail, but until 2026-10-24 no caller kept one: `report()` in
    `scripts/cron/env.sh` piped tg.py's output to /dev/null, so the only way to
    answer "did it send?" was to send a test. The receipt is the evidence, and
    the launcher now passes this a path.

    Path resolution: the argument, else `LOVEWORK_TG_RECEIPTS`, else nothing is
    written (an interactive caller still gets the receipt on stdout). The row is
    the receipt plus `label`, `job` (LOVEWORK_JOB) and `pid`, so a report can be
    traced to the job that emitted it.

    Never raises: an unwritable receipt must not fail a report that arrived.
    """
    target = str(path) if path else os.getenv(RECEIPTS_ENV, "").strip()
    if not target:
        return ""
    row = dict(receipt)
    row["label"] = label
    row["job"] = os.getenv("LOVEWORK_JOB", "")
    row["pid"] = os.getpid()
    try:
        out = Path(target).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return str(out)
    except OSError:
        return ""


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=None,
                        help="send this file (default: stdin)")
    parser.add_argument("--tee", action="store_true",
                        help="print the text to stdout as well — the launcher form")
    parser.add_argument("--markdown", action="store_true",
                        help="Telegram Markdown, with a plain-text fallback")
    parser.add_argument("--doctor", action="store_true",
                        help="verify token + chat without sending, then exit")
    parser.add_argument("--discover-chat", action="store_true",
                        help="list the chats that recently messaged the bot, "
                             "with their ids — how a PRIVATE channel's numeric "
                             "id is found (post once in the channel first)")
    parser.add_argument("--json", action="store_true",
                        help="print the receipt as JSON")
    parser.add_argument("--receipts-log", default=None, metavar="PATH",
                        help="append this send's receipt as one JSON line to PATH "
                             f"(else ${RECEIPTS_ENV}, else nowhere)")
    parser.add_argument("--label", default="",
                        help="what this report is (sweep, monitor, morning…), "
                             "recorded in the receipt")
    args = parser.parse_args(argv)
    receipts = args.receipts_log if args.receipts_log else None

    if args.discover_chat:
        rows = discover_chats()
        for row in rows:
            if "error" in row:
                print(f"tg: {row['error']}", file=sys.stderr)
                return 1
            print(f"tg: chat {row['id']}  [{row['type']}]  {row['title']}")
        if not rows:
            print("tg: no updates yet — post once in the channel (or /start "
                  "the bot in a 1:1), then run this again", file=sys.stderr)
        return 0

    if args.doctor:
        receipt = doctor()
        recorded = record_receipt(receipt, path=receipts, label=args.label or "doctor")
        line = (f"tg: ok · {receipt.get('bot')} → {receipt.get('chat_title')}"
                if receipt["ok"] else f"tg: {receipt['error']}")
        if recorded and not args.json:
            line += f" · receipt → {recorded}"
        print(json.dumps(receipt, sort_keys=True) if args.json else line)
        if receipt.get("unconfigured"):
            return 75
        return 0 if receipt["ok"] else 1

    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"tg: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()
    if args.tee:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    receipt = send(text, markdown=args.markdown)
    recorded = record_receipt(receipt, path=receipts, label=args.label or "report")
    if args.json:
        print(json.dumps(receipt, sort_keys=True))      # one line, log-friendly
    else:
        if receipt["ok"]:
            note = (f" (+{receipt['fallbacks']} plain-text fallback)"
                    if receipt.get("fallbacks") else "")
            print(f"tg: sent {receipt['chunks']} message(s) to "
                  f"{receipt['chat']}{note}"
                  + (f" · receipt → {recorded}" if recorded else ""))
        else:
            print(f"tg: {receipt['error']}", file=sys.stderr)
    if receipt.get("unconfigured"):
        return 75
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())

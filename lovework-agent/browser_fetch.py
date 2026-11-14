#!/usr/bin/env python3
"""browser_fetch.py — read a page through a real, logged-in Chrome.

LJ, 2026-10-14: *"Can we not re-use user login into LinkedIn via Chrome
browser for all 3 principals … It's a temporary solution while we are developing
LoveWork."*

Some adverts are only visible to a logged-in visitor. LinkedIn is the clear case:
a logged-out request gets a login wall, so our fetcher (Exa, then plain HTTP) sees
a shell or nothing — and then *absence of evidence* quietly reads as *evidence of
life*. That is how a closed advert stayed in the working set (docs/46).

This provider drives the Chrome that is already on the host, with a **profile that
carries the principal's session**, over the DevTools protocol. Two properties make
it the right shape for now:

- **It is a credential, not a bypass.** The profile directory is named by
  configuration (`LOVEWORK_CHROME_PROFILE`), per principal
  (`LOVEWORK_BROWSER_CREDENTIAL_HOME`), exactly like the Gmail token. Moving from
  this development arrangement to one profile per principal is an env var, not a
  rewrite.
- **It reads. It never acts.** Navigation and DOM extraction only — no clicking
  Apply, no form filling, no messaging. The doctrine that the principal acts is
  what keeps an agent with a session from becoming an agent with consequences.

    ./browser_login.sh                                  # log the profile in, once
    LOVEWORK_FETCH_PROVIDERS=exa,chrome,http ../venv/bin/python3 crawler.py <url>
    LOVEWORK_CHROME_PROFILE=~/.lovework/browser/<principal>/chrome-profile …  # per principal

Requires `websockets` (already in the venv) and `google-chrome` on PATH.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from html import unescape
from pathlib import Path

logger = logging.getLogger(__name__)

#: The Chrome binary. `google-chrome` is the Debian/Ubuntu name; the others are
#: the Snap/Chromium spellings.
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser")

#: Which profile directory carries the session. Default: the user's own Chrome
#: profile — the *temporary* development arrangement LJ asked for. A principal
#: profile (option B in docs/46) is this variable pointing elsewhere.
PROFILE_ENV = "LOVEWORK_CHROME_PROFILE"

#: Where a *dedicated* LoveWork browser profile would live, per principal. Used
#: when the default profile is absent.
CREDENTIAL_HOME_ENV = "LOVEWORK_BROWSER_CREDENTIAL_HOME"

DEBUG_PORT_ENV = "LOVEWORK_CHROME_PORT"
DEFAULT_PORT = 9222

#: How long to wait for a page to settle after `load`. LinkedIn paints its advert
#: after the load event, and reading the DOM too early returns the skeleton.
SETTLE_SECONDS = float(os.getenv("LOVEWORK_CHROME_SETTLE_SECONDS", "3"))


def chrome_binary() -> str | None:
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def lovework_profile() -> Path:
    """The dedicated LoveWork browser profile — the default, and the safe one.

    A profile is a credential: this one is logged in once by the principal and
    used only by LoveWork, so nothing an agent does can disturb the browser he
    uses himself, and moving to one profile per principal is this path changing
    shape (`state/<p>/…` or `LOVEWORK_CHROME_PROFILE`), not a rewrite (docs/46 §4).
    """
    root = os.environ.get(CREDENTIAL_HOME_ENV)
    base = Path(os.path.expanduser(root)) if root else Path(os.getenv("LOVEWORK_HOME") or Path.home() / ".lovework")
    return base / "browser" / "chrome-profile"


def public_profile() -> Path:
    """A profile for pages that need **no** session — a working directory, not a
    credential.

    Chrome requires a profile to start, and most of the adverts LoveWork reads are
    public: Workday, Gem, Workable, Greenhouse. Only the login-walled ones (LinkedIn
    and its kind) need the principal's session, so requiring a credential profile to
    read a public page made us blind for no reason — measured 2026-10-14 on the
    EMBL-EBI Workday advert: the open-web providers returned **0 characters**, while
    Chrome with an empty profile read the page and it said *"The page you are
    looking for doesn't exist"*. That is the closure, and we could not see it.

    Deliberately separate from the session profile: nothing logged in ever touches
    this directory, so using it can neither read nor disturb the principal's
    accounts.
    """
    root = os.environ.get(CREDENTIAL_HOME_ENV)
    base = Path(os.path.expanduser(root)) if root else Path(os.getenv("LOVEWORK_HOME") or Path.home() / ".lovework")
    return base / "browser" / "public-profile"


def profile_dir() -> Path | None:
    """The Chrome profile that carries the session, if there is one.

    Order matters: an explicit path wins, then the dedicated LoveWork profile,
    and the user's own daily profile is **not** used by default — launching Chrome
    on it takes the singleton lock and can cost him his own session, which is the
    one outcome this feature must never cause. Pointing `LOVEWORK_CHROME_PROFILE`
    at it is allowed, deliberately, and logged as the development arrangement.
    """
    import os as _os

    explicit = _os.environ.get(PROFILE_ENV)
    if explicit:
        path = Path(_os.path.expanduser(explicit))
        if path.is_dir():
            if "google-chrome" in str(path) or "chromium" in str(path):
                logger.warning(
                    "[browser] using the user's own Chrome profile (%s): this is "
                    "the development arrangement — Chrome on it holds the singleton "
                    "lock while LoveWork fetches, and a shared session sees every "
                    "principal's adverts as one person's", path)
            return path
        return None
    dedicated = lovework_profile()
    if dedicated.is_dir() and (dedicated / "Default").is_dir():
        return dedicated
    return None


def available(*, session: bool = True) -> tuple[bool, str]:
    """(usable, why not) — a caller can say *why* the provider is absent.

    `session=True` asks for the profile that carries a login (a host that walls its
    pages behind one). `session=False` asks only for a browser that can read a
    public page, which needs no credential at all.
    """
    if chrome_binary() is None:
        return False, "no chrome binary on PATH"
    if session and profile_dir() is None:
        return False, f"no browser profile ({PROFILE_ENV} unset and no default)"
    try:
        import websockets  # noqa: F401
    except Exception:
        return False, "websockets not installed"
    return True, ""


def _debug_port() -> int:
    try:
        return int(os.environ.get(DEBUG_PORT_ENV, DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _endpoint(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            return json.loads(response.read())
    except Exception:
        return None


def ensure_chrome(*, timeout: float = 20.0, session: bool = True) -> dict | None:
    """A Chrome with a debug port, launched if one is not already there.

    Headless (`--headless=new`) and quiet: this runs inside cron. It uses the
    configured profile, so the session is the principal's — and it will refuse to
    start if that profile is locked by a Chrome the principal is using, which is
    the honest failure (the caller falls back to the open-web providers).
    """
    port = _debug_port()
    version = _endpoint(port)
    if version is not None:
        return version
    binary = chrome_binary()
    profile = profile_dir() if session else public_profile()
    if binary is None or profile is None:
        return None
    args = [
        binary, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync",
        "--disable-blink-features=AutomationControlled", "--window-size=1365,768",
        "about:blank",
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        logger.warning("[browser] could not start chrome: %s", exc)
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        version = _endpoint(port)
        if version is not None:
            return version
        time.sleep(0.5)
    logger.warning("[browser] chrome did not open a debug port within %ss", timeout)
    return None


async def _read_page(ws_url: str, url: str, *, settle: float = SETTLE_SECONDS) -> str:
    """Navigate one tab and return the rendered HTML. Read-only."""
    import websockets

    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as socket:
        counter = {"id": 0}

        async def call(method: str, params: dict | None = None) -> dict:
            counter["id"] += 1
            message = {"id": counter["id"], "method": method, "params": params or {}}
            await socket.send(json.dumps(message))
            while True:
                reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=45))
                if reply.get("id") == counter["id"]:
                    return reply

        await call("Page.enable")
        await call("Page.navigate", {"url": url})
        # Wait for the load event, then for the *content*: a page that paints after
        # load (every single-page app) reads as a skeleton otherwise, and a fixed
        # sleep is a guess. Poll the rendered text until it stops growing — the
        # first version slept 3 s and returned 51 characters on one run and 2,466
        # on the next (measured 2026-10-14).
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            except asyncio.TimeoutError:
                break
            if event.get("method") == "Page.loadEventFired":
                break
        previous, stable = -1, 0
        while time.monotonic() < deadline:
            await asyncio.sleep(max(0.5, settle / 3))
            probe = await call("Runtime.evaluate",
                               {"expression": "document.body ? document.body.innerText.length : 0",
                                "returnByValue": True})
            length = int(probe.get("result", {}).get("result", {}).get("value") or 0)
            if length > 0 and length == previous:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            previous = length
        # **Chrome's own error page is not the page.** A DNS failure, a refused
        # connection or a TLS problem renders a *long* document saying so, and
        # reading it would turn "we could not reach it" into a page we can quote —
        # the check then judges it as a live page that does not carry the role
        # (found 2026-10-14, when a test that expected an unreachable advert began
        # reporting `live_unmatched`). `chrome-error://` is the browser telling us
        # it never arrived.
        landed = await call("Runtime.evaluate",
                            {"expression": "document.location.href",
                             "returnByValue": True})
        landed_url = str(landed.get("result", {}).get("result", {}).get("value") or "")
        if landed_url.startswith("chrome-error://"):
            logger.info("[browser] %s landed on Chrome's error page", url)
            return ""
        result = await call("Runtime.evaluate",
                            {"expression": "document.documentElement.outerHTML",
                             "returnByValue": True})
        return str(result.get("result", {}).get("result", {}).get("value") or "")


def _html_to_linked_text(html: str) -> str:
    """Page text with every anchor kept as `[text](href)`.

    **The reason this exists** (2026-10-15): a JavaScript-only careers site hands
    the open web its *text* — `Enigma - Jobs` came back as 3,332 characters with the
    role titles and **no links at all**, because the links are anchors and the
    markdown conversion drops them. So we kept the titles and lost the hrefs, and
    LJ's first GO row read `(no advert link)`. The rendered DOM still has the
    anchors, so this keeps them, and the extractor can associate a title with the
    link beside it.
    """
    text = re.sub(r"<script[^>]*>.*?</script>", "", html or "", flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)

    def _anchor(match: "re.Match") -> str:
        href = match.group(1).strip()
        label = re.sub(r"<[^>]+>", " ", match.group(2))
        label = re.sub(r"\s+", " ", unescape(label)).strip()
        if not href or href.startswith(("javascript:", "#")):
            return label
        return f"[{label}]({href})" if label else f"({href})"

    text = re.sub(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", _anchor,
                  text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_linked_text(url: str, *, session: bool = True) -> str:
    """The rendered page with its links preserved — "" when we did not arrive."""
    usable, why = available(session=session)
    if not usable:
        logger.info("[browser] unavailable: %s", why)
        return ""
    html = fetch_html(url, session=session)
    return _html_to_linked_text(html) if html else ""


def fetch_html(url: str, *, session: bool = True) -> str:
    """The rendered DOM, or "" — the raw material for link-preserving reads."""
    usable, why = available(session=session)
    if not usable:
        logger.info("[browser] unavailable: %s", why)
        return ""
    version = ensure_chrome(session=session)
    if version is None:
        return ""
    port = _debug_port()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read())
        html = asyncio.run(_read_page(target["webSocketDebuggerUrl"], url))
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/json/close/{target['id']}",
                                       method="PUT"), timeout=5).read()
        except Exception:
            pass
        return html
    except Exception as exc:
        logger.info("[browser] html fetch failed for %s: %s", url, exc)
        return ""


def _html_to_text(html: str) -> str:
    """The same tag-strip the HTTP provider uses: one converter for raw HTML."""
    from crawler import _http_fetch  # noqa: F401  (imported for the shared shape)

    text = unescape(html)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch(url: str, *, session: bool = True) -> str:
    """The page as a browser sees it, or "" — never an exception.

    `session=True` (the default) uses the profile that carries a login. Pass
    `session=False` for a **public** page: Chrome still needs a profile to start,
    but no credential is involved, so a page we could not read at all becomes
    readable (see `public_profile`).
    """
    usable, why = available(session=session)
    if not usable:
        logger.info("[browser] unavailable: %s", why)
        return ""
    version = ensure_chrome(session=session)
    if version is None:
        return ""
    port = _debug_port()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read())
        ws_url = target["webSocketDebuggerUrl"]
        html = asyncio.run(_read_page(ws_url, url))
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/json/close/{target['id']}",
                                       method="PUT"), timeout=5).read()
        except Exception:
            pass                      # a tab that will not close is not a failure
    except Exception as exc:
        logger.info("[browser] fetch failed for %s: %s", url, exc)
        return ""
    text = _html_to_text(html)
    if len(text) < 400:
        # A short answer from a page that should be long is a failed read, not a
        # short page: try once more before reporting nothing. (A genuinely small
        # page loses one extra fetch, and this provider is not on the fast path.)
        logger.info("[browser] %s came back short (%d chars) — retrying",
                    url, len(text))
        text = _retry(url) or text
    logger.info("[browser] fetched %s (%d chars)", url, len(text))
    return text


def _retry(url: str) -> str:
    """One more attempt through a fresh tab."""
    version = ensure_chrome()
    if version is None:
        return ""
    port = _debug_port()
    try:
        import urllib.parse

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read())
        html = asyncio.run(_read_page(target["webSocketDebuggerUrl"], url,
                                      settle=SETTLE_SECONDS * 2))
        return _html_to_text(html)
    except Exception:
        return ""


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(f"chrome: {chrome_binary()}")
    print(f"profile: {profile_dir()}")
    print(f"available: {available()}")
    text = fetch(args.url)
    print(f"fetched {len(text)} chars")
    print(text[:1500])


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by fetch's URL building)

    raise SystemExit(_cli())

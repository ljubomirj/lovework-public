#!/usr/bin/env python3
"""fetch_bench.py — the same adverts through every fetch provider, measured.

LJ, 2026-11-04: *"it's a good test, to see on our application — is some of those
web search web fetch providers better or worse? We have Firecrawl/Tavily/Exa and
even DeepSeek. Plus the personal Brave and or local Chrome as a last ditch
fallbacks?"*

`fetch_ledger.py` measures the chain's real traffic; this measures a **fair
comparison** of it: one URL set, every provider, no fallbacks and no cache, so the
answer is not "whatever the chain happened to try first that day".

    ../venv/bin/python3 fetch_bench.py                    # 8 adverts, all providers
    ../venv/bin/python3 fetch_bench.py --count 12 --providers tavily,deepseek,chrome
    ../venv/bin/python3 fetch_bench.py --urls https://… https://…

The URL set defaults to **our own adverts** — the newest packs' links, at most two
per host so one board cannot decide the result. Rows go to the same ledger the
chain writes (`run: "bench"`), so `fetch_ledger.py --run bench` reports on them
later; `--run-chain` includes the real traffic for context.

Cost: one paid call per (provider, URL). Eight URLs across five providers is 40
calls — cents at these prices, and it is the only way to answer the question.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

logger = logging.getLogger(__name__)

#: Everything we can fetch with. `exa` is absent on purpose: `crawler` blacklists
#: it (credits exhausted 2026-10-22), so a bench row for it would be a call billed
#: to a dead account. Add it back here only if the account is restored.
ALL_PROVIDERS = ("tavily", "deepseek", "firecrawl", "http", "chrome")


def _call(provider: str, url: str) -> tuple[str, str]:
    """Fetch one URL through one provider, no chain, no cache. Returns (text, error)."""
    import crawler

    # The provider functions report their own failure in `crawler._PROVIDER_ERRORS`
    # (they return "" rather than raising), so the bench reads it the same way the
    # chain does — otherwise a 402 row says nothing about why.
    crawler._PROVIDER_ERRORS.pop(provider, None)
    try:
        if provider == "tavily":
            text = crawler._tavily_fetch(url)
        elif provider == "deepseek":
            text = crawler._deepseek_fetch(url)
        elif provider == "firecrawl":
            text = crawler._firecrawl_fetch(url)
        elif provider == "http":
            text = crawler._http_fetch(url)
        elif provider == "chrome":
            import browser_fetch

            text = browser_fetch.fetch(url, session=False)
        elif provider == "exa":
            raise ValueError("exa is blacklisted in crawler.BLACKLISTED_PROVIDERS")
        else:
            raise ValueError(f"unknown provider {provider!r}")
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    text = text or ""
    return text, ("" if text else crawler._PROVIDER_ERRORS.get(provider, ""))


def advert_urls(count: int, *, state_dir: Path | None = None) -> list[str]:
    """Our own newest advert links — at most two per host, newest first."""
    from application_packs import pack_advert_url, pack_index

    state_dir = state_dir or config.STATE_DIR
    seen: dict[str, int] = {}
    out: list[str] = []
    for profile in config.principal_names(state_dir):
        try:
            packs = pack_index(profile, root=state_dir / profile / "applications")
        except Exception as exc:                   # a broken index is not fatal
            logger.warning("pack index for %s failed: %s", profile, exc)
            continue
        # `pack_index` maps advert identity → pack directory: the *values* are the
        # packs. Iterating the dict yields the hashes, which are not paths.
        for pack in sorted(packs.values(), key=lambda p: Path(p).name, reverse=True):
            url = pack_advert_url(Path(pack))
            if not url or not urlparse(url).scheme.startswith("http"):
                continue
            host = urlparse(url).netloc.lower()
            if seen.get(host, 0) >= 2:
                continue
            seen[host] = seen.get(host, 0) + 1
            if url not in out:
                out.append(url)
            if len(out) >= count:
                return out
    return out


def run(urls: list[str], providers: list[str]) -> list[dict]:
    import fetch_ledger

    rows: list[dict] = []
    for url in urls:
        print(f"\n{url}")
        for provider in providers:
            started = time.monotonic()
            text, error = _call(provider, url)
            row = fetch_ledger.record(provider, url, text,
                                      seconds=time.monotonic() - started,
                                      run="bench", error=error)
            rows.append(row)
            verdict = "ok " if row["ok"] else "—  "
            print(f"   {verdict} {provider:<10} {row['chars']:>7} chars  "
                  f"{row['seconds']:>6.2f}s"
                  + (f"  {row['error'][:70]}" if row["error"] else ""))
    return rows


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=8,
                        help="how many adverts from our own packs (default 8)")
    parser.add_argument("--providers", default=",".join(ALL_PROVIDERS),
                        help=f"comma-separated; known: {', '.join(ALL_PROVIDERS)}")
    parser.add_argument("--urls", nargs="*", default=None,
                        help="explicit URLs instead of our own adverts")
    parser.add_argument("--run-chain", action="store_true",
                        help="also report the chain's real traffic in the summary")
    args = parser.parse_args(argv)

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    for name in providers:
        if name not in ALL_PROVIDERS:
            print(f"unknown provider {name!r}; known: {', '.join(ALL_PROVIDERS)}")
            return 64

    urls = args.urls or advert_urls(args.count)
    if not urls:
        print("No advert links found in the pack index — pass --urls explicitly.")
        return 1

    print(f"Fetch bench — {len(urls)} URL(s) × {len(providers)} provider(s) "
          f"= {len(urls) * len(providers)} calls")
    import fetch_ledger

    rows = run(urls, providers)
    print("\n" + fetch_ledger.report(days=1, run="bench"))
    if args.run_chain:
        print("\n" + fetch_ledger.report(days=7, run="chain"))
    ok = sum(1 for r in rows if r["ok"])
    print(f"\n{ok} of {len(rows)} attempts returned usable evidence "
          f"(={ok}/{len(rows)}).")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(_main())

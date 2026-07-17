#!/usr/bin/env python3
"""
LoveWork — interactive agent CLI.

Usage:
    python -m lovework_agent --profile lj --role general
    python -m lovework_agent --profile vj --role platform-sre --query "Find me UK-based SRE roles"
    python -m lovework_agent --autonomous --profile lj --role general  # same as today's cron

Starts a REPL where you can ask the agent questions about jobs.
Type 'quit' to exit.
"""

import argparse
import logging
import sys

import config
from agent import run_autonomous
from agent_runtime import TauRuntimeUnavailable, build_agent_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("lovework-agent")


def main():
    parser = argparse.ArgumentParser(description="LoveWork — personal job discovery agent")
    parser.add_argument("--profile", default="lj", choices=["lj", "vj"], help="Candidate profile")
    parser.add_argument("--role", default=None, help="Role file under profiles/<name>/roles/")
    parser.add_argument("--query", default=None, help="Single query (non-interactive)")
    parser.add_argument("--autonomous", action="store_true", help="Run the full pipeline (cron mode)")
    parser.add_argument("--source", default="all", help="Source for autonomous mode")
    parser.add_argument(
        "--runtime",
        default="local",
        choices=["local", "tau"],
        help="Interactive agent runtime. 'local' uses the existing implementation; 'tau' is a future pinned harness backend.",
    )
    args = parser.parse_args()

    if args.autonomous:
        role = args.role or "general"
        run_autonomous(args.profile, role, source=args.source)
        return

    role = args.role or "general"
    try:
        runtime = build_agent_runtime(args.runtime)
    except TauRuntimeUnavailable as e:
        print(f"Runtime unavailable: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"LoveWork agent ready (profile: {args.profile}, role: {role}, runtime: {args.runtime})")
    print("Type a question, or 'quit' to exit.")
    print()

    if args.query:
        run = runtime.run_task(args.query, profile_name=args.profile, role=role)
        print(run.output if run.status == "succeeded" else f"ERROR: {run.error}")
        return

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break
        run = runtime.run_task(user_input, profile_name=args.profile, role=role)
        print(run.output if run.status == "succeeded" else f"ERROR: {run.error}")
        print()


if __name__ == "__main__":
    main()

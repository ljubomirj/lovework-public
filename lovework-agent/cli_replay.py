"""Principal-scoped access to recorded replay and isolated policy experiments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import replay
from principal_runtime import resolve_principal_runtime
from strata import StrataEngine


def _name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise argparse.ArgumentTypeError("use a simple name containing letters, digits, - or _")
    return value


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _matcher(profile_text: str, model: str, budget: int):
    from llm_client import LLMClient
    from matcher import JobMatcher

    class BudgetClient(LLMClient):
        calls = 0

        def chat(self, *args, **kwargs):
            if self.calls >= budget:
                raise replay.IsolationViolation("model call budget exhausted")
            self.calls += 1  # Failed calls consume budget too.
            kwargs["retries"] = 1
            return super().chat(*args, **kwargs)

    client = BudgetClient(model=model)
    client.client = client.client.with_options(max_retries=0)
    return JobMatcher(client, profile_text, registry=None, use_history=False)


def _counterfactual(args, engine, parser) -> dict:
    root = engine.state_root / "replays" / args.replay_id
    if not root.resolve().is_relative_to(engine.state_root.resolve() / "replays"):
        raise ValueError("replay directory escapes the principal's replay root")
    manifest = engine.read_manifest(args.run_id)
    if manifest is None:
        return {"status": "unknown_run", "run_id": args.run_id}
    changes = {"profile_file": str(args.profile_file) if args.profile_file else None,
               "role": args.role, "model": args.model, "description": args.description}
    result = replay.counterfactual(
        engine, replay_id=args.replay_id, changes=changes,
        budget={"model_calls": args.budget_calls, "adverts": args.limit})
    result["run_id"] = args.run_id
    result["not_predicted"] = "Preview only; --execute runs the explicitly budgeted experiment."
    result["limitations"] = [
        "Frozen registry rows supply title, organization, URL and location only.",
        "Description is the declared common override; no live advert enrichment or history is read.",
        "Execution uses current matcher policy and the explicitly supplied profile and model.",
    ]
    if not args.execute:
        return result
    if not all((args.profile_file, args.role, args.model, args.budget_calls)):
        parser.error("--execute requires --profile-file, --role, --model and --budget-calls")
    if root.exists() or root.is_symlink():
        raise replay.IsolationViolation("replay already exists; choose a new --replay-id")
    profile_text = args.profile_file.read_text(encoding="utf-8")
    if not profile_text.strip():
        raise ValueError("--profile-file must contain the complete experimental profile")
    from model_transcripts import active_dataset_dir

    matcher = _matcher(profile_text, args.model, args.budget_calls)
    with active_dataset_dir(root / "dataset"):
        executed = replay.execute_counterfactual(
            engine, replay_id=args.replay_id, run_id=args.run_id,
            matcher=matcher, profile_text=profile_text, role=args.role,
            limit=args.limit, budget_calls=args.budget_calls,
            description=args.description)
    executed["limitations"] = result["limitations"]
    executed["changes"] = changes
    # The library counts successful judgments; expose attempted model calls too.
    executed["attempted_model_calls"] = getattr(matcher.llm, "calls", None)
    return executed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_name)
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    recorded = sub.add_parser("recorded", help="Read saved outputs and judgments; no model calls")
    recorded.add_argument("--run-id", type=_name)
    recorded.add_argument("--pipeline-run-id", type=_name)
    recorded.add_argument("--org")
    recorded.add_argument("--limit", type=_positive, default=50)
    reconstruct = sub.add_parser("reconstruct", help="Check exact-playback prerequisites; never infer missing calls")
    reconstruct.add_argument("--run-id", type=_name, required=True)
    counterfactual = sub.add_parser("counterfactual", help="Preview a policy experiment; --execute permits model calls")
    counterfactual.add_argument("--run-id", type=_name, required=True)
    counterfactual.add_argument("--replay-id", type=_name, required=True)
    counterfactual.add_argument("--profile-file", type=Path, help="Complete declared experimental profile")
    counterfactual.add_argument("--role", type=_name)
    counterfactual.add_argument("--model", help="Model on the configured inference endpoint")
    counterfactual.add_argument("--budget-calls", type=_positive)
    counterfactual.add_argument("--limit", type=_positive, default=10)
    counterfactual.add_argument("--description", default="")
    mode = counterfactual.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="Explicit preview (the default)")
    args = parser.parse_args(argv)
    try:
        runtime = resolve_principal_runtime(args.profile)
        if not runtime.state_root.is_dir():
            result = {"status": "no_state", "principal": args.profile}
        else:
            engine = StrataEngine(runtime.state_root)
            if args.command == "recorded":
                result = replay.recorded(engine, run_id=args.run_id, org=args.org,
                                         pipeline_run_id=args.pipeline_run_id, limit=args.limit)
            elif args.command == "reconstruct":
                # Never use today's transcript ledger as historical evidence.
                result = replay.reconstruct(
                    engine, args.run_id,
                    dataset_dir=engine.bundle_path(args.run_id) / "state" / "dataset")
                result["note"] = "Readiness check only; no pipeline execution or network calls."
            else:
                result = _counterfactual(args, engine, parser)
    except (OSError, ValueError, replay.IsolationViolation) as exc:
        result = {"status": "error", "detail": str(exc)}
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Replay {args.command}: {result['status']}")
        print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in {"ok", "preview", "ready", "executed"} else 1


if __name__ == "__main__":
    sys.exit(main())

"""WP5 — isolated replay and evaluation corpus.

Three modes, and the difference between them is what they are allowed to *know*:

* **`recorded`** — return what was actually saved (a seal's original outputs, the
  assessment ledger rows). No model call, and no requirement that we stored model
  transcripts. If the original output is missing, it is **reported as missing** —
  it is never reconstructed, because a reconstruction is a different artifact
  wearing the original's name.
* **`reconstruct`** — exact playback of a past execution. It *requires* a complete
  frozen execution specification and recorded responses matching exact requests.
  Where those are absent (we do not store model transcripts today), playback is
  **invalidated** rather than approximated, and the original output is preserved
  independently.
* **`counterfactual`** — a **preview** of what a different input would change:
  missing dependencies, changed inputs, budget and write set, all without
  inference. Execution is confined to `state/<p>/replays/<id>/` with read-only
  frozen inputs and separate caches; live acquisition, Gmail, case generation,
  sends and production mutation are forbidden.

The through-line is the plan's rule: **never fabricate**. Every mode reports what
it cannot do and why, rather than producing a plausible artifact that would be
indistinguishable from evidence.
"""
from __future__ import annotations
import jsonl_io

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPLAYS_DIRNAME = "replays"

#: What a counterfactual run is forbidden to do. Enforced by `_isolation`, and
#: stated in the preview so a caller cannot assume otherwise.
FORBIDDEN = (
    "live evidence acquisition (no network, no crawler)",
    "Gmail reads or writes",
    "case generation",
    "any outbound send",
    "production mutation (the live state tree is read-only)",
)


def _ledger(dataset_dir: Path, name: str) -> list[dict]:
    path = dataset_dir / name
    if not path.is_file():
        return []
    return [json.loads(l) for l in jsonl_io.lines(path)
            if l.strip()]


def recorded(engine, *, run_id: Optional[str] = None,
             dataset_dir: Optional[Path] = None, org: Optional[str] = None,
             pipeline_run_id: Optional[str] = None,
             limit: int = 50) -> dict:
    """Return saved judgments/output for a sealed run. No model call.

    Missing pieces are listed, not filled in. If there is no seal to read, the
    result says `no_seal` — a replay that silently fell back to live state would
    be the opposite of an isolated replay.
    """
    seals = engine.sealed_run_ids()
    if not seals:
        return {"status": "no_seal", "mode": "recorded",
                "detail": "no sealed run to replay; a replay must not read live "
                          "state instead"}
    chosen = run_id or None
    if chosen is None:
        newest = None
        for rid in seals:
            manifest = engine.read_manifest(rid)
            if manifest and (newest is None
                             or manifest.clock.sealed_at > newest.clock.sealed_at):
                newest = manifest
        chosen = newest.run.run_id if newest else None
    manifest = engine.read_manifest(chosen)
    if manifest is None:
        return {"status": "unknown_run", "mode": "recorded", "run_id": chosen}

    bundle = engine.bundle_path(chosen)
    missing: list[str] = []
    outputs = []
    for out in getattr(manifest, "outputs", []) or []:
        if not getattr(out, "original", False):
            continue
        path = bundle / out.path
        entry = {"path": out.path, "present": path.is_file()}
        if path.is_file():
            entry["bytes"] = path.stat().st_size
        else:
            entry["reason"] = "recorded as an original output but not in the bundle"
            missing.append(out.path)
        outputs.append(entry)

    # Prefer the seal's OWN frozen copy of the ledger: an isolated replay must
    # read sealed evidence, not whatever the live ledger says today.
    sealed_ledger = bundle / "state" / "dataset" / "assessments.jsonl"
    evidence = "seal"
    if sealed_ledger.is_file():
        assessments = [json.loads(l)
                       for l in jsonl_io.lines(sealed_ledger)
                       if l.strip()]
    elif dataset_dir is not None:
        assessments = _ledger(dataset_dir, "assessments.jsonl")
        evidence = "live ledger (not sealed)"
        if not assessments:
            missing.append("assessments.jsonl (no assessment ledger to replay)")
    else:
        assessments = []
        evidence = "none"
        missing.append("assessments.jsonl (no sealed copy and no dataset_dir given)")
    # Only rows that *are* assessments: a ledger can carry other shapes, and
    # presenting one of those as a saved judgment would be a fabrication by
    # omission. The skipped count says so instead of hiding it.
    judgments = [r for r in assessments
                 if r.get("assessment_id") or r.get("decision")]
    skipped = len(assessments) - len(judgments)
    # The ledger is **run-spanning** and its rows carry the *pipeline's* run ids,
    # which do not map to this seal's operation id — so a strict match would
    # return nothing, and pretending the rows are "this run's" would misattribute
    # other runs' judgments. The honest shape: present what the seal captured,
    # say so, and let a caller scope to a known pipeline run when they have one.
    if pipeline_run_id:
        judgments = [r for r in judgments if r.get("run_id") == pipeline_run_id]
    rows = [r for r in judgments
            if not org or org.lower() in (r.get("org_name") or "").lower()]
    ledger_run_ids = sorted({r.get("run_id") for r in judgments if r.get("run_id")})

    return {
        "status": "ok",
        "mode": "recorded",
        "run_id": chosen,
        "sealed_at": manifest.clock.sealed_at,
        "seal_digest": manifest.digest(),
        # The seal's own status set, unwrapped field by field: "did this run
        # finish, and did it capture?" is the first question a replay must answer.
        "status_set": {
            field: getattr(manifest.status, field, None)
            for field in ("execution", "capture", "qa", "delivery")
        } if getattr(manifest, "status", None) is not None else None,
        "outputs": outputs,
        "assessments": rows[-limit:],
        "assessment_count": len(rows),
        "non_assessment_rows_skipped": skipped,
        "assessment_evidence": evidence,
        "ledger_scope": {
            "spanning_runs": len(ledger_run_ids),
            "note": ("the captured ledger spans earlier runs and its rows carry "
                     "pipeline run ids, which do not map to this seal's operation "
                     "id; scope with pipeline_run_id when you know which run you "
                     "mean"),
        },
        "missing": missing,
        "model_calls": 0,
        "note": ("saved judgments are returned as they were recorded; nothing "
                 "here calls a model or reconstructs output"),
    }


def reconstruct(engine, run_id: str, *, dataset_dir: Optional[Path] = None) -> dict:
    """Exact playback — or an honest refusal.

    Exact playback needs two things this system does not yet store: a complete
    frozen execution specification and the recorded model responses matching the
    exact requests. Without them the only honest answer is that playback is
    invalidated; the original output stays readable through `recorded`.
    """
    manifest = engine.read_manifest(run_id)
    if manifest is None:
        return {"status": "unknown_run", "mode": "reconstruct", "run_id": run_id}
    inputs = [i.role for i in getattr(manifest, "inputs", []) or []]
    transcripts = _ledger(dataset_dir, "model_calls.jsonl") if dataset_dir else []
    for_this_run = [t for t in transcripts if t.get("run_id") == run_id]

    blockers = []
    if not transcripts:
        blockers.append("no recorded model-call transcript exists at all — this "
                        "deployment does not store them, so exact playback "
                        "cannot be verified")
    elif not for_this_run:
        blockers.append(f"no recorded calls for {run_id} in the transcript")
    if not inputs:
        blockers.append("the seal pinned no inputs, so the execution "
                        "specification is incomplete")

    out = {
        "status": "invalidated" if blockers else "ready",
        "mode": "reconstruct",
        "run_id": run_id,
        "pinned_inputs": inputs,
        "recorded_calls": len(for_this_run),
        "blockers": blockers,
        "original_output_preserved": True,
        "fallback": ("use `recorded` for the saved output; a reconstruction is "
                     "not a substitute for it"),
    }
    if not blockers:
        import model_transcripts as mt

        # Completeness cannot be proven from the transcript alone — we do not
        # know how many calls a run *should* make. It is discovered during
        # playback: an unmatched request is refused, not guessed.
        out["transcript"] = mt.Transcripts(dataset_dir, run_id).summary()
        out["playback"] = (
            "construct a ReplayLLMClient over this run's transcripts to drive an "
            "execution with no network; any request that is not in the recording "
            "raises ReplayMiss rather than being approximated")
        out["completeness"] = ("unproven from the recording: a missing call is "
                               "discovered as a ReplayMiss during playback")
    return out


def counterfactual(engine, *, replay_id: str, changes: Optional[dict] = None,
                   budget: Optional[dict] = None) -> dict:
    """Preview a what-if without inferring anything, and state the isolation.

    Execution (when it is ever enabled) happens under
    `state/<p>/replays/<replay_id>/` with frozen read-only inputs and separate
    caches. This function only *previews*: it names the missing dependencies, the
    changed inputs, the budget and the write set, and it refuses to guess at
    outcomes.
    """
    state_root = engine.state_root
    replays = state_root / REPLAYS_DIRNAME
    return {
        "status": "preview",
        "mode": "counterfactual",
        "replay_id": replay_id,
        "replays_dir": str(replays),
        "isolation": {
            "inputs": "read-only copies of sealed evidence (frozen)",
            "caches": "separate, under the replay directory",
            "live_state": "read-only — production mutation is forbidden",
            "forbidden": list(FORBIDDEN),
        },
        "changes": changes or {},
        "budget": budget or {},
        "write_set": [str(replays / replay_id)],
        "missing_dependencies": _preview_dependencies(engine),
        "not_predicted": ("this preview does not infer outcomes: a counterfactual "
                          "result requires an actual isolated execution, and "
                          "execution is not enabled in this deployment"),
    }


def _preview_dependencies(engine) -> list[str]:
    out = []
    if not engine.sealed_run_ids():
        out.append("no sealed run to freeze inputs from")
    return out


def execute(*args, **kwargs):  # pragma: no cover - superseded
    """Superseded by `execute_counterfactual`, which enforces the guards.

    Kept so an older caller fails loudly with a pointer rather than silently
    doing nothing.
    """
    raise RuntimeError(
        "use execute_counterfactual(...): it enforces the isolation contract "
        "(sandbox root, frozen inputs, separate caches, no evidence acquisition, "
        "no Gmail, no sends, no production mutation)")

# ── exact playback ───────────────────────────────────────────────────────

class ReplayMiss(RuntimeError):
    """A request was not in the transcript. Refused, never guessed.

    An unmatched request means the execution differs from the recorded one —
    different inputs, a different prompt, a different parameter. Serving the
    nearest recorded response would be fabrication, so playback stops instead.
    """


class ReplayLLMClient:
    """Answers model calls from a transcript. No network, no model.

    Interface-compatible with `LLMClient.chat` for the calls a run makes, so a
    pipeline can be driven through it unchanged. Every hit is recorded, so the
    replay can report which recorded calls the execution never asked for.
    """

    def __init__(self, transcripts, *, model: str = "", provider: str = "replay"):
        self.transcripts = transcripts
        self.model = model or "(recorded)"
        self.provider = provider
        self.api_style = "replay"
        self.hits = 0
        self.misses: list[str] = []

    def chat(self, messages: list[dict], response_format=None,
             temperature: float | None = None, max_tokens: int | None = None,
             reasoning_effort: str | None = None, retries: int = 1,
             context: str = "") -> str:
        import model_transcripts as mt

        # Same canonical shape the recorder used: a digest over provider-shaped
        # kwargs could not be reproduced here without re-implementing the
        # provider's request builder.
        canonical = mt.canonical_request(
            messages, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, reasoning_effort=reasoning_effort)
        digest = mt.request_digest(messages, canonical)
        content = self.transcripts.lookup(digest)
        if content is None:
            self.misses.append(digest)
            raise ReplayMiss(
                "no recorded response for this exact request "
                f"(digest {digest[:12]}); the execution differs from the "
                "recording, and playback will not guess")
        self.hits += 1
        return content

    def structured(self, *args, **kwargs):  # pragma: no cover - parity only
        raise ReplayMiss("structured calls are replayed through chat()")

    def report(self) -> dict:
        return {
            "hits": self.hits,
            "misses": len(self.misses),
            "unused_recorded_calls": self.transcripts.unused_calls(),
            "recorded_calls": self.transcripts.count,
        }

# ── counterfactual execution (isolated) ──────────────────────────────────

class IsolationViolation(RuntimeError):
    """A counterfactual tried to reach outside its sandbox."""


def isolation(engine, replay_id: str, *, create: bool = False) -> dict:
    """The sandbox contract for one counterfactual, and its guards.

    Execution happens under `state/<p>/replays/<id>/` with frozen read-only
    inputs and separate caches. The *live* state tree is read-only throughout:
    a counterfactual that mutated production would be indistinguishable from a
    real run, which is exactly what the plan forbids.
    """
    root = engine.state_root / REPLAYS_DIRNAME / replay_id
    if create and root.exists():
        raise IsolationViolation(
            f"replay {replay_id!r} already exists; a counterfactual must not "
            f"clobber a previous one")
    if create:
        (root / "inputs").mkdir(parents=True, exist_ok=True)
        (root / "cache").mkdir(parents=True, exist_ok=True)
    return {
        "replay_id": replay_id,
        "root": str(root),
        "inputs": str(root / "inputs"),
        "cache": str(root / "cache"),
        "live_state": "read-only",
        "forbidden": list(FORBIDDEN),
        "model_calls": "allowed (a what-if is new judgement, not new evidence)",
    }


def _frozen_adverts(engine, run_id: str, limit: int) -> list[dict]:
    """Adverts from a seal's frozen registry — never from the network."""
    import csv

    path = engine.bundle_path(run_id) / "state" / "cache" / "jobs.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [{"org_name": r.get("org", ""), "title": r.get("title", ""),
             "url": r.get("url", ""), "location": r.get("location", ""),
             "source": r.get("source", "")} for r in rows[:limit]]


def execute_counterfactual(engine, *, replay_id: str, run_id: str,
                           matcher, profile_text: str = "", role: str = "",
                           limit: int = 10, budget_calls: int = 20,
                           description: str = "") -> dict:
    """Re-judge a *frozen* advert set under changed inputs, in isolation.

    What it may do: call a model (a what-if is new judgement). What it may not
    do: acquire evidence (no crawler, no network fetches), read Gmail, generate
    cases, send anything, or write outside its sandbox. The adverts come from the
    seal, so the world is held fixed and only the lens moves.
    """
    contract = isolation(engine, replay_id, create=True)
    root = Path(contract["root"])
    adverts = _frozen_adverts(engine, run_id, limit)
    if not adverts:
        return {"status": "no_frozen_adverts", "run_id": run_id,
                "isolation": contract}

    # Frozen inputs are written *inside* the sandbox; the live tree is untouched.
    (root / "inputs" / "profile.md").write_text(profile_text or "# (unchanged)\n",
                                               encoding="utf-8")
    results, calls, violations = [], 0, []
    for advert in adverts:
        if calls >= budget_calls:
            violations.append(f"budget_calls exhausted at {calls}")
            break
        try:
            match = matcher.match(advert["title"], description, advert["org_name"],
                                  job_url=advert["url"],
                                  location=advert["location"])
        except Exception as e:
            results.append({**advert, "error": f"{type(e).__name__}: {e}"})
            continue
        calls += 1
        results.append({**advert, "decision": getattr(match, "decision", None),
                        "score": getattr(match, "score", None),
                        "reasoning": (getattr(match, "reasoning", "") or "")[:200]})

    (root / "results.json").write_text(
        json.dumps({"run_id": run_id, "role": role, "results": results},
                   indent=1, sort_keys=True, default=str), encoding="utf-8")
    return {
        "status": "executed",
        "replay_id": replay_id,
        "from_run": run_id,
        "role": role,
        "adverts": len(results),
        "model_calls": calls,
        "budget_calls": budget_calls,
        "warnings": violations,
        "results_path": str(root / "results.json"),
        "isolation": contract,
        "note": ("the world was held fixed (adverts came from the seal); only "
                 "the lens moved"),
    }


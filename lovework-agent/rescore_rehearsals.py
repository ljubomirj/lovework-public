#!/usr/bin/env python3
"""Re-score existing rehearsal transcripts with the LLM violation classifier.

After the 2026-07-30 review found that the heuristic violation classifier
was producing unreliable counts (1/0/7/9/0 on 5 identical-config runs),
the classifier was replaced with an LLM-based check. The 5 committed
rehearsal transcripts still carry the OLD heuristic's verdicts in their
candidate-report.md and rehearsal-report.md files.

This script re-runs ONLY the violation analysis on the existing
transcript bodies (no new conversation, no new interviewer/candidate
LLM calls — just the per-assertion audit). It writes:

  <rehearsal-dir>/candidate-report.md.llm-rescored    — new report
  <rehearsal-dir>/rehearsal-report.md.llm-rescored    — new verdict
  <rehearsal-dir>/rescore-summary.json                — old vs new counts

The original files are NOT overwritten — they remain as the historic
record of what the heuristic said. The .llm-rescored sidecars are the
trustworthy verdicts.

Usage::

    ../venv/bin/python3 rescore_rehearsals.py \\
        --case ../state/lj/applications/20260723-Hyperspell-Product_Engineer-LoveWork-ATA/ \\
        [--rehearsal-dir NAME]  # limit to one (default: all)

"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Resolve project root for imports
_AGENT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _AGENT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from interview_providers.candidate_agent import CandidateAgent


def _load_claims(case_dir: Path) -> list[dict[str, Any]]:
    """Load claims.json from the case dir."""
    claims_path = case_dir / "agent-interview" / "claims.json"
    if not claims_path.exists():
        print(f"  ⚠ no claims.json at {claims_path}", file=sys.stderr)
        return []
    return json.loads(claims_path.read_text(encoding="utf-8"))


def _load_evidence_pack(case_dir: Path) -> str:
    ep = case_dir / "agent-interview" / "evidence-pack.md"
    return ep.read_text(encoding="utf-8") if ep.exists() else ""


def _extract_candidate_responses(transcript_path: Path) -> list[dict[str, Any]]:
    """Pull (question, response) pairs from transcript.jsonl.

    Each candidate message is paired with the most recent preceding
    interviewer message as the 'question' context.
    """
    pairs: list[dict[str, Any]] = []
    last_interviewer_msg = ""
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role", "")
        content = entry.get("content", "")
        stage = entry.get("stage")
        if role == "interviewer":
            last_interviewer_msg = content
        elif role == "candidate":
            pairs.append({
                "question": last_interviewer_msg,
                "response": content,
                "stage": stage,
                "classification": "permitted",  # we don't know — treat all as auditable
            })
    return pairs


def _read_old_counts(rehearsal_dir: Path) -> dict[str, int]:
    """Read the old heuristic verdict from the existing reports."""
    old = {"violations": -1, "refusals": -1}
    cr = rehearsal_dir / "candidate-report.md"
    if cr.exists():
        text = cr.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "Violations" in line and ":" in line:
                try:
                    old["violations"] = int(line.split(":")[-1].strip().rstrip("."))
                except ValueError:
                    pass
    rr = rehearsal_dir / "rehearsal-report.md"
    if rr.exists():
        text = rr.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "Total violations" in line and ":" in line:
                try:
                    old["violations"] = int(line.split(":")[-1].strip().rstrip(","))
                except ValueError:
                    pass
            if "Total refusals" in line and ":" in line:
                try:
                    old["refusals"] = int(line.split(":")[-1].strip().rstrip(","))
                except ValueError:
                    pass
    return old


def _write_rescored_reports(
    rehearsal_dir: Path,
    agent: CandidateAgent,
    violations: list[dict[str, Any]],
    candidate_response_count: int,
    old_counts: dict[str, int],
) -> None:
    """Write the new sidecar reports."""
    # candidate-report.md.llm-rescored
    lines = [
        "# Candidate Agent Report (LLM-rescored 2026-07-30)",
        "",
        f"**Principal:** {agent._principal}",
        f"**Candidate responses analysed:** {candidate_response_count}",
        f"**Refusals (recorded during run, unchanged):** {len(agent._refusals)}",
        f"**Violations (LLM-classifier):** {len(violations)}",
        f"**Violations (old heuristic, for comparison):** {old_counts.get('violations', '?')}",
        "",
    ]
    if violations:
        lines.append("## ⚠️ Violations (LLM-detected)")
        for v in violations:
            lines.append(f"- **Question:** {v.get('question', '')[:100]}")
            lines.append(f"  **Violated:** {v.get('violated_claim', '')}")
            lines.append(f"  **Evidence:** {', '.join(v.get('matched_tokens', []))[:120]}")
            lines.append(f"  **Response preview:** {v.get('response_preview', '')[:120]}")
            lines.append("")
    else:
        lines.append("## ✅ No violations detected by LLM classifier")
        lines.append("")
    (rehearsal_dir / "candidate-report.md.llm-rescored").write_text(
        "\n".join(lines), encoding="utf-8"
    )

    # rehearsal-report.md.llm-rescored
    verdict = "✅ CLEAN" if not violations else f"⚠️ {len(violations)} VIOLATION(S)"
    lines = [
        "# Rehearsal Report (LLM-rescored 2026-07-30)",
        "",
        f"**Original heuristic verdict:** "
        f"{old_counts.get('violations', '?')} violations",
        f"**LLM classifier verdict:** {len(violations)} violations",
        "",
        f"## Verdict",
        "",
        f"**{verdict}** (per LLM classifier)",
        "",
        "The original heuristic produced false positives on generic token "
        "matches (e.g. 'location' triggering US-work-auth). The LLM "
        "classifier distinguishes assertion from reference. See "
        "candidate-report.md.llm-rescored for details.",
        "",
    ]
    (rehearsal_dir / "rehearsal-report.md.llm-rescored").write_text(
        "\n".join(lines), encoding="utf-8"
    )

    # rescore-summary.json
    summary = {
        "rescored_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "method": "llm-classifier",
        "old_heuristic_violations": old_counts.get("violations"),
        "old_heuristic_refusals": old_counts.get("refusals"),
        "new_llm_violations": len(violations),
        "candidate_responses_analysed": candidate_response_count,
        "violations": violations,
    }
    (rehearsal_dir / "rescore-summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )


def rescore_one(case_dir: Path, rehearsal_dir: Path) -> dict[str, Any]:
    """Re-score a single rehearsal. Returns the summary dict."""
    print(f"\n━━━ {rehearsal_dir.name} ━━━")

    transcript_path = rehearsal_dir / "transcript.jsonl"
    if not transcript_path.exists():
        print(f"  ⚠ no transcript.jsonl — skipping")
        return {"skipped": True, "reason": "no transcript"}

    pairs = _extract_candidate_responses(transcript_path)
    print(f"  candidate responses: {len(pairs)}")
    if not pairs:
        print(f"  ⚠ no candidate responses — skipping")
        return {"skipped": True, "reason": "no candidate responses"}

    old_counts = _read_old_counts(rehearsal_dir)
    print(f"  old heuristic: {old_counts.get('violations', '?')} violations")

    claims = _load_claims(case_dir)
    evidence_pack = _load_evidence_pack(case_dir)

    # Build agent with the REAL LLM client so the LLM classifier path runs.
    # Lazy-import to avoid openai import-time network checks.
    from llm_client import LLMClient
    llm = LLMClient()
    agent = CandidateAgent(
        claims=claims,
        evidence_pack=evidence_pack,
        principal="lj",
        llm_client=llm,
    )
    agent._assertions = pairs  # inject the transcript's responses directly

    violations = agent.check_violations()
    print(f"  new LLM classifier: {len(violations)} violations")

    _write_rescored_reports(
        rehearsal_dir, agent, violations, len(pairs), old_counts
    )

    return {
        "old_violations": old_counts.get("violations"),
        "new_violations": len(violations),
        "responses": len(pairs),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-score rehearsal transcripts with the LLM classifier."
    )
    parser.add_argument(
        "--case", required=True, type=Path,
        help="Path to the ATA case directory",
    )
    parser.add_argument(
        "--rehearsal-dir", default=None,
        help="Limit to one rehearsal dir name (default: all)",
    )
    args = parser.parse_args()

    case_dir = args.case.resolve()
    rehearsals_root = case_dir / "agent-interview" / "rehearsals"
    if not rehearsals_root.exists():
        print(f"No rehearsals dir at {rehearsals_root}", file=sys.stderr)
        sys.exit(1)

    dirs = sorted(d for d in rehearsals_root.iterdir() if d.is_dir())
    if args.rehearsal_dir:
        dirs = [d for d in dirs if d.name == args.rehearsal_dir]
        if not dirs:
            print(f"No rehearsal dir matching '{args.rehearsal_dir}'", file=sys.stderr)
            sys.exit(1)

    print(f"Re-scoring {len(dirs)} rehearsal(s) in {rehearsals_root}")
    results = {}
    for d in dirs:
        results[d.name] = rescore_one(case_dir, d)

    print("\n━━━ Summary ━━━")
    print(f"{'Rehearsal':<40} {'Old':>5} {'New':>5} {'Responses':>10}")
    for name, r in results.items():
        if r.get("skipped"):
            print(f"{name:<40} {'SKIP':>5} {'':>5} {'':>10}  ({r.get('reason')})")
        else:
            print(f"{name:<40} {r['old_violations']:>5} {r['new_violations']:>5} {r['responses']:>10}")


if __name__ == "__main__":
    main()

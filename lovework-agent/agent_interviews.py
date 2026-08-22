"""Prepare principal-owned agent-to-agent (ATA) interview cases.

An ATA preparation pack is application material, but it is not evidence that
the interview has started.  The explicit marker below protects LoveWork's
history and outcome logic until the principal authorises the live interview.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import config
from interview_providers.superme import SuperMePublicAdapter

ATA_PREPARED_MARKER = "LoveWork ATA status: PREPARED — interview not started"
ATA_SCHEMA_VERSION = 1
SEPARATOR = "=" * 72


@dataclass(frozen=True)
class AgentRuntime:
    host: str
    hermes_profile: str
    messaging_platform: str
    messaging_account: str


DEFAULT_AGENT_RUNTIMES = (
    AgentRuntime("macbook2", "hermeo", "telegram", "hermeo_lj_bot"),
    AgentRuntime("gigul2", "hermel", "telegram", "hermel_lj_bot"),
)


@dataclass(frozen=True)
class PreparedInterviewCase:
    path: Path
    slug: str
    status: str
    manifest_path: Path


def _slug_part(value: str, max_length: int) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^A-Za-z0-9._-]+", "", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return (value or "x")[:max_length]


def ata_case_slug(when: date, company: str, position: str) -> str:
    return (
        f"{when.strftime('%Y%m%d')}-{_slug_part(company, 40)}-"
        f"{_slug_part(position, 60)}-LoveWork-ATA"
    )


def principal_applications_dir(principal: str) -> Path:
    """Return the principal-owned case root.

    ``state/<principal>/applications/`` is the authoritative RW home for
    LoveWork-created cases (``*-LoveWork*`` packs), mirrored as read-only
    symlinks in the parent repo's unified ``applications/`` view. The unified
    view is only a fallback for LJ when the state root is absent
    (pre-migration setup).
    """
    state_path = config.STATE_DIR / principal.lower() / "applications"
    if state_path.exists() or principal.lower() != "lj":
        return state_path
    return config.APPLICATIONS_DIR


def _current_host() -> str:
    return socket.gethostname().split(".", 1)[0].lower()


def _runtime_manifest() -> dict[str, object]:
    preferred = next(item for item in DEFAULT_AGENT_RUNTIMES if item.host == "macbook2")
    host = _current_host()
    prepared_by = next(
        (item for item in DEFAULT_AGENT_RUNTIMES if item.host == host),
        AgentRuntime(host, "TBD", "telegram", "TBD"),
    )
    return {
        "preferred": asdict(preferred),
        "allowed": [asdict(item) for item in DEFAULT_AGENT_RUNTIMES],
        "prepared_by": asdict(prepared_by),
        "rule": (
            "Resolve the Hermes profile at runtime; do not persist a token or "
            "assume the preparation host will conduct the interview."
        ),
    }


def _case_text(
    *,
    principal: str,
    company: str,
    position: str,
    adapter: SuperMePublicAdapter,
    created: date,
) -> str:
    runtime = _runtime_manifest()
    preferred = runtime["preferred"]
    allowed = runtime["allowed"]
    lines = [
        ATA_PREPARED_MARKER,
        "",
        SEPARATOR,
        "",
        "Agent-to-agent interview case identity",
        "",
        f"Created: {created.isoformat()}",
        f"Principal: {principal}",
        f"Company: {company}",
        f"Position: {position}",
        f"Provider: SuperMe ({adapter.provider})",
        f"Provider role ID: {adapter.role_id}",
        "Interview ID: NOT CREATED",
        "Application/interview state: preparation only; no account, interview, message, or submission created",
        "",
        SEPARATOR,
        "",
        "Runtime identity",
        "",
        (
            f"Preferred: {preferred['host']} / Hermes profile "
            f"{preferred['hermes_profile']} / Telegram {preferred['messaging_account']}"
        ),
        "Allowed alternatives:",
        *[
            (
                f"- {item['host']} / Hermes profile {item['hermes_profile']} / "
                f"Telegram {item['messaging_account']}"
            )
            for item in allowed
        ],
        "",
        "The active runtime must identify itself when the interview starts. Credentials",
        "remain host-local and are never stored in this application directory.",
        "",
        SEPARATOR,
        "",
        "Official role and protocol references",
        "",
        f"Role: {adapter.role_url}",
        f"Preparation guide: {adapter.prepare_url}",
        "Agent API specification: https://api.superme.ai/v3/agent/.well-known/spec",
        "Official Python SDK: https://github.com/superme-ai/superme-sdk",
        "",
        "SuperMe provides an authenticated REST API, Python SDK, MCP endpoint,",
        "interview state, transcripts, stage progression, dry-run support, and",
        "mock responses. This is therefore primarily an integration and governance",
        "problem, not a browser-driving problem.",
        "",
        "Fetched copies and hashes are under agent-interview/references/.",
        "",
        SEPARATOR,
        "",
        f"Phase 1 readiness finding for {company} — {position}",
        "",
        f"Company: {company}",
        f"Position: {position}",
        f"Provider: SuperMe ({adapter.provider})",
        f"Provider role ID: {adapter.role_id}",
        "",
        "This finding is generated from the role text and principal profile.",
        "Claims requiring attention are listed in the evidence pack.",
        "The interview should proceed only after the principal reviews viability.",
        "",
        "",
        SEPARATOR,
        "",
        "Consent boundary",
        "",
        "- [x] Public documentation may be fetched and inspected",
        "- [x] A local preparation/evidence pack may be created",
        "- [ ] Create or modify a SuperMe account",
        "- [ ] Connect LinkedIn, GitHub, or another external account",
        "- [ ] Start the interview",
        "- [ ] Send interview messages or upload artifacts",
        "- [ ] Commit or push work in an interview repository",
        "- [ ] Submit the interview",
        "",
        "All unchecked actions require a later, explicit scope of authority from LJ.",
        "",
    ]
    return "\n".join(lines)


def _evidence_pack_from_claims(
    *,
    principal: str,
    company: str,
    position: str,
    claims: list[dict[str, object]],
) -> str:
    """Generate an evidence pack from profile-derived claims.

    Replaces the old Hyperspell-hardcoded _evidence_pack().  Produces the
    same markdown structure: strong evidence, claims that require care,
    and questions requiring the principal.
    """
    permitted = [c for c in claims if c.get("permitted")]
    not_permitted = [c for c in claims if not c.get("permitted")]

    lines = [
        f"# {company} {position} — principal evidence pack",
        "",
        "Status: preparation evidence only. It is not an instruction to start or answer",
        "the interview.",
        "",
        "## Strong, source-grounded evidence",
        "",
    ]

    for i, claim in enumerate(permitted, 1):
        sources = claim.get("sources", [])
        lines.append(f"{i}. **{claim['claim']}**")
        lines.append(f"   - Confidence: {claim.get('confidence', 'unknown')}")
        for s in sources:
            lines.append(f"   - Source: `{s}`")
        lines.append("")

    if not_permitted:
        lines.extend(["## Claims that require care", ""])
        for claim in not_permitted:
            lines.extend([
                f"- **{claim['id']}:** {claim['claim']}",
                f"  - Confidence: {claim.get('confidence', 'unknown')} — not permitted for interview.",
                "",
            ])

    # Generate questions from not-permitted claims (generic — no hardcoded skills)
    questions: list[str] = []
    for claim in not_permitted:
        cid = claim.get("id", "")
        if "us-work" in cid or "work-auth" in cid:
            questions.append(
                "Does the principal have any work-authorisation route not in the profile?"
            )
        elif "location" in cid:
            questions.append(
                "Would the principal consider relocation for this role, and under what terms?"
            )
        elif "gap-" in cid:
            skill = cid.replace("gap-", "").replace("-", " ").title()
            questions.append(
                f"What {skill} experience can be evidenced beyond the profile?"
            )
        else:
            # Generic: use the claim text itself
            questions.append(
                f"Regarding '{claim.get('claim', cid)}': can this be evidenced?"
            )

    if questions:
        lines.extend(["## Questions requiring the principal", ""])
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q}")
        lines.append("")

    return "\n".join(lines)

def _profile_dir(principal: str) -> Path:
    """Return the principal's profile directory, checking standard locations."""
    for p in config.PROFILES_DIR_PRINCIPALS:
        d = p / principal.lower()
        if d.is_dir():
            return d
    return config.PROFILES_DIR_PRINCIPALS[0] / principal.lower()


def _read_profile(filename: str, principal: str) -> str | None:
    """Read a profile file, returning None if absent."""
    d = _profile_dir(principal)
    p = d / filename
    return p.read_text(encoding="utf-8") if p.exists() else None


def _generate_claims(
    principal: str,
    role_gaps: list[str] | None = None,
) -> list[dict[str, object]]:
    """Generate honest, source-grounded claims from profile files.

    Scans ``cv-short.md`` for known skill keywords and generates
    high-confidence permitted claims for skills with evidence.
    Cross-references ``work_auth.md`` and ``soul.md`` for location
    constraints.  If ``role_gaps`` is provided, skills the role
    asks for but the profile doesn't evidence are flagged as
    ``unsupported``.
    """
    cv = _read_profile("cv-short.md", principal) or ""
    soul = _read_profile("soul.md", principal) or ""
    work_auth = _read_profile("work_auth.md", principal) or ""
    cv_lower = cv.lower()
    claims: list[dict[str, object]] = []

    # High-confidence permitted claims.  Source paths are templates
    # interpolated per-principal at use, so VJ/KJ/PK get correct provenance
    # instead of inherited LJ paths.
    CV = f"profiles/{principal.lower()}/cv-short.md"
    BIO = f"profiles/{principal.lower()}/bio-long.md"
    WORK_AUTH = f"profiles/{principal.lower()}/work_auth.md"
    SOUL = f"profiles/{principal.lower()}/soul.md"

    high_claims: dict[str, tuple[list[str], str, list[str]]] = {
        "production-systems": (
            ["production", "built", "operated", "systems"],
            "Built and operated production research and quantitative systems.",
            [CV, BIO],
        ),
        "python": (
            ["python"],
            "Uses Python for research and engineering.",
            [CV],
        ),
        "agentic-systems": (
            ["agent", "llm", "dspy", "hermes"],
            "Builds and uses agentic systems and LLM workflows.",
            [CV, BIO],
        ),
        "quantitative-modelling": (
            ["quantitative", "forecasting", "optimisation", "portfolio"],
            "Built quantitative models for forecasting, optimisation and trading.",
            [CV],
        ),
        "research-ml": (
            ["research", "machine learning", "ml", "deep learning", "phd"],
            "Conducts ML/AI research in industrial R&D settings.",
            [CV, BIO],
        ),
        "mentoring": (
            ["mentor", "mentored", "junior"],
            "Mentored junior researchers and engineers.",
            [CV],
        ),
        "startup-small-team": (
            ["startup", "distributed", "small team"],
            "Worked in startup and small-team remote environments.",
            [CV],
        ),
        "c-cpp": (
            ["c++", "c/c++"],
            "Uses C/C++ for systems-level engineering.",
            [CV],
        ),
        "sql-data": (
            ["sql", "duckdb", "database"],
            "Uses SQL and data engineering tools.",
            [CV],
        ),
    }
    for claim_id, (keywords, claim_text, sources) in high_claims.items():
        if any(kw in cv_lower for kw in keywords):
            claims.append({
                "id": claim_id, "claim": claim_text,
                "confidence": "high", "sources": sources, "permitted": True,
            })

    # Location constraints
    combined = (work_auth + "\n" + soul).lower()
    us_blocked = any(phrase in combined for phrase in [
        "not ok", "deal-breaker", "auto-drop", "no us",
        "not open to us", "us-only", "us-timezone",
    ])
    if us_blocked:
        claims.append({
            "id": "us-work-authorisation",
            "claim": "Can work on location in the US.",
            "confidence": "likely-false",
            "sources": [WORK_AUTH, SOUL],
            "permitted": False,
        })

    # Role-specific gaps
    if role_gaps:
        for gap in role_gaps:
            gap_lower = gap.lower()
            if gap_lower not in cv_lower:
                claims.append({
                    "id": f"gap-{gap_lower.replace(' ', '-')}",
                    "claim": f"Has documented {gap} experience.",
                    "confidence": "unsupported",
                    "sources": [],
                    "permitted": False,
                })

    return claims


def _claims() -> list[dict[str, object]]:
    """Backward-compatible wrapper — generates claims from LJ's profile.

    For new code, prefer ``_generate_claims(principal, role_gaps)`` which
    works for any principal and any role.
    """
    return _generate_claims("lj")


def _extract_role_gaps(role_text: str, principal: str) -> list[str]:
    """Extract skills the role asks for but the profile doesn't evidence.

    Generic — works for any role text and any principal.  Scans the role
    text for skill keywords (programming languages, tools, domain terms)
    and checks whether the profile's ``cv-short.md`` contains them.
    Returns a list of skills that are in the role but missing from the
    profile.
    """
    # Skills to check: (keyword in role text, display name)
    # This list is intentionally broad — it covers common tech roles.
    SKILL_KEYWORDS: list[tuple[str, str]] = [
        ("typescript", "TypeScript"),
        ("javascript", "JavaScript"),
        ("rust", "Rust"),
        (r"go\b", "Go"),
        ("golang", "Go"),
        (r"java\b", "Java"),
        ("ruby", "Ruby"),
        ("php", "PHP"),
        ("swift", "Swift"),
        ("kotlin", "Kotlin"),
        ("react", "React"),
        ("vue", "Vue"),
        ("angular", "Angular"),
        (r"node\.?js", "Node.js"),
        (r"next\.?js", "Next.js"),
        ("django", "Django"),
        ("fastapi", "FastAPI"),
        ("flask", "Flask"),
        ("rails", "Ruby on Rails"),
        ("kubernetes", "Kubernetes"),
        ("docker", "Docker"),
        ("aws", "AWS"),
        ("gcp", "GCP"),
        ("azure", "Azure"),
        ("terraform", "Terraform"),
        ("product engineer", "Product Engineering"),
        ("frontend", "Frontend Development"),
        ("backend", "Backend Development"),
        (r"full.?stack", "Full-Stack Development"),
        ("devops", "DevOps"),
        ("sre", "Site Reliability Engineering"),
        ("data engineer", "Data Engineering"),
        ("ml engineer", "ML Engineering"),
        ("platform engineer", "Platform Engineering"),
        ("security engineer", "Security Engineering"),
    ]

    import re

    role_lower = role_text.lower()
    profile_dir = _profile_dir(principal)
    cv_text = ""
    cv_path = profile_dir / "cv-short.md"
    if cv_path.exists():
        cv_text = cv_path.read_text(encoding="utf-8").lower()
    bio_path = profile_dir / "bio-long.md"
    if bio_path.exists():
        cv_text += " " + bio_path.read_text(encoding="utf-8").lower()

    gaps: list[str] = []
    for pattern, display_name in SKILL_KEYWORDS:
        if re.search(pattern, role_lower):
            # Skill is mentioned in the role — check if profile evidences it
            if not re.search(pattern, cv_text):
                gaps.append(display_name)

    return gaps


def prepare_superme_interview_case(
    *,
    principal: str,
    company: str,
    position: str,
    role_url: str,
    when: date | None = None,
    applications_dir: Path | None = None,
    refresh_references: bool = False,
    dry_run: bool = False,
) -> PreparedInterviewCase:
    """Create an idempotent, non-live SuperMe ATA preparation pack."""
    current_date = when or date.today()
    root = applications_dir or principal_applications_dir(principal)
    slug = ata_case_slug(current_date, company, position)
    case_dir = root / slug
    interview_dir = case_dir / "agent-interview"
    manifest_path = interview_dir / "manifest.json"
    if dry_run:
        return PreparedInterviewCase(case_dir, slug, "would_create", manifest_path)

    case_dir.mkdir(parents=True, exist_ok=True)
    interview_dir.mkdir(parents=True, exist_ok=True)
    adapter = SuperMePublicAdapter(role_url)

    # Fetch references first — need role-page.txt for gap detection.
    references = adapter.fetch_public_references(
        interview_dir / "references",
        refresh=refresh_references,
    )

    # Read role page text for role-specific gap detection (Phase D.1).
    role_page_path = interview_dir / "references" / "role-page.txt"
    role_text = role_page_path.read_text(encoding="utf-8") if role_page_path.exists() else ""

    # Extract role gaps: skills the role asks for but the profile doesn't evidence.
    role_gaps = _extract_role_gaps(role_text, principal)

    # Generate claims from profiles — no more Hyperspell hard-coding.
    claims = _generate_claims(principal, role_gaps=role_gaps or None)

    main_path = case_dir / f"{slug}.txt"
    if not main_path.exists():
        main_path.write_text(
            _case_text(
                principal=principal,
                company=company,
                position=position,
                adapter=adapter,
                created=current_date,
            ),
            encoding="utf-8",
        )

    # Claims and evidence-pack are ALWAYS regenerated from the current
    # profile + role text — they are derived artefacts, not primary data.
    # (Phase A references are fetched once and cached because they're
    # external; claims/pack are local computations and must reflect the
    # current code.) The main case .txt is preserved on re-run because it
    # may carry hand-edited principal notes.
    claims_path = interview_dir / "claims.json"
    claims_path.write_text(
        json.dumps(claims, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    evidence_path = interview_dir / "evidence-pack.md"
    evidence_path.write_text(
        _evidence_pack_from_claims(
            principal=principal, company=company,
            position=position, claims=claims,
        ),
        encoding="utf-8",
    )

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": ATA_SCHEMA_VERSION,
        "kind": "agent-to-agent-interview",
        "status": "PREPARED",
        "live_interview_started": False,
        "principal": principal,
        "company": company,
        "position": position,
        "provider": adapter.provider,
        "provider_role_id": adapter.role_id,
        "interview_id": None,
        "case_slug": slug,
        "created_at": now,
        "runtime": _runtime_manifest(),
        "references_manifest": "references/manifest.json",
        "protocol_valid": references["protocol_validation"]["valid"],
        "consent": {
            "public_reference_fetch": True,
            "local_preparation": True,
            "account_onboarding": False,
            "external_account_connection": False,
            "interview_start": False,
            "interview_messages": False,
            "artifact_upload": False,
            "interview_submission": False,
        },
    }
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = previous.get("created_at", now)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    events_path = interview_dir / "events.jsonl"
    if not events_path.exists():
        event = {
            "at": now,
            "kind": "ata_case_prepared",
            "principal": principal,
            "provider": adapter.provider,
            "role_id": adapter.role_id,
            "state": "PREPARED",
            "external_write": False,
        }
        events_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        status = "created"
    else:
        status = "existing"
    return PreparedInterviewCase(case_dir, slug, status, manifest_path)

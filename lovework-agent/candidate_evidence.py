"""Small, deterministic candidate-fact retriever for grounded matching."""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"[a-z][a-z0-9+#]{2,}")
STOPWORDS = {
    "and", "the", "for", "with", "from", "into", "that", "this", "are", "our",
    "you", "your", "we", "they", "work", "role", "job", "team", "teams", "using",
    "across", "experience", "building", "looking", "prefer", "strong", "technical",
}
SYNONYM_GROUPS = (
    {"voice", "speech", "audio", "transcription", "recognition", "asr"},
    {"agent", "agents", "agentic", "orchestration", "workflow", "workflows"},
    {"startup", "founding", "founder", "early", "small", "product"},
    {"backend", "api", "apis", "infrastructure", "systems", "production"},
    {"quant", "trading", "finance", "financial"},
)
DOMAIN_TERMS = set().union(*SYNONYM_GROUPS)
ARTIFACT_TERMS = {
    "built", "demo", "demoed", "demonstrated", "implemented", "invented",
    "launched", "production", "shipped", "patent", "prototype",
}


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower())) - STOPWORDS


def _expand(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for group in SYNONYM_GROUPS:
        if tokens & group:
            expanded |= group
    return expanded


@dataclass(frozen=True)
class CandidateFact:
    text: str
    source: str
    tokens: frozenset[str]


class CandidateEvidenceIndex:
    """Retrieve high-overlap profile/bio paragraphs without another LLM call."""

    def __init__(self, profile_text: str, bio_text: str = ""):
        self.facts = self._chunk(profile_text, "profile") + self._chunk(bio_text, "bio-long")

    @staticmethod
    def _chunk(text: str, source: str) -> list[CandidateFact]:
        facts: list[CandidateFact] = []
        heading = source
        for raw in re.split(r"\n\s*\n", text):
            paragraph = " ".join(line.strip() for line in raw.splitlines()).strip()
            if not paragraph:
                continue
            if paragraph.startswith("#") and len(paragraph) < 160:
                heading = paragraph.lstrip("# ") or source
                continue
            tokens = _tokens(paragraph)
            if len(tokens) < 3:
                continue
            facts.append(CandidateFact(paragraph[:1200], f"{source}: {heading}", frozenset(tokens)))
        return facts

    def retrieve(self, query: str, limit: int = 8) -> list[CandidateFact]:
        query_tokens = _expand(_tokens(query))
        ranked: list[tuple[float, CandidateFact]] = []
        for fact in self.facts:
            overlap = query_tokens & set(fact.tokens)
            if not overlap:
                continue
            # Rare, specific words are more useful than generic overlap. The
            # synonym expansion supplies domain bridges such as voice↔speech.
            score = sum(1.0 + min(len(token), 10) / 10 for token in overlap)
            score += 2.0 * len(overlap & DOMAIN_TERMS)
            score /= max(len(fact.tokens) ** 0.35, 1)
            ranked.append((score, fact))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [fact for _, fact in ranked[:limit]]

    def format_for_matcher(self, query: str, limit: int = 12) -> str:
        facts = self.retrieve(query, limit=limit)
        if not facts:
            return "No candidate evidence retrieved. Do not infer missing experience."
        lines = []
        for fact in facts:
            marker = " [CONCRETE ARTIFACT]" if set(fact.tokens) & ARTIFACT_TERMS else ""
            lines.append(f"- [{fact.source}]{marker} {fact.text}")
        return "\n".join(lines)


class EvidenceGroundedMatcher:
    """Drop-in wrapper that supplies retrieved candidate facts to a matcher."""

    def __init__(self, matcher, evidence_index: CandidateEvidenceIndex):
        self.matcher = matcher
        self.evidence_index = evidence_index

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ):
        query = f"{job_title}\n{org_name}\n{job_description}"
        evidence = self.evidence_index.format_for_matcher(query)
        grounded_description = (
            f"{job_description}\n\n"
            "--- RETRIEVED CANDIDATE EVIDENCE ---\n"
            f"{evidence}\n"
            "--- END RETRIEVED CANDIDATE EVIDENCE ---"
        )
        return self.matcher.match(
            job_title,
            grounded_description,
            org_name,
            job_url=job_url,
            location=location,
        )

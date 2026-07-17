"""
Local wiki storage for crawl results.

Format: Markdown files under wiki/.
- wiki/reports/YYYY-MM-DD-report.md — per-run reports
- wiki/orgs/<org-name>.md — per-organization history
- wiki/index.md — master index of all findings
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import config
from report_header import build_header

logger = logging.getLogger(__name__)


def match_fields(match) -> dict:
    """Return optional multi-axis fields from a MatchResult-like object."""
    return {
        "fit_score": getattr(match, "fit_score", None),
        "reach_score": getattr(match, "reach_score", None),
        "flourish_score": getattr(match, "flourish_score", None),
        "combined_score": getattr(match, "combined_score", None),
        "recommended_action": getattr(match, "recommended_action", None),
        "primary_content_hash": getattr(match, "primary_content_hash", None),
        "primary_fetched_at": getattr(match, "primary_fetched_at", None),
        "primary_fetch_method": getattr(match, "primary_fetch_method", None),
        "alignment_matrix": getattr(match, "alignment_matrix", None),
        "gaps": getattr(match, "gaps", None),
        "application_angle": getattr(match, "application_angle", None),
        "screening_story": getattr(match, "screening_story", None),
        "likely_day_to_day": getattr(match, "likely_day_to_day", None),
        "prestige_trap_risk": getattr(match, "prestige_trap_risk", None),
        "assessment_status": getattr(match, "assessment_status", "SCORED"),
    }


def _format_entry(lines: List[str], e: "WikiEntry", show_age: bool = False) -> None:
    """Format a single WikiEntry into the report lines."""
    lines.append(f"### {e.org_name} — {e.title}")
    lines.append("")
    if e.url:
        lines.append(f"- **URL**: {e.url}")
    if e.discovery_url:
        date_suffix = f" ({e.discovery_date})" if e.discovery_date else ""
        lines.append(f"- **Found via**: [{e.source}]({e.discovery_url}){date_suffix}")
    if e.location:
        lines.append(f"- **Location**: {e.location}")
    if e.assessment_status == "UNSCORED":
        lines.append("- **Score**: UNSCORED")
    else:
        lines.append(f"- **Score**: {e.score}/10")
    if e.fit_score is not None:
        lines.append(f"- **Fit**: {e.fit_score}/10")
    if e.reach_score is not None:
        lines.append(f"- **Reach**: {e.reach_score}/10")
    if e.flourish_score is not None:
        lines.append(f"- **Flourish**: {e.flourish_score}/10")
    if e.combined_score is not None:
        lines.append(f"- **Combined**: {e.combined_score}/10")
    if e.recommended_action:
        lines.append(f"- **Action**: {e.recommended_action}")
    if e.primary_content_hash:
        lines.append(
            f"- **Primary evidence**: `{e.primary_content_hash[:12]}` "
            f"via {e.primary_fetch_method} ({e.primary_fetched_at})"
        )
    if e.alignment_matrix:
        lines.append("- **Evidence alignment**:")
        lines.extend(f"  - {item}" for item in e.alignment_matrix)
    if e.gaps:
        lines.append("- **Gaps**:")
        lines.extend(f"  - {item}" for item in e.gaps)
    if e.application_angle:
        lines.append(f"- **Application angle**: {e.application_angle}")
    if e.screening_story:
        lines.append(f"- **Screening story**: {e.screening_story}")
    if e.likely_day_to_day:
        lines.append(f"- **Likely day-to-day**: {e.likely_day_to_day}")
    if e.prestige_trap_risk:
        lines.append(f"- **Prestige-trap risk**: {e.prestige_trap_risk}")
    if e.lifecycle_status:
        age_str = ""
        if show_age and e.first_seen:
            try:
                first = datetime.fromisoformat(e.first_seen)
                days = (datetime.now() - first).days
                age_str = f" (open {days} days, since {e.first_seen})"
            except Exception:
                pass
        lines.append(f"- **Lifecycle**: {e.lifecycle_status}{age_str}")
    lines.append(f"- **Source**: {e.source}")
    lines.append(f"- **Reasoning**: {e.reasoning}")
    lines.append("")


class WikiEntry:
    """Single job finding to store in wiki."""

    def __init__(
        self,
        org_name: str,
        title: str,
        url: Optional[str],
        location: Optional[str],
        score: float,
        decision: str,
        reasoning: str,
        source: str,
        date: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        first_seen: Optional[str] = None,
        fit_score: Optional[float] = None,
        reach_score: Optional[float] = None,
        flourish_score: Optional[float] = None,
        combined_score: Optional[float] = None,
        recommended_action: Optional[str] = None,
        discovery_url: Optional[str] = None,
        discovery_date: Optional[str] = None,
        primary_content_hash: Optional[str] = None,
        primary_fetched_at: Optional[str] = None,
        primary_fetch_method: Optional[str] = None,
        alignment_matrix: Optional[List[str]] = None,
        gaps: Optional[List[str]] = None,
        application_angle: Optional[str] = None,
        screening_story: Optional[str] = None,
        likely_day_to_day: Optional[str] = None,
        prestige_trap_risk: Optional[str] = None,
        assessment_status: Optional[str] = None,
    ):
        self.org_name = org_name
        self.title = title
        self.url = url or ""
        self.location = location or ""
        self.score = score
        self.decision = decision
        self.reasoning = reasoning
        self.source = source
        self.date = date or datetime.now().strftime("%Y-%m-%d")
        self.lifecycle_status = lifecycle_status  # new, still_open, long_lasting, disappeared
        self.first_seen = first_seen  # ISO date when first seen by the crawler
        self.fit_score = fit_score
        self.reach_score = reach_score
        self.flourish_score = flourish_score
        self.combined_score = combined_score
        self.recommended_action = recommended_action
        self.discovery_url = discovery_url or ""
        self.discovery_date = discovery_date or ""
        self.primary_content_hash = primary_content_hash or ""
        self.primary_fetched_at = primary_fetched_at or ""
        self.primary_fetch_method = primary_fetch_method or ""
        self.alignment_matrix = alignment_matrix or []
        self.gaps = gaps or []
        self.application_angle = application_angle or ""
        self.screening_story = screening_story or ""
        self.likely_day_to_day = likely_day_to_day or ""
        self.prestige_trap_risk = prestige_trap_risk or ""
        self.assessment_status = assessment_status or "SCORED"


class WikiStore:
    """Manages local markdown wiki."""

    def __init__(self, root: Path = config.WIKI_ROOT):
        self.root = root
        self.orgs_dir = root / "orgs"
        self.reports_dir = root / "reports"
        self.orgs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def save_report(self, entries: List[WikiEntry], profile_name: str = "LJ") -> Path:
        """Save a daily run report.

        The filename includes an hourly timestamp suffix so multiple
        sweeps on the same calendar date produce distinct files.
        """
        date = entries[0].date if entries else datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H%M%S")
        path = self.reports_dir / f"{date}-{ts}-{profile_name.lower()}-report.md"

        go = [e for e in entries if e.decision == "GO"]
        maybe = [e for e in entries if e.decision == "MAYBE"]
        flag = [e for e in entries if e.decision == "FLAG"]
        kill = [e for e in entries if e.decision == "DROP"]

        new_jobs = [e for e in entries if e.lifecycle_status == "new"]
        long_lasting = [e for e in entries if e.lifecycle_status == "long_lasting"]
        still_open = [e for e in entries if e.lifecycle_status == "still_open"]

        lines = build_header(
            run_type="FULL SWEEP",
            profile_label=profile_name,
            sources=None,  # full sweep covers all 8; header explainer says so
        )
        # Drop the canonical "---" since we'll add our own later
        if lines and lines[-2] == "---":
            lines = lines[:-2]

        # New listings section
        if new_jobs:
            lines.append(f"## New Listings ({len(new_jobs)})")
            lines.append("")
            lines.append("First time seen by the crawler. Fresh opportunities.")
            lines.append("")
            for e in sorted(new_jobs, key=lambda x: x.score, reverse=True):
                if e.decision in ("GO", "MAYBE"):
                    _format_entry(lines, e)
            lines.append("")

        # Long-lasting (suspicious)
        if long_lasting:
            lines.append(f"## Long-Lasting / Suspicious ({len(long_lasting)})")
            lines.append("")
            lines.append(
                "Open >30 days. Company may be picky, unserious, or has infinite need. "
                "Score is lowered accordingly."
            )
            lines.append("")
            for e in sorted(long_lasting, key=lambda x: x.score, reverse=True):
                _format_entry(lines, e, show_age=True)
            lines.append("")

        for section, items in (("GO", go), ("MAYBE", maybe), ("FLAG", flag), ("DROP", kill)):
            if not items:
                continue
            lines.append(f"## {section} ({len(items)})")
            lines.append("")
            for e in sorted(items, key=lambda x: x.score, reverse=True):
                _format_entry(lines, e)
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Report saved to {path}")
        return path

    def update_org_page(self, entry: WikiEntry) -> None:
        """Append a finding to an organization's wiki page."""
        safe_name = self._safe_filename(entry.org_name)
        path = self.orgs_dir / f"{safe_name}.md"

        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            content = f"# {entry.org_name}\n\nFindings from lovework-agent.\n\n---\n\n"

        block = [
            f"### {entry.date} — {entry.title}",
            "",
            (
                f"- **Decision**: {entry.decision} (UNSCORED)"
                if entry.assessment_status == "UNSCORED"
                else f"- **Decision**: {entry.decision} ({entry.score}/10)"
            ),
        ]
        if entry.fit_score is not None:
            block.append(f"- **Fit**: {entry.fit_score}/10")
        if entry.reach_score is not None:
            block.append(f"- **Reach**: {entry.reach_score}/10")
        if entry.flourish_score is not None:
            block.append(f"- **Flourish**: {entry.flourish_score}/10")
        if entry.combined_score is not None:
            block.append(f"- **Combined**: {entry.combined_score}/10")
        if entry.recommended_action:
            block.append(f"- **Action**: {entry.recommended_action}")
        if entry.url:
            block.append(f"- **URL**: {entry.url}")
        if entry.discovery_url:
            date_suffix = f" ({entry.discovery_date})" if entry.discovery_date else ""
            block.append(f"- **Found via**: [{entry.source}]({entry.discovery_url}){date_suffix}")
        if entry.primary_content_hash:
            block.append(
                f"- **Primary evidence**: `{entry.primary_content_hash[:12]}` "
                f"via {entry.primary_fetch_method} ({entry.primary_fetched_at})"
            )
        if entry.alignment_matrix:
            block.append("- **Evidence alignment**:")
            block.extend(f"  - {item}" for item in entry.alignment_matrix)
        if entry.gaps:
            block.append("- **Gaps**:")
            block.extend(f"  - {item}" for item in entry.gaps)
        if entry.application_angle:
            block.append(f"- **Application angle**: {entry.application_angle}")
        if entry.screening_story:
            block.append(f"- **Screening story**: {entry.screening_story}")
        if entry.likely_day_to_day:
            block.append(f"- **Likely day-to-day**: {entry.likely_day_to_day}")
        if entry.prestige_trap_risk:
            block.append(f"- **Prestige-trap risk**: {entry.prestige_trap_risk}")
        if entry.location:
            block.append(f"- **Location**: {entry.location}")
        block.append(f"- **Reasoning**: {entry.reasoning}")
        block.append(f"- **Source**: {entry.source}")
        block.append("")

        content += "\n".join(block)
        path.write_text(content, encoding="utf-8")

    def rebuild_index(self, entries: List[WikiEntry]) -> None:
        """Rebuild the master index.md.

        `entries` is the current run's findings. The index combines them
        with anything already on disk in `wiki/orgs/` so a partial / cron
        run still leaves a fully populated index.

        The org-page format is parsed (### DATE — Title blocks under each
        org's wiki file) and merged with the in-memory entries; duplicates
        (same org+title+url) are dedup'd by first-seen.
        """
        path = self.root / "index.md"
        # Merge: in-memory entries + parsed from disk.
        all_entries = list(entries) + self._entries_from_org_pages()
        seen: set[tuple[str, str, str]] = set()
        deduped: List[WikiEntry] = []
        for e in all_entries:
            key = (
                (e.org_name or "").lower().strip(),
                (e.title or "").lower().strip(),
                (e.url or "").lower().strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)

        go = [e for e in deduped if e.decision == "GO"]
        maybe = [e for e in deduped if e.decision == "MAYBE"]
        flag = [e for e in deduped if e.decision == "FLAG"]

        # Latest report = the most recent by report date.
        latest_report_rel = self._latest_report_rel()

        lines = [
            "# LoveWork Index",
            "",
            f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Quick Links",
            "",
            f"- [Latest report]({latest_report_rel})" if latest_report_rel else "- [Reports](reports/)",
            f"- [All reports](reports/)",
            f"- [Organizations](orgs/) ({len(list(self.orgs_dir.glob('*.md')))} pages)",
            "",
            "## Counts",
            "",
            f"- **GO**: {len(go)} listings",
            f"- **MAYBE**: {len(maybe)} listings",
            f"- **FLAG**: {len(flag)} listings",
            f"- **Total findings on file**: {len(deduped)}",
            "",
            "## GO Listings",
            "",
        ]
        if go:
            for e in sorted(go, key=lambda x: (x.date, x.score), reverse=True):
                url_text = f" [{e.url}]({e.url})" if e.url else ""
                lines.append(
                    f"- **{e.date}** | {e.org_name} — {e.title} | {e.score:.1f}/10{url_text}"
                )
        else:
            lines.append("_No GO listings yet._")
        lines.append("")

        lines.append("## MAYBE Listings")
        lines.append("")
        if maybe:
            # Cap the MAYBE list at 50 to keep the index scannable; full
            # listings are in the reports.
            for e in sorted(maybe, key=lambda x: (x.date, x.score), reverse=True)[:50]:
                url_text = f" [{e.url}]({e.url})" if e.url else ""
                lines.append(
                    f"- **{e.date}** | {e.org_name} — {e.title} | {e.score:.1f}/10{url_text}"
                )
            if len(maybe) > 50:
                lines.append(f"_…and {len(maybe) - 50} more (see reports)._")
        else:
            lines.append("_No MAYBE listings yet._")
        lines.append("")

        # Organisational browsing — first 30 org pages by mtime, with link.
        orgs = sorted(self.orgs_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if orgs:
            lines.append("## Organizations (recent)")
            lines.append("")
            for p in orgs[:30]:
                stem = p.stem.replace("_", " ")
                lines.append(f"- [{stem}](orgs/{p.name})")
            if len(orgs) > 30:
                lines.append(f"_…and {len(orgs) - 30} more in [orgs/](orgs/)._")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Index rebuilt at {path}")

    def _entries_from_org_pages(self) -> List[WikiEntry]:
        """Parse existing `wiki/orgs/<Org>.md` files back into WikiEntry
        rows. Used to merge into the index on rebuild so the index is
        always populated even after a partial / cron run.
        """
        import re
        out: List[WikiEntry] = []
        if not self.orgs_dir.is_dir():
            return out
        section_re = re.compile(r"^###\s+(?P<date>\d{4}-\d{2}-\d{2})\s+—\s+(?P<title>.+?)\s*$")
        decision_re = re.compile(
            r"^\s*-\s*\*\*Decision\*\*:\s*\*?(GO|MAYBE|FLAG|DROP)\*?"
            r"(?:\s*\((?P<score>\d+(?:\.\d+)?)/10\))?"
        )
        action_re = re.compile(r"^\s*-\s*\*\*Action\*\*:\s*(?P<action>[A-Z_]+)")
        fit_re = re.compile(r"^\s*-\s*\*\*Fit\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        reach_re = re.compile(r"^\s*-\s*\*\*Reach\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        flourish_re = re.compile(r"^\s*-\s*\*\*Flourish\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        combined_re = re.compile(r"^\s*-\s*\*\*Combined\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10")
        url_re = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
        loc_re = re.compile(r"^\s*-\s*\*\*Location\*\*:\s*(?P<loc>.+)")
        source_re = re.compile(r"^\s*-\s*\*\*Source\*\*:\s*(?P<src>.+)")
        found_via_re = re.compile(
            r"^\s*-\s*\*\*Found via\*\*:\s*\[[^]]+\]\((?P<url>[^)]+)\)"
            r"(?:\s+\((?P<date>\d{4}-\d{2}-\d{2})\))?"
        )
        for path in self.orgs_dir.glob("*.md"):
            # Prefer the H1 of the file (write_org_page writes "# Org"),
            # fall back to the filename stem (with collapse for double/triple
            # underscores introduced by the safe_filename sanitizer).
            org_name = ""
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines()[:5]:
                if line.startswith("# "):
                    org_name = line[2:].strip()
                    break
            if not org_name:
                import re as _re
                stem = _re.sub(r"_+", " ", path.stem).strip()
                org_name = stem
            current: dict | None = None
            for line in text.splitlines():
                sec = section_re.match(line.rstrip())
                if sec:
                    if current and current.get("decision") in ("GO", "MAYBE", "FLAG"):
                        out.append(WikiEntry(
                            org_name=org_name,
                            title=current.get("title", ""),
                            url=current.get("url") or None,
                            location=current.get("location") or None,
                            score=float(current.get("score") or 0.0),
                            decision=current.get("decision", ""),
                            reasoning="(from wiki/orgs)",
                            source=current.get("source", ""),
                            date=current.get("date", ""),
                            fit_score=current.get("fit_score"),
                            reach_score=current.get("reach_score"),
                            flourish_score=current.get("flourish_score"),
                            combined_score=current.get("combined_score"),
                            recommended_action=current.get("recommended_action"),
                            discovery_url=current.get("discovery_url"),
                            discovery_date=current.get("discovery_date"),
                        ))
                    current = {"date": sec.group("date"), "title": sec.group("title").strip()}
                    continue
                if current is None:
                    continue
                m = decision_re.match(line)
                if m:
                    current["decision"] = m.group(1)
                    if m.group("score"):
                        current["score"] = float(m.group("score"))
                    continue
                m = action_re.match(line)
                if m:
                    current["recommended_action"] = m.group("action").strip()
                    continue
                m = fit_re.match(line)
                if m:
                    current["fit_score"] = float(m.group("score"))
                    continue
                m = reach_re.match(line)
                if m:
                    current["reach_score"] = float(m.group("score"))
                    continue
                m = flourish_re.match(line)
                if m:
                    current["flourish_score"] = float(m.group("score"))
                    continue
                m = combined_re.match(line)
                if m:
                    current["combined_score"] = float(m.group("score"))
                    continue
                m = url_re.match(line)
                if m:
                    current["url"] = m.group("url").strip()
                    continue
                m = loc_re.match(line)
                if m:
                    current["location"] = m.group("loc").strip()
                    continue
                m = source_re.match(line)
                if m:
                    current["source"] = m.group("src").strip()
                    continue
                m = found_via_re.match(line)
                if m:
                    current["discovery_url"] = m.group("url").strip()
                    current["discovery_date"] = (m.group("date") or "").strip()
                    continue
            # Flush trailing section.
            if current and current.get("decision") in ("GO", "MAYBE", "FLAG"):
                out.append(WikiEntry(
                    org_name=org_name,
                    title=current.get("title", ""),
                    url=current.get("url") or None,
                    location=current.get("location") or None,
                    score=float(current.get("score") or 0.0),
                    decision=current.get("decision", ""),
                    reasoning="(from wiki/orgs)",
                    source=current.get("source", ""),
                    date=current.get("date", ""),
                    fit_score=current.get("fit_score"),
                    reach_score=current.get("reach_score"),
                    flourish_score=current.get("flourish_score"),
                    combined_score=current.get("combined_score"),
                    recommended_action=current.get("recommended_action"),
                    discovery_url=current.get("discovery_url"),
                    discovery_date=current.get("discovery_date"),
                ))
        return out

    def _latest_report_rel(self) -> Optional[str]:
        """Return the relative path of the most recent report (by H1 date)."""
        import re
        if not self.reports_dir.is_dir():
            return None
        best: Optional[tuple[str, Path]] = None
        date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
        for p in self.reports_dir.glob("*.md"):
            try:
                first = p.read_text(encoding="utf-8").splitlines()[:3]
            except Exception:
                continue
            for line in first:
                m = date_re.search(line)
                if m:
                    date = m.group(1)
                    if best is None or date > best[0]:
                        best = (date, p)
                    break
        if best is None:
            # Fall back to the file with the latest mtime.
            files = sorted(self.reports_dir.glob("*.md"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            return f"reports/{files[0].name}" if files else None
        return f"reports/{best[1].name}"

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Sanitise a string into a safe markdown filename stem.

        Rules (ASCII-only by design — non-ASCII org names get folded to `_`):
        - Letters / digits / space / dash / underscore / dot stay.
        - Everything else (slashes, slashes, parens, punctuation, and any
          non-ASCII code point) is replaced with `_`.
        - Runs of `_` are collapsed to one.
        - Leading/trailing `_` and `.` are stripped.
        - Empty result is replaced with `x` (a defensive default).
        - Capped at 200 chars to keep paths sane.

        Why ASCII-only: filesystem behaviour for unicode paths varies
        across macOS / Linux / WSL, and unicode confusable chars (e.g.
        "Zūm" / "Zum" look identical but hash differently) silently
        produce duplicate org pages. ASCII fixes both.
        """
        import re
        n = name or "x"
        # Drop any non-ASCII char first; replace with `_`.
        n = n.encode("ascii", errors="replace").decode("ascii")
        n = n.replace("?", "_")
        # Keep alnum + a small set of safe punctuation; replace the rest.
        n = re.sub(r"[^A-Za-z0-9._-]+", "_", n)
        # Collapse runs of underscores introduced by adjacent unsafe chars.
        n = re.sub(r"_+", "_", n)
        n = n.strip("._-") or "x"
        return n[:200]

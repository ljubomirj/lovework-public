"""Prepared LoveWork dossiers are not confused with submitted applications."""

from datetime import date

from application_packs import (
    PREPARED_MARKER,
    go_entries_from_report,
    insert_pack_report_section,
    prepare_go_cases,
)
from history import scan_applications
from wiki_store import WikiEntry


def _go(org="Example AI", title="Founding Engineer", url="https://example.test/jobs/1"):
    return WikiEntry(
        org_name=org,
        title=title,
        url=url,
        location="London",
        score=8.6,
        decision="GO",
        reasoning="Strong fit.",
        source="hn_hiring",
        discovery_url="https://news.ycombinator.com/item?id=1",
        discovery_date="2026-07-19",
        lifecycle_status="new",
        alignment_matrix=["agent workflows -> production agent systems"],
        gaps=["no explicit product metric"],
        application_angle="Lead with the shipped agent system.",
        advert_excerpt="Build and ship an agentic product for real users.",
    )


def test_prepare_go_case_creates_txt_dossier_and_is_idempotent(tmp_path):
    entry = _go()
    created = prepare_go_cases([entry], cases_root=tmp_path, today=date(2026, 7, 19), only_new=False)

    assert created[0].status == "created"
    text_path = created[0].path / f"{created[0].path.name}.txt"
    text = text_path.read_text(encoding="utf-8")
    assert PREPARED_MARKER in text
    assert "=" * 72 in text
    assert "https://news.ycombinator.com/item?id=1" in text
    assert "Build and ship an agentic product for real users." in text
    assert "Company, hiring, and work reality" in text

    repeated = prepare_go_cases([entry], cases_root=tmp_path, today=date(2026, 7, 19), only_new=False)
    assert repeated[0].status == "existing"
    assert text_path.read_text(encoding="utf-8") == text


def test_prepared_case_is_not_a_history_application(tmp_path):
    entry = _go()
    prepare_go_cases([entry], cases_root=tmp_path, today=date(2026, 7, 19), only_new=False)

    assert scan_applications("Example AI", applications_dir=tmp_path) == []


def test_default_root_is_principal_state_and_mirrors_unified_view(tmp_path, monkeypatch):
    import config as lw_config

    # conftest's autouse isolated_config fixture already created tmp_path/applications
    # and pointed LOVEWORK_APPLICATIONS_DIR at it; add the principal state root.
    state_root = tmp_path / "state"
    (state_root / "lj" / "applications").mkdir(parents=True)
    unified = tmp_path / "applications"
    monkeypatch.setattr(lw_config, "STATE_DIR", state_root)
    monkeypatch.setattr(lw_config, "APPLICATIONS_DIR", unified)

    created = prepare_go_cases([_go()], today=date(2026, 7, 19), only_new=False)

    assert created[0].status == "created"
    pack = created[0].path
    assert pack.parent == state_root / "lj" / "applications"
    assert pack.name.endswith("-LoveWork")
    assert pack.is_dir()
    # Parent unified view mirrors the state-owned pack as a relative symlink.
    link = unified / pack.name
    assert link.is_symlink()
    assert link.resolve() == pack
    # Re-run stays idempotent and does not duplicate the mirror.
    repeated = prepare_go_cases([_go()], today=date(2026, 7, 19), only_new=False)
    assert repeated[0].status == "existing"
    assert len(list(unified.glob("*-LoveWork"))) == 1


def test_explicit_cases_root_does_not_touch_unified_view(tmp_path, monkeypatch):
    import config as lw_config

    unified = tmp_path / "applications"
    monkeypatch.setattr(lw_config, "APPLICATIONS_DIR", unified)

    created = prepare_go_cases(
        [_go()], cases_root=tmp_path / "cases", today=date(2026, 7, 19), only_new=False
    )

    assert created[0].status == "created"
    assert created[0].path.parent == tmp_path / "cases"
    assert list(unified.glob("*-LoveWork")) == []


def test_recent_real_application_skips_semantically_similar_pack(tmp_path):
    existing = tmp_path / "20260707-Example_AI-Senior_AI_Engineer"
    existing.mkdir()
    (existing / "application.txt").write_text("Applied through employer site.\n", encoding="utf-8")

    results = prepare_go_cases(
        [_go(title="Senior AI Engineer")],
        cases_root=tmp_path,
        today=date(2026, 7, 19),
        only_new=False,
    )

    assert results[0].status == "skipped_recent_application"
    assert results[0].path == existing


def test_non_role_title_is_visible_for_repair_not_written_as_case_name(tmp_path):
    results = prepare_go_cases(
        [_go(org="Hiya", title="Seattle/London")],
        cases_root=tmp_path,
        today=date(2026, 7, 19),
        only_new=False,
    )

    assert results[0].status == "needs_title_repair"
    assert not list(tmp_path.glob("*-LoveWork"))


def test_report_go_parser_preserves_provenance_and_assessment(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        """# FULL SWEEP

## GO (1)

### Example AI — Founding Engineer

- **URL**: https://example.test/jobs/1
- **Found via**: [hn_hiring](https://news.ycombinator.com/item?id=1) (2026-07-19)
- **Location**: London
- **Score**: 8.6/10
- **Fit**: 9.0/10
- **Reach**: 8.0/10
- **Flourish**: 9.0/10
- **Action**: APPLY_NOW
- **Evidence alignment**:
  - agent workflows -> production systems
- **Gaps**:
  - no product metric
- **Application angle**: Lead with shipped work.
- **Reasoning**: Strong fit.

## MAYBE (0)
""",
        encoding="utf-8",
    )

    entries = go_entries_from_report(report)

    assert len(entries) == 1
    assert entries[0].discovery_url.endswith("item?id=1")
    assert entries[0].alignment_matrix == ["agent workflows -> production systems"]
    assert entries[0].application_angle == "Lead with shipped work."


def test_pack_section_is_inserted_after_go_and_is_idempotent(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\n\n## GO (1)\n\n### Example AI — Founding Engineer\n\n## MAYBE (0)\n", encoding="utf-8")
    result = prepare_go_cases([_go()], cases_root=tmp_path / "cases", today=date(2026, 7, 19), only_new=False)[0]

    insert_pack_report_section(report, [result])
    insert_pack_report_section(report, [result])
    text = report.read_text(encoding="utf-8")

    assert text.index("## LoveWork application packs") < text.index("## MAYBE")
    assert text.count("## LoveWork application packs") == 1

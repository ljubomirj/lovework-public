"""Phase 0-1 agent-to-agent interview preparation tests."""

import json
from datetime import date

import httpx

from agent_interviews import (
    ATA_PREPARED_MARKER,
    ata_case_slug,
    prepare_superme_interview_case,
)
from history import scan_applications
from interview_providers.superme import (
    SuperMePublicAdapter,
    build_dry_run_request,
    validate_agent_spec,
)


ROLE_URL = "https://www.superme.ai/hyperspell/role/43A0pbylxajHuP7wQ8m3"
SPEC = """
dry_run=true
mock=true
POST /interview/start
GET /interview/{interview_id}
POST /interview/{interview_id}/message
manual_intervention == true
status == "awaiting_input"
"""


def _mock_client() -> httpx.Client:
    content = {
        ROLE_URL: "<html><body><h1>Product Engineer</h1><p>Role.</p></body></html>",
        f"{ROLE_URL}/prepare": "<html><body><h1>Agent Setup</h1></body></html>",
        "https://api.superme.ai/v3/agent/.well-known/spec": SPEC,
        "https://raw.githubusercontent.com/superme-ai/superme-sdk/main/README.md": "# SDK\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=content[str(request.url)], request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ata_slug_has_explicit_lovework_ata_suffix():
    assert ata_case_slug(
        date(2026, 7, 23),
        "Hyperspell",
        "Product Engineer",
    ) == "20260723-Hyperspell-Product_Engineer-LoveWork-ATA"


def test_spec_validation_requires_manual_stage_and_recovery_guards():
    result = validate_agent_spec(SPEC)

    assert result["valid"] is True
    assert result["network_writes_permitted"] is False


def test_dry_run_request_is_a_plan_and_is_never_sent():
    request = build_dry_run_request(
        "POST",
        "/interview/start",
        body={"role_id": "role-1"},
    )

    assert "dry_run=true" in request["url"]
    assert "mock=true" in request["url"]
    assert request["send"] is False


def test_public_adapter_fetches_references_and_readable_html(tmp_path):
    adapter = SuperMePublicAdapter(ROLE_URL, client=_mock_client())

    manifest = adapter.fetch_public_references(tmp_path)
    original_manifest_text = (tmp_path / "manifest.json").read_text()
    repeated = adapter.fetch_public_references(tmp_path)

    assert manifest["protocol_validation"]["valid"] is True
    assert repeated == manifest
    assert (tmp_path / "manifest.json").read_text() == original_manifest_text
    assert (tmp_path / "role-page.html").exists()
    assert "Product Engineer" in (tmp_path / "role-page.txt").read_text()
    assert (tmp_path / "preparation-guide.txt").exists()
    assert len(manifest["planned_requests_not_sent"]) == 2


def test_prepare_case_is_idempotent_and_not_prior_application(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_interviews.SuperMePublicAdapter",
        lambda role_url: SuperMePublicAdapter(role_url, client=_mock_client()),
    )

    first = prepare_superme_interview_case(
        principal="lj",
        company="Hyperspell",
        position="Product Engineer",
        role_url=ROLE_URL,
        when=date(2026, 7, 23),
        applications_dir=tmp_path,
    )
    second = prepare_superme_interview_case(
        principal="lj",
        company="Hyperspell",
        position="Product Engineer",
        role_url=ROLE_URL,
        when=date(2026, 7, 23),
        applications_dir=tmp_path,
    )

    assert first.status == "created"
    assert second.status == "existing"
    main_text = (first.path / f"{first.slug}.txt").read_text()
    assert ATA_PREPARED_MARKER in main_text
    assert "hermeo_lj_bot" in main_text
    assert "hermel_lj_bot" in main_text
    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["live_interview_started"] is False
    assert manifest["runtime"]["preferred"]["hermes_profile"] == "hermeo"
    assert scan_applications("Hyperspell", applications_dir=tmp_path) == []
    assert len((first.path / "agent-interview" / "events.jsonl").read_text().splitlines()) == 1

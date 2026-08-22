"""
Tests for the LoveWork MCP server (JSON-RPC over HTTP) and the dashboard's
POST /mcp route.

Two layers tested:
1. mcp_server.handle_request — pure JSON-RPC semantics (no socket).
2. dashboard_server.DashboardHandler — the real HTTP wiring (POST /mcp, GET /).

The HTTP tests boot a DashboardHandler on an ephemeral port in a background
thread and hit it with http.client. No external deps; stdlib only.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import HTTPServer

import pytest

import doc_serve
import mcp_server
from mcp_server import handle_request


# ── Pure JSON-RPC semantics (no socket) ──────────────────────────────────


def _call(req: dict | bytes) -> dict | None:
    body = req if isinstance(req, bytes) else json.dumps(req).encode("utf-8")
    raw = handle_request(body)
    return None if raw is None else json.loads(raw.decode("utf-8"))


def test_initialize_returns_capabilities():
    r = _call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r["jsonrpc"] == "2.0"
    assert r["id"] == 1
    result = r["result"]
    assert "protocolVersion" in result
    assert result["serverInfo"]["name"] == "lovework"
    assert "tools" in result["capabilities"]


def test_tools_list_returns_nine_tools():
    r = _call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = r["result"]["tools"]
    assert len(tools) == 9
    names = {t["name"] for t in tools}
    expected = {
        "crawl_org", "match_profile", "search_jobs", "check_history",
        "fetch_url", "update_wiki", "registry_stats", "run_python", "run_pipeline",
    }
    assert names == expected
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "properties" in t["inputSchema"]


def test_tools_call_registry_stats_returns_envelope():
    """tools/call for a read-only tool wraps the result in MCP content envelope."""
    r = _call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "registry_stats", "arguments": {}}})
    result = r["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert "stats" in payload or "available" in payload


def test_tools_call_unknown_tool_returns_method_not_found():
    r = _call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
               "params": {"name": "no_such_tool", "arguments": {}}})
    assert r["error"]["code"] == -32601
    assert "no_such_tool" in r["error"]["message"]


def test_missing_required_param_returns_invalid_params():
    """check_history requires org_name; omitting it returns -32602."""
    r = _call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": "check_history", "arguments": {}}})
    assert r["error"]["code"] == -32602
    assert "org_name" in r["error"]["message"]


def test_unknown_method_returns_method_not_found():
    r = _call({"jsonrpc": "2.0", "id": 6, "method": "bogus/method", "params": {}})
    assert r["error"]["code"] == -32601


def test_parse_error_on_malformed_json():
    r = _call(b"this is not json")
    assert r["error"]["code"] == -32700
    assert r["id"] is None


def test_notification_returns_none():
    """A request without `id` is a notification — no response body."""
    assert _call({"jsonrpc": "2.0", "method": "tools/list", "params": {}}) is None


def test_protocol_version_is_set():
    assert isinstance(mcp_server.PROTOCOL_VERSION, str)
    assert mcp_server.PROTOCOL_VERSION


# ── HTTP layer (POST /mcp + GET / regression) ────────────────────────────


@pytest.fixture()
def port():
    """Boot the dashboard on an ephemeral port in a background thread; yield port."""
    from dashboard_server import DashboardHandler
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    p = sock.getsockname()[1]
    sock.close()
    server = HTTPServer(("127.0.0.1", p), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield p
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _post(port: int, body: dict | bytes) -> tuple[int, dict | None]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    conn.request("POST", "/mcp", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    parsed = None
    if data:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
    return resp.status, parsed


def test_http_initialize(port):
    status, body = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "lovework"


def test_http_tools_list(port):
    status, body = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert status == 200
    assert len(body["result"]["tools"]) == 9


def test_http_tools_call_registry_stats(port):
    status, body = _post(port, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "registry_stats", "arguments": {}}})
    assert status == 200
    assert body["result"]["isError"] is False


def test_http_notification_returns_202_no_body(port):
    """A notification (no id) gets a 202 with empty body."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/mcp",
                 body=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}}).encode("utf-8"),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 202
    assert data == b""


def test_http_get_root_renders_lan_master_index(port):
    """The root is the stable browser front door for LAN users."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/", headers={"Connection": "close"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="ignore")
    conn.close()
    assert resp.status == 200
    assert "LoveWork — Local LAN" in body
    assert 'href="/dashboard/"' in body


def test_http_dashboard_route_still_renders_detailed_dashboard(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/dashboard/", headers={"Connection": "close"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="ignore")
    conn.close()
    assert resp.status == 200
    assert "LoveWork Dashboard" in body


def test_http_does_not_publish_state_as_a_filesystem_tree(port):
    """Principal results use /principals; raw state remains private."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/state/vj/cache/jobs.csv", headers={"Connection": "close"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 404


def test_http_legacy_candidate_url_redirects_to_canonical_principal_url(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "GET",
        "/candidates/vj/wiki/reports/?raw",
        headers={"Connection": "close"},
    )
    resp = conn.getresponse()
    resp.read()
    location = resp.getheader("Location")
    conn.close()

    assert resp.status == 302
    assert location == "/principals/vj/wiki/reports/?raw"


def test_scoped_doc_serving_uses_public_url_and_blocks_path_escape(tmp_path):
    root = tmp_path / "published"
    root.mkdir()
    (root / "report.md").write_text("# VJ report\n\n[Other](other.md)\n", encoding="utf-8")
    (tmp_path / "private.md").write_text("private", encoding="utf-8")

    result = doc_serve.try_serve_path(
        "report.md",
        "",
        root,
        request_path="/principals/vj/wiki/reports/report.md",
    )

    assert result is not None
    rendered = result["data"].decode("utf-8")
    assert "/principals/vj/wiki/reports/report.md?raw" in rendered
    assert 'href="/principals/vj/wiki/reports/other.md"' in rendered
    assert doc_serve.try_serve_path("../private.md", "", root) is None


def test_principal_public_route_only_maps_allowlisted_areas(tmp_path, monkeypatch):
    import dashboard_server

    published_wiki = tmp_path / "published-wiki"
    published_wiki.mkdir()
    (published_wiki / "report.md").write_text("# Published\n", encoding="utf-8")

    def fake_area_root(principal: str, area: str):
        return published_wiki if (principal, area) == ("vj", "wiki") else None

    monkeypatch.setattr(dashboard_server, "principal_area_root", fake_area_root)
    result = dashboard_server._serve_principal_public_path(
        "/principals/vj/wiki/report.md", ""
    )

    assert result is not None
    assert b"Published" in result["data"]
    assert dashboard_server._serve_principal_public_path("/principals/vj/cache/jobs.csv", "") is None


def test_http_post_non_mcp_path_is_404(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/not-mcp", body=b"{}",
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 404

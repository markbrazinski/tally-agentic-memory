from __future__ import annotations

import json

import httpx
import pytest

from src.external.cockroach_mcp import (
    CockroachManagedMCP,
    ManagedMCPConfig,
    MCPPermissionError,
    MCPUnavailableError,
)


def config(**overrides) -> ManagedMCPConfig:
    values = {
        "cluster_id": "synthetic-cluster-placeholder",
        "database": "synthetic_database",
        "access_token": "test-only-token",
        "service_identity": "synthetic-readonly-runtime",
        "permission_mode": "oauth-read-only",
    }
    values.update(overrides)
    return ManagedMCPConfig(**values)


def _json_response(request, value, status=200, headers=None):
    return httpx.Response(status, json=value, headers=headers, request=request)


def handler(
    *,
    write_tool=False,
    second_page_write=False,
    rows=None,
    status=None,
    session=True,
    write_probe_code=-32601,
    write_probe_result=None,
    write_probe_status=None,
):
    seen = []

    def respond(request: httpx.Request):
        seen.append(request)
        if status is not None:
            return httpx.Response(status, request=request)
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "initialize":
            headers = {"mcp-session-id": "synthetic-session"} if session else None
            return _json_response(
                request,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
                headers=headers,
            )
        if method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if method == "tools/list":
            if payload.get("params", {}).get("cursor") == "page-two":
                return _json_response(
                    request,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [{"name": "insert_rows", "inputSchema": {"type": "object"}}]
                        },
                    },
                )
            tools = [
                {
                    "name": "select_query",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"database": {"type": "string"}, "query": {"type": "string"}},
                        "required": ["database", "query"],
                    },
                }
            ]
            if write_tool:
                tools.append({"name": "insert_rows", "inputSchema": {"type": "object"}})
            result = {"tools": tools}
            if second_page_write:
                result["nextCursor"] = "page-two"
            return _json_response(
                request,
                {"jsonrpc": "2.0", "id": payload["id"], "result": result},
            )
        if method == "tools/call":
            if payload["params"]["name"] == "insert_rows":
                if write_probe_status is not None:
                    return httpx.Response(write_probe_status, request=request)
                if write_probe_result is not None:
                    return _json_response(
                        request,
                        {"jsonrpc": "2.0", "id": payload["id"], "result": write_probe_result},
                    )
                return _json_response(
                    request,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": write_probe_code, "message": "private server detail"},
                    },
                )
            return _json_response(
                request,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"structuredContent": {"rows": rows or []}, "isError": False},
                },
                headers={"x-request-id": "synthetic-request-placeholder"},
            )
        raise AssertionError(method)

    return respond, seen


def test_client_discovers_schema_and_calls_only_select_query():
    respond, seen = handler(rows=[{"case_id": "synthetic"}])
    http = httpx.Client(transport=httpx.MockTransport(respond))

    with CockroachManagedMCP(config(), http_client=http) as mcp:
        result = mcp.select_query(
            "SELECT 'synthetic' AS case_id",
            correlation_id="40000000-0000-4000-8000-000000000001",
        )

    assert result.rows == ({"case_id": "synthetic"},)
    assert result.trace.tool_name == "select_query"
    assert result.trace.server_request_id == "synthetic-request-placeholder"
    payloads = [json.loads(request.content) for request in seen]
    assert [payload["method"] for payload in payloads] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert payloads[-1]["params"] == {
        "name": "select_query",
        "arguments": {
            "database": "synthetic_database",
            "query": "SELECT 'synthetic' AS case_id",
        },
    }
    assert all(request.headers["authorization"] == "Bearer test-only-token" for request in seen)
    assert "test-only-token" not in repr(result.trace)


def test_advertised_write_does_not_expose_an_application_write_path():
    respond, seen = handler(write_tool=True)
    http = httpx.Client(transport=httpx.MockTransport(respond))

    with CockroachManagedMCP(config(), http_client=http) as mcp:
        result = mcp.select_query(
            "SELECT 1",
            correlation_id="40000000-0000-4000-8000-000000000001",
        )

    assert "insert_rows" in result.trace.advertised_tools
    calls = [
        json.loads(request.content)["params"]["name"]
        for request in seen
        if json.loads(request.content)["method"] == "tools/call"
    ]
    assert calls == ["select_query"]


def test_advertised_write_on_second_page_is_recorded_without_execution():
    respond, seen = handler(second_page_write=True)
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        result = mcp.select_query(
            "SELECT 1",
            correlation_id="40000000-0000-4000-8000-000000000001",
        )
    assert "insert_rows" in result.trace.advertised_tools
    tool_lists = [
        json.loads(request.content)
        for request in seen
        if json.loads(request.content)["method"] == "tools/list"
    ]
    assert len(tool_lists) == 2


def test_sessionless_server_still_receives_negotiated_protocol_header():
    respond, seen = handler(session=False)
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        mcp.select_query("SELECT 1", correlation_id="40000000-0000-4000-8000-000000000001")
    methods_and_headers = [
        (json.loads(request.content)["method"], request.headers) for request in seen
    ]
    assert "mcp-protocol-version" not in methods_and_headers[0][1]
    assert all(
        headers["mcp-protocol-version"] == "2025-06-18" for _, headers in methods_and_headers[1:]
    )


def test_live_shaped_write_probe_requires_server_denial_and_redacts_error():
    respond, _ = handler()
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        assert mcp.verify_known_write_tool_denied() is True


def test_invalid_write_arguments_are_not_misreported_as_authorization_denial():
    respond, _ = handler(write_probe_code=-32602)
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPUnavailableError, match="protocol error"):
            mcp.verify_known_write_tool_denied()


def test_unknown_tool_minus_32602_is_accepted_only_with_explicit_semantics():
    respond, _ = handler(write_probe_code=-32602)

    def explicit_unknown(request):
        response = respond(request)
        payload = json.loads(request.content)
        if payload["method"] == "tools/call" and payload["params"]["name"] == "insert_rows":
            return _json_response(
                request,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32602, "message": "Unknown tool: insert_rows"},
                },
            )
        return response

    http = httpx.Client(transport=httpx.MockTransport(explicit_unknown))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        assert mcp.verify_known_write_tool_denied() is True


def test_http_403_on_write_probe_is_explicit_authorization_denial():
    respond, _ = handler(write_probe_status=403)
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        assert mcp.verify_known_write_tool_denied() is True


def test_tool_execution_error_requires_explicit_denial_text():
    respond, _ = handler(
        write_probe_result={
            "isError": True,
            "content": [{"type": "text", "text": "invalid arguments"}],
        }
    )
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPPermissionError, match="did not deny"):
            mcp.verify_known_write_tool_denied()


def test_tool_execution_error_with_explicit_authorization_denial_is_denial():
    respond, _ = handler(
        write_probe_result={
            "isError": True,
            "content": [{"type": "text", "text": "Authorization denied for this tool"}],
        }
    )
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        assert mcp.verify_known_write_tool_denied() is True


def test_provider_write_permission_access_error_is_explicit_denial():
    respond, _ = handler(
        write_probe_result={
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Write access requires an unavailable permission",
                }
            ],
        }
    )
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        assert mcp.verify_known_write_tool_denied() is True


def test_adapter_rejects_non_select_without_network_call():
    respond, seen = handler()
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPPermissionError, match="SELECT"):
            mcp.select_query(
                "UPDATE cases SET state='X'",
                correlation_id="40000000-0000-4000-8000-000000000001",
            )
    assert seen == []


def test_adapter_rejects_stacked_statement_without_network_call():
    respond, seen = handler()
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPPermissionError, match="one SELECT"):
            mcp.select_query(
                "SELECT 1; INSERT INTO cases DEFAULT VALUES",
                correlation_id="40000000-0000-4000-8000-000000000001",
            )
    assert seen == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_redacted_permission_error(status):
    respond, _ = handler(status=status)
    http = httpx.Client(transport=httpx.MockTransport(respond))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPPermissionError) as exc:
            mcp.select_query("SELECT 1", correlation_id="40000000-0000-4000-8000-000000000001")
    assert "test-only-token" not in str(exc.value)


def test_transport_outage_is_recoverable_and_redacted():
    def offline(request):
        raise httpx.ConnectError("private-host-detail", request=request)

    http = httpx.Client(transport=httpx.MockTransport(offline))
    with CockroachManagedMCP(config(), http_client=http) as mcp:
        with pytest.raises(MCPUnavailableError) as exc:
            mcp.select_query("SELECT 1", correlation_id="40000000-0000-4000-8000-000000000001")
    assert str(exc.value) == "Managed MCP transport unavailable"


def test_api_key_mode_is_not_accepted_as_read_only_proof():
    with pytest.raises(MCPPermissionError, match="OAuth"):
        CockroachManagedMCP(config(permission_mode="service-account-api-key"))

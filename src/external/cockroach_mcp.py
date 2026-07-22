"""Narrow Streamable-HTTP client for CockroachDB Cloud Managed MCP.

This adapter intentionally exposes one operation: a read-only ``select_query``.
It discovers the server's live tool schema, exposes only the read operation to
application code, verifies the OAuth identity cannot execute a write, and never
logs bearer credentials or raw responses.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

MCP_ENDPOINT = "https://cockroachlabs.cloud/mcp"
PROTOCOL_VERSION = "2025-06-18"
WRITE_TOOLS = frozenset({"create_database", "create_table", "insert_rows"})
READ_TOOLS = frozenset(
    {
        "list_clusters",
        "get_cluster",
        "list_databases",
        "list_tables",
        "get_table_schema",
        "select_query",
        "explain_query",
        "show_statement",
        "show_running_queries",
    }
)


class MCPUnavailableError(RuntimeError):
    """The Managed MCP service could not provide a trustworthy result."""


class MCPPermissionError(MCPUnavailableError):
    """The configured identity is unauthorized or not demonstrably read-only."""


class MCPProtocolError(MCPUnavailableError):
    """The server returned a malformed or incompatible MCP response."""


class MCPToolDeniedError(MCPPermissionError):
    """Managed MCP explicitly denied a requested tool invocation."""


class MCPAuthorizationDeniedError(MCPPermissionError):
    """Managed MCP returned HTTP 403 for an authenticated request."""


def _explicit_denial_text(value: Any) -> bool:
    text = str(value).lower()
    direct_denial = any(
        marker in text
        for marker in (
            "unknown tool",
            "tool not found",
            "not authorized",
            "authorization denied",
            "unauthorized",
            "forbidden",
            "permission denied",
            "insufficient scope",
        )
    )
    provider_write_denial = all(marker in text for marker in ("permission", "access", "write"))
    return direct_denial or provider_write_denial


@dataclass(frozen=True)
class ManagedMCPConfig:
    cluster_id: str
    database: str
    access_token: str
    service_identity: str
    permission_mode: str
    endpoint: str = MCP_ENDPOINT
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> ManagedMCPConfig:
        values = {
            "cluster_id": os.environ.get("TALLY_MCP_CLUSTER_ID", ""),
            "database": os.environ.get("TALLY_MCP_DATABASE", ""),
            "access_token": os.environ.get("TALLY_MCP_ACCESS_TOKEN", ""),
            "service_identity": os.environ.get("TALLY_MCP_SERVICE_IDENTITY", ""),
            "permission_mode": os.environ.get("TALLY_MCP_PERMISSION_MODE", ""),
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise MCPPermissionError(
                "missing private Managed MCP runtime configuration: " + ", ".join(missing)
            )
        return cls(**values)

    def validate(self) -> None:
        if self.endpoint != MCP_ENDPOINT:
            raise MCPPermissionError("Managed MCP endpoint must be the CockroachDB Cloud endpoint")
        if not self.cluster_id.strip() or not self.database.strip():
            raise MCPPermissionError("cluster and database scope are required")
        if not self.access_token.strip() or not self.service_identity.strip():
            raise MCPPermissionError("credential and service identity are required")
        if self.permission_mode != "oauth-read-only":
            raise MCPPermissionError(
                "Gate 3 requires a live OAuth token authorized for read-only MCP access"
            )
        if self.timeout_seconds <= 0:
            raise MCPPermissionError("MCP timeout must be positive")


@dataclass(frozen=True)
class MCPCallTrace:
    started_at: str
    elapsed_ms: int
    correlation_id: str
    tool_name: str
    service_identity: str
    cluster_id: str
    database: str
    permission_mode: str
    request_id: str
    server_request_id: str | None
    row_count: int
    advertised_tools: tuple[str, ...]

    def as_private_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "correlation_id": self.correlation_id,
            "tool_name": self.tool_name,
            "service_identity": self.service_identity,
            "cluster_id": self.cluster_id,
            "database": self.database,
            "permission_mode": self.permission_mode,
            "request_id": self.request_id,
            "server_request_id": self.server_request_id,
            "row_count": self.row_count,
            "advertised_tools": list(self.advertised_tools),
        }


@dataclass(frozen=True)
class MCPSelectResult:
    rows: tuple[dict[str, Any], ...]
    trace: MCPCallTrace


def _parse_sse(text: str, *, expected_id: int) -> dict[str, Any]:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and value.get("id") == expected_id:
            return dict(value)
    raise MCPProtocolError("MCP event stream did not contain the requested response")


def _normalize_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if all(isinstance(row, Mapping) for row in value):
            return [dict(row) for row in value]
        raise MCPProtocolError("MCP query rows must be JSON objects")
    if isinstance(value, Mapping):
        for key in ("rows", "data", "result"):
            if key in value:
                return _normalize_rows(value[key])
    if isinstance(value, str):
        try:
            return _normalize_rows(json.loads(value))
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP query text was not structured JSON") from exc
    raise MCPProtocolError("MCP query response has no structured rows")


def _rows_from_tool_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    if result.get("isError") is True:
        raise MCPUnavailableError("Managed MCP select_query reported an execution error")
    if "structuredContent" in result:
        return _normalize_rows(result["structuredContent"])
    content = result.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                return _normalize_rows(block.get("text"))
    raise MCPProtocolError("Managed MCP returned no structured query content")


class CockroachManagedMCP:
    """One-session client that can execute only the discovered read tool."""

    def __init__(self, config: ManagedMCPConfig, *, http_client: httpx.Client | None = None):
        config.validate()
        self.config = config
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )
        self._next_id = 1
        self._session_id: str | None = None
        self._protocol_version = PROTOCOL_VERSION
        self._initialized = False

    def __enter__(self) -> CockroachManagedMCP:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._owns_client:
            self._http.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.access_token}",
            "mcp-cluster-id": self.config.cluster_id,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, payload: dict[str, Any], *, expect_response: bool) -> tuple[dict, Any]:
        expected_id = payload.get("id")
        try:
            headers = self._headers()
            headers["Mcp-Method"] = str(payload.get("method", ""))
            params = payload.get("params")
            if payload.get("method") == "tools/call" and isinstance(params, Mapping):
                headers["Mcp-Name"] = str(params.get("name", ""))
            response = self._http.post(
                self.config.endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                raise MCPAuthorizationDeniedError(
                    "Managed MCP denied the requested operation"
                ) from exc
            if exc.response.status_code == 401:
                raise MCPPermissionError("Managed MCP rejected the configured identity") from exc
            raise MCPUnavailableError(
                f"Managed MCP HTTP failure ({exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPUnavailableError("Managed MCP transport unavailable") from exc

        if not expect_response:
            if response.status_code not in {200, 202, 204}:
                raise MCPProtocolError("Managed MCP rejected initialization notification")
            return {}, response

        if not isinstance(expected_id, int):
            raise MCPProtocolError("MCP request ID is missing")
        content_type = response.headers.get("content-type", "").lower()
        try:
            if "text/event-stream" in content_type:
                body = _parse_sse(response.text, expected_id=expected_id)
            else:
                body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MCPProtocolError("Managed MCP response is not valid JSON") from exc
        if not isinstance(body, Mapping) or body.get("id") != expected_id:
            raise MCPProtocolError("Managed MCP response ID mismatch")
        if "error" in body:
            error = body.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else ""
            if code == -32601 or (code == -32602 and _explicit_denial_text(message)):
                raise MCPToolDeniedError("Managed MCP denied the requested tool")
            raise MCPUnavailableError("Managed MCP returned a protocol error")
        result = body.get("result")
        if not isinstance(result, Mapping):
            raise MCPProtocolError("Managed MCP response has no result object")
        return dict(result), response

    def _request(self, method: str, params: dict[str, Any] | None = None) -> tuple[dict, Any, int]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        result, response = self._post(payload, expect_response=True)
        return result, response, request_id

    def _initialize(self) -> None:
        if self._initialized:
            return
        result, response, _ = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tally-gate3-runtime", "version": "1.0"},
            },
        )
        negotiated = result.get("protocolVersion")
        if not isinstance(negotiated, str) or not negotiated:
            raise MCPProtocolError("Managed MCP did not negotiate a protocol version")
        self._protocol_version = negotiated
        self._session_id = response.headers.get("mcp-session-id")
        self._initialized = True
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_response=False,
        )

    def _read_tool(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            result, _, _ = self._request("tools/list", params)
            tools_value = result.get("tools")
            if not isinstance(tools_value, list):
                raise MCPProtocolError("Managed MCP tools/list returned no tool list")
            tools.extend(dict(tool) for tool in tools_value if isinstance(tool, Mapping))
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise MCPProtocolError("Managed MCP tools/list pagination is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise MCPProtocolError("Managed MCP tools/list exceeded pagination limit")
        names = tuple(sorted(str(tool.get("name")) for tool in tools if tool.get("name")))
        unknown_tools = set(names).difference(READ_TOOLS | WRITE_TOOLS)
        if unknown_tools:
            raise MCPPermissionError(
                "configured MCP identity advertises unrecognized tools: "
                + ", ".join(sorted(unknown_tools))
            )
        select_tool = next((tool for tool in tools if tool.get("name") == "select_query"), None)
        if select_tool is None:
            raise MCPPermissionError("configured MCP identity cannot use select_query")
        return select_tool, names

    def verify_known_write_tool_denied(self) -> bool:
        """Execute a no-arguments write-tool request and require server denial.

        Providers may advertise cluster-level tools that the OAuth scope cannot
        execute.  The deliberately incomplete invocation cannot contain row
        data and is accepted only when the live server rejects it.
        """
        self._initialize()
        self._read_tool()
        try:
            result, _, _ = self._request("tools/call", {"name": "insert_rows", "arguments": {}})
        except (MCPToolDeniedError, MCPAuthorizationDeniedError):
            return True
        if result.get("isError") is True and _explicit_denial_text(result.get("content")):
            return True
        raise MCPPermissionError("Managed MCP did not deny the write-tool probe")

    def _select_arguments(self, tool: Mapping[str, Any], query: str) -> dict[str, Any]:
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            raise MCPProtocolError("select_query has no input schema")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise MCPProtocolError("select_query input schema has no properties")
        query_key = next((key for key in ("query", "sql", "statement") if key in properties), None)
        database_key = next(
            (key for key in ("database", "database_name", "databaseName") if key in properties),
            None,
        )
        if query_key is None or database_key is None:
            raise MCPProtocolError("select_query schema has no recognized query/database fields")
        arguments = {query_key: query, database_key: self.config.database}
        required = schema.get("required", [])
        if isinstance(required, list):
            unsupported = set(required).difference(arguments)
            if unsupported:
                raise MCPProtocolError(
                    "select_query requires unsupported fields: " + ", ".join(sorted(unsupported))
                )
        return arguments

    def select_query(self, query: str, *, correlation_id: str) -> MCPSelectResult:
        """Execute one SELECT; no application API is exposed for advertised writes."""
        candidate = query.strip()
        if not candidate.upper().startswith("SELECT") or ";" in candidate:
            raise MCPPermissionError("MCP adapter accepts one SELECT statement only")
        self._initialize()
        tool, names = self._read_tool()
        arguments = self._select_arguments(tool, query)
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        started = time.monotonic()
        result, response, request_id = self._request(
            "tools/call", {"name": "select_query", "arguments": arguments}
        )
        rows = _rows_from_tool_result(result)
        trace = MCPCallTrace(
            started_at=started_at,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            correlation_id=correlation_id,
            tool_name="select_query",
            service_identity=self.config.service_identity,
            cluster_id=self.config.cluster_id,
            database=self.config.database,
            permission_mode=self.config.permission_mode,
            request_id=str(request_id),
            server_request_id=(
                response.headers.get("x-request-id")
                or response.headers.get("x-cockroach-request-id")
            ),
            row_count=len(rows),
            advertised_tools=names,
        )
        return MCPSelectResult(rows=tuple(rows), trace=trace)

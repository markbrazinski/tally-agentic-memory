"""Narrow Streamable-HTTP client for CockroachDB Cloud Managed MCP.

This adapter intentionally exposes one operation: a read-only ``select_query``.
It discovers the server's live tool schema, exposes only the read operation to
application code, verifies the OAuth identity cannot execute a write, and never
logs bearer credentials or raw responses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

MCP_ENDPOINT = "https://cockroachlabs.cloud/mcp"
PROTOCOL_VERSION = "2025-11-25"
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
OBSERVED_COCKROACH_READ_SCOPE_DENIAL_SHA256 = (
    "2a95a5f43db2db6a6181556535a8c0c8201e7cbd44f3ff64fc760bf08210e5e7"
)


class MCPUnavailableError(RuntimeError):
    """The Managed MCP service could not provide a trustworthy result."""


class MCPPermissionError(MCPUnavailableError):
    """The configured identity is unauthorized or not demonstrably read-only."""


class MCPAuthenticationError(MCPPermissionError):
    """Managed MCP rejected an expired or invalid bearer token with HTTP 401."""


class MCPProtocolError(MCPUnavailableError):
    """The server returned a malformed or incompatible MCP response."""


class MCPAuthorizationDeniedError(MCPPermissionError):
    """Managed MCP returned OAuth insufficient_scope for the write scope."""


class MCPForbiddenError(MCPPermissionError):
    """Managed MCP returned a 403 that was not a write-scope denial proof."""


def _explicit_write_scope_denial(response: httpx.Response) -> bool:
    """Require OAuth insufficient_scope semantics for the write capability."""
    header = response.headers.get("www-authenticate", "")
    if not header.lower().startswith("bearer "):
        return False
    parameters = {
        key.lower(): value
        for key, value in re.findall(r'([A-Za-z][A-Za-z0-9_-]*)="([^"\\]*)"', header)
    }
    return parameters.get("error") == "insufficient_scope" and "mcp:write" in set(
        parameters.get("scope", "").split()
    )


def _explicit_inband_authorization_denial(result: Mapping[str, Any]) -> bool:
    """Recognize Cockroach's observed, undocumented permission-denial shape.

    This is deliberately not generic permission-text matching. It is accepted
    only for an ``isError`` result from the hard-coded write probe whose
    normalized text exactly matches the one-way fingerprint observed live.
    """
    content = result.get("content")
    if result.get("isError") is not True or not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != "text":
        return False
    normalized = " ".join(str(block.get("text", "")).lower().split())
    observed = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return secrets.compare_digest(
        observed, OBSERVED_COCKROACH_READ_SCOPE_DENIAL_SHA256
    )


@dataclass(frozen=True)
class ManagedMCPConfig:
    cluster_id: str
    database: str
    access_token: str = field(repr=False)
    service_identity: str
    permission_mode: str
    endpoint: str = MCP_ENDPOINT
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls, *, access_token: str | None = None) -> ManagedMCPConfig:
        oauth_runtime = access_token is not None
        values = {
            "cluster_id": os.environ.get("TALLY_MCP_CLUSTER_ID", ""),
            "database": os.environ.get("TALLY_MCP_DATABASE", ""),
            # Runtime callers pass the OAuth-manager token explicitly.  The
            # environment lookup remains only for older private operator
            # tools, never for the public demo path.
            "access_token": (
                access_token
                if access_token is not None
                else os.environ.get("TALLY_MCP_ACCESS_TOKEN", "")
            ),
            "service_identity": (
                "oauth-read-only-client"
                if oauth_runtime
                else os.environ.get("TALLY_MCP_SERVICE_IDENTITY", "")
            ),
            "permission_mode": (
                "oauth-read-only"
                if oauth_runtime
                else os.environ.get("TALLY_MCP_PERMISSION_MODE", "")
            ),
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
                "Managed MCP requires a live OAuth token authorized for read-only access"
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
        http_failure: MCPUnavailableError | None = None
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
            if exc.response.status_code == 403 and _explicit_write_scope_denial(exc.response):
                http_failure = MCPAuthorizationDeniedError(
                    "Managed MCP denied the requested operation"
                )
            elif exc.response.status_code == 401:
                http_failure = MCPAuthenticationError(
                    "Managed MCP rejected the configured identity"
                )
            elif exc.response.status_code == 403:
                http_failure = MCPForbiddenError("Managed MCP forbade the request")
            else:
                http_failure = MCPUnavailableError(
                    f"Managed MCP HTTP failure ({exc.response.status_code})"
                )
        except httpx.HTTPError:
            http_failure = MCPUnavailableError("Managed MCP transport unavailable")
        if http_failure is not None:
            raise http_failure

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

    def _discover_tools(self) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
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
        return tools, names

    def _read_tool(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        tools, names = self._discover_tools()
        select_tool = next((tool for tool in tools if tool.get("name") == "select_query"), None)
        if select_tool is None:
            raise MCPPermissionError("configured MCP identity cannot use select_query")
        return select_tool, names

    def verify_known_write_tool_denied(self) -> bool:
        """Execute a non-mutating write-tool probe and require authorization denial.

        The probe targets a fresh random table name through ``insert_rows``.
        It cannot create a table, so it cannot mutate the isolated database.
        Only an explicit authorization denial passes. Validation, missing-table,
        or other execution errors mean the credential was not proven read-only.
        """
        self._initialize()
        tools, _ = self._discover_tools()
        insert_tool = next((tool for tool in tools if tool.get("name") == "insert_rows"), None)
        if insert_tool is None:
            raise MCPProtocolError("Managed MCP did not advertise the known write probe tool")
        arguments = self._write_probe_arguments(insert_tool)
        try:
            result, _, _ = self._request(
                "tools/call", {"name": "insert_rows", "arguments": arguments}
            )
        except MCPAuthorizationDeniedError:
            return True
        if _explicit_inband_authorization_denial(result):
            return True
        raise MCPPermissionError("Managed MCP did not deny the write-tool probe")

    def _write_probe_arguments(self, tool: Mapping[str, Any]) -> dict[str, Any]:
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            raise MCPProtocolError("insert_rows has no input schema")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise MCPProtocolError("insert_rows input schema has no properties")
        table_name = f"__tally_gate5_write_denial_probe_{uuid4().hex}"
        insert_query = (
            f'INSERT INTO "{table_name}" (gate5_probe) '
            "VALUES ('denial-required')"
        )
        choices = {
            "database": self.config.database,
            "database_name": self.config.database,
            "databaseName": self.config.database,
            "query": insert_query,
            "sql": insert_query,
            "statement": insert_query,
            "schema": "public",
            "schema_name": "public",
            "schemaName": "public",
            "table": table_name,
            "table_name": table_name,
            "tableName": table_name,
            "rows": [{"gate5_probe": "denial-required"}],
            "columns": ["gate5_probe"],
            "values": [["denial-required"]],
        }
        arguments = {name: value for name, value in choices.items() if name in properties}
        has_structured_insert = any(
            key in arguments for key in ("table", "table_name", "tableName")
        ) and any(key in arguments for key in ("rows", "values"))
        has_fixed_query = any(key in arguments for key in ("query", "sql", "statement"))
        if not has_structured_insert and not has_fixed_query:
            raise MCPProtocolError("insert_rows schema has no safe recognized probe shape")
        required = schema.get("required", [])
        if isinstance(required, list):
            unsupported = set(required).difference(arguments)
            if unsupported:
                raise MCPProtocolError(
                    "insert_rows requires unsupported fields: " + ", ".join(sorted(unsupported))
                )
        return arguments

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

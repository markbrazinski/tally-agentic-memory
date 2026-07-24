"""Tests for the fixed Managed MCP reconstruction read (no direct-DB fallback)."""

from __future__ import annotations

import pytest

from src.external.cockroach_mcp import (
    MCPCallTrace,
    MCPProtocolError,
    MCPSelectResult,
    MCPUnavailableError,
)
from src.external.reconstruction_mcp import (
    RECONSTRUCTION_MEMORY_VIEW,
    build_reconstruction_query,
    read_reconstruction_memory,
)

CUTOFF = "2026-06-22T08:00:00Z"


def test_query_is_single_bounded_select():
    q = build_reconstruction_query(
        shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
        knowledge_cutoff_iso=CUTOFF,
    )
    assert q.startswith("SELECT ")
    assert ";" not in q
    assert RECONSTRUCTION_MEMORY_VIEW in q
    # Cutoff constraint present and deterministic ordering.
    assert "recorded_at <= '2026-06-22T08:00:00Z'" in q
    assert q.rstrip().endswith("ORDER BY occurred_at, public_ref")


def test_query_refuses_unsafe_scope_literal():
    with pytest.raises(MCPProtocolError):
        build_reconstruction_query(
            shipment_ref="X'; DROP TABLE t;--",
            container_ref="C",
            knowledge_cutoff_iso=CUTOFF,
        )


class _FakeMCP:
    def __init__(self, rows, *, raise_exc=None):
        self._rows = rows
        self._raise = raise_exc
        self.queries = []

    def select_query(self, query, *, correlation_id):
        self.queries.append(query)
        if self._raise is not None:
            raise self._raise
        trace = MCPCallTrace(
            started_at="2026-06-22T08:00:00Z",
            elapsed_ms=12,
            correlation_id=correlation_id,
            tool_name="select_query",
            service_identity="oauth-read-only-client",
            cluster_id="c",
            database="d",
            permission_mode="oauth-read-only",
            request_id="1",
            server_request_id="srv-1",
            row_count=len(self._rows),
            advertised_tools=("select_query",),
        )
        return MCPSelectResult(rows=tuple(self._rows), trace=trace)


def _good_row(ref="SE-001"):
    return {
        "public_ref": ref,
        "event_type": "GATE_OUT",
        "shipment_ref": "TLLU4829317",
        "container_ref": "TLLU4829317",
        "source_public_ref": "SRC-1",
        "source_verification_state": "VERIFIED",
        "display_anchor": "row 1",
        "provenance_classification": "DEMO_SCENARIO",
        "occurred_at": "2026-06-14T17:00:00+00:00",
        "recorded_at": "2026-06-20T00:00:00+00:00",
    }


def test_read_maps_rows_to_raw_events():
    mcp = _FakeMCP([_good_row(), _good_row("SE-002")])
    result = read_reconstruction_memory(
        mcp,
        shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
        knowledge_cutoff_iso=CUTOFF,
        correlation_id="task-1",
    )
    assert result.returned_row_count == 2
    assert [r.public_ref for r in result.rows] == ["SE-001", "SE-002"]
    assert result.server_request_id == "srv-1"


def test_missing_required_column_is_protocol_error_not_dropped_row():
    bad = _good_row()
    del bad["occurred_at"]
    mcp = _FakeMCP([bad])
    with pytest.raises(MCPProtocolError):
        read_reconstruction_memory(
            mcp,
            shipment_ref="TLLU4829317",
            container_ref="TLLU4829317",
            knowledge_cutoff_iso=CUTOFF,
            correlation_id="task-1",
        )


def test_mcp_unavailable_propagates_no_fallback():
    mcp = _FakeMCP([], raise_exc=MCPUnavailableError("down"))
    with pytest.raises(MCPUnavailableError):
        read_reconstruction_memory(
            mcp,
            shipment_ref="TLLU4829317",
            container_ref="TLLU4829317",
            knowledge_cutoff_iso=CUTOFF,
            correlation_id="task-1",
        )
    # The adapter issued exactly one MCP query and never touched a driver.
    assert len(mcp.queries) == 1

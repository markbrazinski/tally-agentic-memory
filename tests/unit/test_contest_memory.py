from __future__ import annotations

from dataclasses import replace

from src.external.cockroach_mcp import MCPCallTrace, MCPSelectResult, MCPUnavailableError
from src.platform.contest_memory import build_sealed_memory_query, retrieve_contest_memory
from tests.unit.test_sealed_memory import CASE_ID, CONTEST_ID, TENANT_ID, sealed_rows


def trace(row_count: int = 1) -> MCPCallTrace:
    return MCPCallTrace(
        started_at="2026-07-20T18:01:00Z",
        elapsed_ms=12,
        correlation_id="40000000-0000-4000-8000-000000000001",
        tool_name="select_query",
        service_identity="synthetic-readonly-runtime",
        cluster_id="synthetic-cluster-placeholder",
        database="synthetic_database",
        permission_mode="oauth-read-only",
        request_id="3",
        server_request_id="synthetic-request-placeholder",
        row_count=row_count,
        advertised_tools=("select_query",),
    )


class Selector:
    def __init__(self, rows=None, error=None):
        self.rows = sealed_rows() if rows is None else rows
        self.error = error
        self.calls = []

    def select_query(self, query, *, correlation_id):
        self.calls.append((query, correlation_id))
        if self.error:
            raise self.error
        return MCPSelectResult(
            tuple(self.rows), replace(trace(len(self.rows)), correlation_id=correlation_id)
        )


def test_application_path_retrieves_verified_sealed_memory():
    selector = Selector()
    correlation = "40000000-0000-4000-8000-000000000001"

    outcome = retrieve_contest_memory(
        selector,
        tenant_id=TENANT_ID,
        case_id=CASE_ID,
        contest_id=CONTEST_ID,
        correlation_id=correlation,
    )

    assert outcome.status == "found"
    assert outcome.memory is not None
    assert outcome.memory.case_id == CASE_ID
    assert len(selector.calls) == 1
    sql, used_correlation = selector.calls[0]
    assert f"ca.tenant_id = '{TENANT_ID}'::UUID" in sql
    assert f"ca.id = '{CASE_ID}'::UUID" in sql
    assert f"co.id = '{CONTEST_ID}'::UUID" in sql
    assert "ce.tenant_id = ca.tenant_id" in sql
    assert "ca.state IN ('FILED', 'CONTESTED', 'RESOLVED')" in sql
    assert used_correlation == correlation


def test_wrong_tenant_unknown_or_unsealed_is_clean_not_found():
    outcome = retrieve_contest_memory(
        Selector(rows=[]), tenant_id=TENANT_ID, case_id=CASE_ID, contest_id=CONTEST_ID
    )
    assert outcome.status == "not_found"
    assert outcome.memory is None


def test_outage_is_recoverable_and_never_invents_memory():
    outcome = retrieve_contest_memory(
        Selector(error=MCPUnavailableError("offline")),
        tenant_id=TENANT_ID,
        case_id=CASE_ID,
        contest_id=CONTEST_ID,
    )
    assert outcome.status == "unavailable"
    assert outcome.error_code == "mcp_unavailable"
    assert outcome.memory is None
    assert outcome.mcp_trace is None


def test_malformed_return_is_unavailable_not_memory():
    rows = sealed_rows()
    rows[0]["evidence_hash"] = "sha256:" + "0" * 64
    outcome = retrieve_contest_memory(
        Selector(rows=rows), tenant_id=TENANT_ID, case_id=CASE_ID, contest_id=CONTEST_ID
    )
    assert outcome.status == "unavailable"
    assert outcome.error_code == "invalid_sealed_receipt"
    assert outcome.memory is None


def test_query_builder_rejects_injection_before_sql_construction():
    try:
        build_sealed_memory_query(
            tenant_id=TENANT_ID + "' OR true --",
            case_id=CASE_ID,
            contest_id=CONTEST_ID,
            correlation_id="40000000-0000-4000-8000-000000000001",
        )
    except ValueError as exc:
        assert "tenant_id must be a UUID" in str(exc)
    else:
        raise AssertionError("non-UUID tenant value was accepted")

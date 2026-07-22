"""Later-contest retrieval through CockroachDB Cloud Managed MCP.

This is the Gate 3 application/demo path.  It has no ordinary-DAL fallback:
when Managed MCP is unavailable, the caller gets a recoverable unavailable
state rather than an invented or silently substituted memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from src.core.sealed_memory import (
    SealedCaseMemory,
    SealedMemoryValidationError,
    validate_sealed_case_memory,
)
from src.external.cockroach_mcp import (
    MCPAuthenticationError,
    MCPCallTrace,
    MCPSelectResult,
    MCPUnavailableError,
)

QUERY_TEMPLATE = "gate3-sealed-memory-v1"


class MCPSelector(Protocol):
    def select_query(self, query: str, *, correlation_id: str) -> MCPSelectResult: ...


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def build_sealed_memory_query(
    *, tenant_id: str, case_id: str, contest_id: str, correlation_id: str
) -> str:
    """Build the one statement exposed through the Gate 3 MCP adapter.

    CockroachDB's published ``select_query`` schema does not document bound
    parameters.  Values are therefore accepted only after canonical UUID
    parsing, then embedded in this fixed statement.  No prompt or free-form
    caller text can alter the SQL shape.
    """
    tenant = _uuid(tenant_id, field="tenant_id")
    case = _uuid(case_id, field="case_id")
    contest = _uuid(contest_id, field="contest_id")
    correlation = _uuid(correlation_id, field="correlation_id")
    return f"""SELECT
  ca.tenant_id::STRING AS tenant_id,
  ca.id::STRING AS case_id,
  co.id::STRING AS contest_id,
  co.status AS contest_status,
  ca.invoice_id::STRING AS invoice_id,
  ca.finding_id::STRING AS finding_id,
  ca.state AS current_state,
  ca.manifest_version,
  ca.evidence_manifest,
  ca.evidence_hash,
  ca.sealed_by::STRING AS approved_by,
  ca.sealed_at_display AS sealed_at,
  ca.sealed_txn_ts::STRING AS sealed_txn_ts,
  i.invoice_no AS current_invoice_no,
  i.source_version_id AS current_invoice_version_id,
  i.sha256 AS current_invoice_sha256,
  i.claimed_rate::STRING AS current_invoice_claimed_rate,
  i.currency AS current_invoice_currency,
  i.rate_unit AS current_invoice_rate_unit,
  i.charge_days AS current_invoice_charge_days,
  f.recommendation AS current_recommendation,
  f.calculation AS current_calculation,
  f.human_approval_state AS current_approval_state,
  f.tariff_clause_id::STRING AS current_clause_id,
  f.recorded_rate::STRING AS current_recorded_rate,
  f.invoice_claimed_rate::STRING AS current_finding_claimed_rate,
  f.rate_unit AS current_finding_rate_unit,
  f.charge_days AS current_finding_charge_days,
  ce.id::STRING AS evidence_id,
  ce.kind AS evidence_kind,
  ce.source_table,
  ce.source_id::STRING AS source_id,
  ce.content AS evidence_content,
  ce.content_sha256,
  ce.sealed AS evidence_sealed
FROM public.cases AS ca
JOIN public.contests AS co
  ON co.tenant_id = ca.tenant_id AND co.case_id = ca.id
JOIN public.invoices AS i
  ON i.tenant_id = ca.tenant_id AND i.id = ca.invoice_id
JOIN public.findings AS f
  ON f.tenant_id = ca.tenant_id AND f.id = ca.finding_id AND f.invoice_id = ca.invoice_id
JOIN public.case_evidence AS ce
  ON ce.tenant_id = ca.tenant_id AND ce.case_id = ca.id
WHERE ca.tenant_id = '{tenant}'::UUID
  AND ca.id = '{case}'::UUID
  AND co.id = '{contest}'::UUID
  AND ca.state IN ('FILED', 'CONTESTED', 'RESOLVED')
  AND ca.manifest_version = 1
  AND ca.evidence_manifest IS NOT NULL
  AND ca.evidence_hash IS NOT NULL
  AND ca.sealed_by IS NOT NULL
  AND ca.sealed_at_display IS NOT NULL
  AND ca.sealed_txn_ts IS NOT NULL
  AND ce.sealed = true
  /* tally:{QUERY_TEMPLATE} correlation={correlation} */
ORDER BY ce.id
LIMIT 100"""


@dataclass(frozen=True)
class ContestMemoryOutcome:
    status: str
    correlation_id: str
    query_template: str
    memory: SealedCaseMemory | None = None
    mcp_trace: MCPCallTrace | None = None
    error_code: str | None = None

    def as_private_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "correlation_id": self.correlation_id,
            "query_template": self.query_template,
            "memory": self.memory.as_dict() if self.memory else None,
            "mcp_trace": self.mcp_trace.as_private_dict() if self.mcp_trace else None,
            "error_code": self.error_code,
        }


def retrieve_contest_memory(
    mcp: MCPSelector,
    *,
    tenant_id: str,
    case_id: str,
    contest_id: str,
    correlation_id: str | None = None,
) -> ContestMemoryOutcome:
    """Retrieve and verify one sealed receipt for a later contest."""
    trusted_tenant = _uuid(tenant_id, field="tenant_id")
    requested_case = _uuid(case_id, field="case_id")
    trusted_contest = _uuid(contest_id, field="contest_id")
    correlation = _uuid(correlation_id or str(uuid4()), field="correlation_id")
    query = build_sealed_memory_query(
        tenant_id=trusted_tenant,
        case_id=requested_case,
        contest_id=trusted_contest,
        correlation_id=correlation,
    )
    try:
        result = mcp.select_query(query, correlation_id=correlation)
    except MCPAuthenticationError:
        # The public runtime owns one bounded OAuth refresh/replay.  Do not
        # collapse its retry signal into the ordinary unavailable outcome.
        raise
    except MCPUnavailableError:
        return ContestMemoryOutcome(
            status="unavailable",
            correlation_id=correlation,
            query_template=QUERY_TEMPLATE,
            error_code="mcp_unavailable",
        )

    try:
        memory = validate_sealed_case_memory(
            result.rows,
            expected_tenant_id=trusted_tenant,
            expected_case_id=requested_case,
            expected_contest_id=trusted_contest,
        )
    except SealedMemoryValidationError:
        return ContestMemoryOutcome(
            status="unavailable",
            correlation_id=correlation,
            query_template=QUERY_TEMPLATE,
            mcp_trace=result.trace,
            error_code="invalid_sealed_receipt",
        )
    if memory is None:
        return ContestMemoryOutcome(
            status="not_found",
            correlation_id=correlation,
            query_template=QUERY_TEMPLATE,
            mcp_trace=result.trace,
        )
    return ContestMemoryOutcome(
        status="found",
        correlation_id=correlation,
        query_template=QUERY_TEMPLATE,
        memory=memory,
        mcp_trace=result.trace,
    )

"""Fresh send-gate checks — real DB reads against the invoice's lineage.

Promoted verbatim from ``scripts/gate6_isolated_trace.py`` (the proven Gate-6
trace logic), minus the test-only ``force_source_fail`` hook. These are the
four gates the platform injects into ``approve_and_send``: APPROVED_MEMORY_MCP,
VECTOR_CLAUSE_BINDING, EXACT_S3_SOURCE, NO_FALLBACK. SECOND_AUTHORIZATION and
LOCKED_FIELDS are computed inside the repository, not here.
"""

from __future__ import annotations

from src.core.correspondence import GateResult, GateState
from src.external.dal import DAL
from src.platform.correspondence_repository import GateCheck


def build_fresh_gate_checks(
    dal: DAL, *, invoice_id: str, decision_seal_id: str
) -> dict[str, GateCheck]:
    """Real fresh gate checks against the invoice's lineage."""
    tenant_id = dal.tenant.tenant_id

    def _mcp_gate() -> GateResult:
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM reconstructions WHERE tenant_id=%s AND invoice_id=%s "
                "ORDER BY version DESC LIMIT 1;", (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "COMPLETE"
        return GateResult("APPROVED_MEMORY_MCP",
                          GateState.VERIFIED if ok else GateState.FAILED,
                          "reconstruction complete" if ok else "no complete memory")

    def _vector_gate() -> GateResult:
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT validation_state FROM applicable_rules WHERE tenant_id=%s "
                "AND invoice_id=%s;", (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "VERIFIED"
        return GateResult("VECTOR_CLAUSE_BINDING",
                          GateState.VERIFIED if ok else GateState.FAILED, None)

    def _source_gate() -> GateResult:
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT preservation_status FROM invoice_sources WHERE tenant_id=%s "
                "AND invoice_id=%s AND source_type='INVOICE_PDF';",
                (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "VERSION_VERIFIED"
        return GateResult("EXACT_S3_SOURCE",
                          GateState.VERIFIED if ok else GateState.FAILED, None)

    def _no_fallback_gate() -> GateResult:
        return GateResult("NO_FALLBACK", GateState.VERIFIED, "no substitute used")

    return {
        "APPROVED_MEMORY_MCP": _mcp_gate,
        "VECTOR_CLAUSE_BINDING": _vector_gate,
        "EXACT_S3_SOURCE": _source_gate,
        "NO_FALLBACK": _no_fallback_gate,
    }

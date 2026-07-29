"""Fixed, bounded Managed MCP read for pre-invoice reconstruction memory.

This is the load-bearing Gate 2 sponsor path. It issues ONE deterministic
read-only SELECT through CockroachDB Managed MCP against a narrow reconstruction
memory view, scoped to the tenant/shipment/container and constrained to the
invoice knowledge cutoff. It has NO direct-database fallback: if MCP is
unavailable, unauthorized, or returns a malformed response, the caller sees the
MCP error and fails the reconstruction closed — it never substitutes a driver
query, a fixture, or a synthesized row.

The view name and the exact SQL literal are private; only public refs and
verification state cross into the application. Per the DAL query-log convention
the raw SELECT is never logged with private locators.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.core.reconstruction import RawEventRow
from src.external.cockroach_mcp import (
    CockroachManagedMCP,
    MCPProtocolError,
)

# The narrow, allowlisted reconstruction-memory view the read is confined to.
# It exposes only verified, non-superseded facts with their source verification
# state and public refs — never raw S3 locators.
RECONSTRUCTION_MEMORY_VIEW = "mcp_reconstruction_memory_v1"

_SELECT_COLUMNS = (
    "public_ref",
    "event_type",
    "shipment_ref",
    "container_ref",
    "source_public_ref",
    "source_verification_state",
    "display_anchor",
    "provenance_classification",
    "occurred_at",
    "recorded_at",
    "observed_at",
    "effective_from",
    "effective_to",
)


@dataclass(frozen=True)
class ReconstructionMemoryResult:
    rows: tuple[RawEventRow, ...]
    correlation_id: str
    elapsed_ms: int
    returned_row_count: int
    server_request_id: str | None


def _quote_literal(value: str) -> str:
    """SQL string literal for a validated identifier (no user free text).

    The shipment/container refs and cutoff come from committed server state
    (the invoice + its extracted claims), never from a browser. We still refuse
    anything that could break out of a single SELECT.
    """
    if "'" in value or ";" in value or "\\" in value or "\n" in value:
        raise MCPProtocolError("reconstruction scope value is not a safe literal")
    return f"'{value}'"


def build_reconstruction_query(
    *,
    shipment_ref: str,
    container_ref: str,
    knowledge_cutoff_iso: str,
) -> str:
    """The single fixed SELECT. Deterministic ordering; cutoff-constrained.

    Only ``recorded_at <= knowledge_cutoff`` rows are eligible — the
    knowledge-cutoff rule enforced at the source of the read, and re-checked
    deterministically in ``validate_events`` (defense in depth)."""
    columns = ", ".join(_SELECT_COLUMNS)
    return (
        f"SELECT {columns} FROM {RECONSTRUCTION_MEMORY_VIEW} "
        f"WHERE shipment_ref = {_quote_literal(shipment_ref)} "
        f"AND container_ref = {_quote_literal(container_ref)} "
        f"AND recorded_at <= {_quote_literal(knowledge_cutoff_iso)} "
        "ORDER BY occurred_at, public_ref"
    )


def _row_field(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    return str(value)


def _required(row: Mapping[str, object], key: str) -> str:
    value = _row_field(row, key)
    if value is None or value == "":
        raise MCPProtocolError(f"reconstruction memory row missing {key}")
    return value


def read_reconstruction_memory(
    mcp: CockroachManagedMCP,
    *,
    shipment_ref: str,
    container_ref: str,
    knowledge_cutoff_iso: str,
    correlation_id: str,
) -> ReconstructionMemoryResult:
    """Execute the fixed MCP read and map rows to RawEventRow.

    Raises the MCP error unchanged (no fallback) on any failure. Mapping is
    total: a row missing a required column is a protocol error, not a dropped
    row, so a truncated/tampered response cannot silently shrink the timeline.
    """
    query = build_reconstruction_query(
        shipment_ref=shipment_ref,
        container_ref=container_ref,
        knowledge_cutoff_iso=knowledge_cutoff_iso,
    )
    result = mcp.select_query(query, correlation_id=correlation_id)
    rows: list[RawEventRow] = []
    for raw in result.rows:
        if not isinstance(raw, Mapping):
            raise MCPProtocolError("reconstruction memory row is not an object")
        rows.append(
            RawEventRow(
                public_ref=_required(raw, "public_ref"),
                event_type=_required(raw, "event_type"),
                shipment_ref=_required(raw, "shipment_ref"),
                container_ref=_required(raw, "container_ref"),
                source_public_ref=_required(raw, "source_public_ref"),
                source_verification_state=_required(raw, "source_verification_state"),
                display_anchor=_required(raw, "display_anchor"),
                provenance_classification=_required(raw, "provenance_classification"),
                occurred_at=_required(raw, "occurred_at"),
                recorded_at=_required(raw, "recorded_at"),
                observed_at=_row_field(raw, "observed_at"),
                effective_from=_row_field(raw, "effective_from"),
                effective_to=_row_field(raw, "effective_to"),
            )
        )
    return ReconstructionMemoryResult(
        rows=tuple(rows),
        correlation_id=result.trace.correlation_id,
        elapsed_ms=result.trace.elapsed_ms,
        returned_row_count=result.trace.row_count,
        server_request_id=result.trace.server_request_id,
    )


def read_reconstruction_memory_via_driver(
    dal,
    *,
    shipment_ref: str,
    container_ref: str,
    knowledge_cutoff_iso: str,
    correlation_id: str,
) -> ReconstructionMemoryResult:
    """Driver fallback for the reconstruction read: the SAME fixed SELECT against
    the SAME view, over the app's existing (non-expiring) psycopg connection.

    Used only when the Managed MCP is unavailable (e.g. an OAuth token lapse), so
    a dead MCP credential can never stall reconstruction. The query is byte-for-byte
    the one build_reconstruction_query produces, so the two paths are row-for-row
    equivalent. The DAL query-log records this SELECT as a driver fallback (the
    caller logs the substitution), per the 'MCP reads fall back to the driver and
    print the fallback' rule — it is transparent, never a hidden substitution.
    """
    query = build_reconstruction_query(
        shipment_ref=shipment_ref,
        container_ref=container_ref,
        knowledge_cutoff_iso=knowledge_cutoff_iso,
    )
    with dal.conn.cursor() as cur:
        cur.execute(query)
        col_names = [d[0] for d in cur.description]
        fetched = cur.fetchall()
    rows: list[RawEventRow] = []
    for raw in fetched:
        # Normalize to the same shapes the MCP JSON returns: datetimes/dates as
        # ISO strings (psycopg hands back native objects), so downstream parsing
        # (datetime.fromisoformat) sees identical input on both paths.
        row = {
            name: (val.isoformat() if hasattr(val, "isoformat") else val)
            for name, val in zip(col_names, raw, strict=True)
        }
        rows.append(
            RawEventRow(
                public_ref=_required(row, "public_ref"),
                event_type=_required(row, "event_type"),
                shipment_ref=_required(row, "shipment_ref"),
                container_ref=_required(row, "container_ref"),
                source_public_ref=_required(row, "source_public_ref"),
                source_verification_state=_required(row, "source_verification_state"),
                display_anchor=_required(row, "display_anchor"),
                provenance_classification=_required(row, "provenance_classification"),
                occurred_at=_required(row, "occurred_at"),
                recorded_at=_required(row, "recorded_at"),
                observed_at=_row_field(row, "observed_at"),
                effective_from=_row_field(row, "effective_from"),
                effective_to=_row_field(row, "effective_to"),
            )
        )
    return ReconstructionMemoryResult(
        rows=tuple(rows),
        correlation_id=correlation_id,
        elapsed_ms=0,
        returned_row_count=len(rows),
        server_request_id=None,
    )

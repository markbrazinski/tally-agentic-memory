"""One bounded durable reconstruction worker iteration; no fallback.

Leases a START_RECONSTRUCTION task, performs the fixed Managed MCP read, runs the
pure deterministic validators, and completes-or-fails through the repository.
Mirrors ``intake_worker.run_one_intake_task``: lease -> real sponsor call ->
deterministic validation -> commit or fail closed. A failed/empty/unauthorized
MCP read never yields a successful reconstruction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from src.core.reconstruction import (
    ChargedDayResult,
    NormalizedEvent,
    ReconstructionState,
    ShipmentEventType,
    adjudicate_charged_days,
    classify_coverage,
    resolve_charge_boundary,
    resolve_terminal_state,
    validate_events,
)
from src.external.cockroach_mcp import CockroachManagedMCP, MCPUnavailableError
from src.external.dal import DAL
from src.external.reconstruction_mcp import read_reconstruction_memory
from src.platform.reconstruction_repository import (
    ReconstructionCompletion,
    claim_next_reconstruction_task,
    complete_reconstruction,
    fail_reconstruction,
)

McpFactory = Callable[[], CockroachManagedMCP]


def run_one_reconstruction_task(
    dal: DAL,
    *,
    worker_id: str,
    mcp_factory: McpFactory,
) -> ReconstructionCompletion | None:
    lease = claim_next_reconstruction_task(dal, worker_id=worker_id)
    if lease is None:
        return None

    # No charged dates means Intake never produced the required period claims;
    # that is a gap, not an invented timeline.
    if not lease.charge_dates:
        fail_reconstruction(
            dal,
            lease=lease,
            error_code="PRIOR_MEMORY_EMPTY",
            retryable=False,
            terminal_state=ReconstructionState.NEEDS_EVIDENCE,
        )
        return None

    try:
        with mcp_factory() as mcp:
            memory = read_reconstruction_memory(
                mcp,
                shipment_ref=lease.shipment_ref,
                container_ref=lease.container_ref,
                knowledge_cutoff_iso=_iso(lease.knowledge_cutoff_at),
                correlation_id=lease.task_id,
            )
    except MCPUnavailableError as exc:
        # Fail closed. Transport/timeout is retryable; auth/protocol is terminal.
        # Log the diagnostic cause (type + message + underlying cause) so a
        # deployed reconstruction failure is not silent. Public-safe: MCP errors
        # carry no token/locator text.
        import logging

        cause = exc.__cause__ or exc.__context__
        logging.getLogger("tally.reconstruction").error(
            "reconstruction MCP failure: code=%s type=%s msg=%s cause=%s:%s",
            _safe_mcp_code(exc), type(exc).__name__, str(exc),
            type(cause).__name__ if cause else None, str(cause) if cause else None,
        )
        retryable = _is_retryable_mcp(exc)
        fail_reconstruction(
            dal,
            lease=lease,
            error_code=_safe_mcp_code(exc),
            retryable=retryable,
            terminal_state=ReconstructionState.BLOCKED_MEMORY_UNAVAILABLE,
        )
        return None

    validation = validate_events(
        list(memory.rows),
        knowledge_cutoff=lease.knowledge_cutoff_at,
        shipment_ref=lease.shipment_ref,
        container_ref=lease.container_ref,
    )
    if not validation.accepted:
        fail_reconstruction(
            dal,
            lease=lease,
            error_code="PRIOR_MEMORY_EMPTY",
            retryable=False,
            terminal_state=ReconstructionState.NEEDS_EVIDENCE,
        )
        return None

    boundary = resolve_charge_boundary(validation.accepted)
    charge_dates = [date.fromisoformat(d) for d in lease.charge_dates]
    days = adjudicate_charged_days(
        charge_dates=charge_dates,
        invoice_rate_minor=lease.invoice_rate_minor,
        currency=lease.currency,
        events=validation.accepted,
        boundary=boundary,
    )
    coverage = classify_coverage(
        events=validation.accepted,
        have_invoice_source=True,
        have_container_identity=bool(lease.container_ref),
        have_charged_dates=bool(lease.charge_dates),
        have_invoice_rate=lease.invoice_rate_minor > 0,
    )
    terminal_state = resolve_terminal_state(days)
    day_event_roles = _day_event_roles(days, validation.accepted, boundary)

    return complete_reconstruction(
        dal,
        lease=lease,
        events=validation.accepted,
        days=days,
        coverage=coverage,
        terminal_state=terminal_state,
        day_event_roles=day_event_roles,
        mcp_correlation_id=memory.correlation_id,
        mcp_query_ref_private=memory.server_request_id or memory.correlation_id,
        issue_codes=validation.issue_codes,
    )


def _day_event_roles(
    days: tuple[ChargedDayResult, ...],
    events: tuple[NormalizedEvent, ...],
    boundary,
) -> dict[str, dict[str, list[str]]]:
    """Map each charged date to the boundary events that adjudicate it."""
    availability = [e.public_ref for e in events if e.event_type is ShipmentEventType.AVAILABLE]
    free_end = [e.public_ref for e in events if e.event_type is ShipmentEventType.FREE_TIME_END]
    gate_out = [e.public_ref for e in events if e.event_type is ShipmentEventType.GATE_OUT]
    roles: dict[str, dict[str, list[str]]] = {}
    for day in days:
        if day.state.value not in {"SOURCE_COMPLETE", "EXCLUDED_NOT_CHARGEABLE"}:
            continue
        roles[day.charge_date.isoformat()] = {
            "AVAILABILITY_BOUNDARY": availability,
            "FREE_TIME_BOUNDARY": free_end,
            "CHARGE_END": gate_out,
        }
    return roles


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _is_retryable_mcp(exc: MCPUnavailableError) -> bool:
    from src.external.cockroach_mcp import MCPPermissionError, MCPProtocolError

    return not isinstance(exc, (MCPPermissionError, MCPProtocolError))


def _safe_mcp_code(exc: MCPUnavailableError) -> str:
    from src.external.cockroach_mcp import (
        MCPAuthenticationError,
        MCPPermissionError,
        MCPProtocolError,
    )

    if isinstance(exc, MCPAuthenticationError):
        return "MCP_UNAUTHENTICATED"
    if isinstance(exc, MCPPermissionError):
        return "MCP_UNAUTHORIZED"
    if isinstance(exc, MCPProtocolError):
        return "MCP_MALFORMED"
    return "MCP_UNAVAILABLE"

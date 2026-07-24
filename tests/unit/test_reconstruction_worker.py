"""Worker-orchestration tests: lease -> MCP -> validate -> commit/fail closed.

Proves the no-fallback contract at the worker seam: a failed, empty, or
malformed Managed MCP read routes to fail_reconstruction and NEVER to
complete_reconstruction; a real read produces the seven-day completion. The MCP
client and repository functions are monkeypatched so these run zero-network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.core.reconstruction import RawEventRow, ReconstructionState
from src.external.cockroach_mcp import (
    MCPAuthenticationError,
    MCPProtocolError,
    MCPUnavailableError,
)
from src.external.reconstruction_mcp import ReconstructionMemoryResult
from src.platform import reconstruction_worker as worker
from src.platform.reconstruction_repository import (
    ReconstructionCompletion,
    ReconstructionTaskLease,
)

CUTOFF = datetime.fromisoformat("2026-06-22T08:00:00+00:00")
SHIP = "TLLU4829317"


def _lease(charge_dates=None):
    return ReconstructionTaskLease(
        task_id="task-1",
        invoice_id="invoice-1",
        attempt=1,
        worker_id="worker-1",
        lease_expires_at=datetime(2026, 6, 22, 8, 2, tzinfo=UTC),
        knowledge_cutoff_at=CUTOFF,
        input_fingerprint="fp-1",
        claim_set_version=1,
        source_id="source-1",
        shipment_ref=SHIP,
        container_ref=SHIP,
        invoice_rate_minor=35000,
        currency="USD",
        charge_dates=tuple(
            charge_dates
            if charge_dates is not None
            else [f"2026-06-{d:02d}" for d in range(8, 15)]
        ),
        initiated_by=None,
        actor_display="Rachel Martinez",
    )


def _raw(ref, event_type, occurred):
    return RawEventRow(
        public_ref=ref,
        event_type=event_type,
        shipment_ref=SHIP,
        container_ref=SHIP,
        source_public_ref=f"SRC-{ref}",
        source_verification_state="VERIFIED",
        display_anchor=f"row {ref}",
        provenance_classification="DEMO_SCENARIO",
        occurred_at=occurred,
        recorded_at="2026-06-20T00:00:00+00:00",
    )


def _hero_memory():
    rows = (
        _raw("SE-001", "DISCHARGED", "2026-06-02T15:00:00+00:00"),
        _raw("SE-002", "AVAILABLE", "2026-06-03T09:00:00+00:00"),
        _raw("SE-003", "FREE_TIME_START", "2026-06-03T09:00:00+00:00"),
        _raw("SE-004", "FREE_TIME_END", "2026-06-07T23:59:00+00:00"),
        _raw("SE-005", "GATE_OUT", "2026-06-14T17:00:00+00:00"),
    )
    return ReconstructionMemoryResult(
        rows=rows,
        correlation_id="task-1",
        elapsed_ms=10,
        returned_row_count=len(rows),
        server_request_id="srv-1",
    )


class _MCPCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, *, lease, memory=None, raise_exc=None):
    monkeypatch.setattr(worker, "claim_next_reconstruction_task", lambda *a, **k: lease)
    monkeypatch.setattr(worker, "_iso", lambda v: "2026-06-22T08:00:00Z")

    def _read(mcp, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return memory

    monkeypatch.setattr(worker, "read_reconstruction_memory", _read)
    completed = {}
    failed = {}
    monkeypatch.setattr(
        worker,
        "complete_reconstruction",
        lambda dal, **k: completed.update(k)
        or ReconstructionCompletion("recon-1", 1, k["terminal_state"].value,
                                    len(k["events"]), len(k["days"]),
                                    sum(1 for d in k["days"]
                                        if d.state.value == "SOURCE_COMPLETE")),
    )
    monkeypatch.setattr(
        worker,
        "fail_reconstruction",
        lambda dal, **k: failed.update(k) or k.get("terminal_state").value,
    )
    return completed, failed


def test_no_charge_dates_fails_closed(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease(charge_dates=[]))
    result = worker.run_one_reconstruction_task(
        object(), worker_id="worker-1", mcp_factory=_MCPCtx
    )
    assert result is None
    assert not completed
    assert failed["error_code"] == "PRIOR_MEMORY_EMPTY"
    assert failed["retryable"] is False


def test_hero_read_completes_seven_days(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease(), memory=_hero_memory())
    result = worker.run_one_reconstruction_task(
        object(), worker_id="worker-1", mcp_factory=_MCPCtx
    )
    assert not failed
    assert result.state == ReconstructionState.COMPLETE.value
    assert len(completed["events"]) == 5
    assert len(completed["days"]) == 7
    assert completed["terminal_state"] is ReconstructionState.COMPLETE


def test_mcp_transport_failure_retryable_no_complete(monkeypatch):
    completed, failed = _patch(
        monkeypatch, lease=_lease(), raise_exc=MCPUnavailableError("down")
    )
    result = worker.run_one_reconstruction_task(
        object(), worker_id="worker-1", mcp_factory=_MCPCtx
    )
    assert result is None
    assert not completed  # NO fallback success
    assert failed["error_code"] == "MCP_UNAVAILABLE"
    assert failed["retryable"] is True
    assert failed["terminal_state"] is ReconstructionState.BLOCKED_MEMORY_UNAVAILABLE


def test_mcp_auth_failure_is_terminal(monkeypatch):
    completed, failed = _patch(
        monkeypatch, lease=_lease(), raise_exc=MCPAuthenticationError("401")
    )
    worker.run_one_reconstruction_task(object(), worker_id="w", mcp_factory=_MCPCtx)
    assert not completed
    assert failed["error_code"] == "MCP_UNAUTHENTICATED"
    assert failed["retryable"] is False


def test_mcp_malformed_is_terminal(monkeypatch):
    completed, failed = _patch(
        monkeypatch, lease=_lease(), raise_exc=MCPProtocolError("bad")
    )
    worker.run_one_reconstruction_task(object(), worker_id="w", mcp_factory=_MCPCtx)
    assert not completed
    assert failed["error_code"] == "MCP_MALFORMED"
    assert failed["retryable"] is False


def test_empty_verified_memory_needs_evidence(monkeypatch):
    empty = ReconstructionMemoryResult(
        rows=(), correlation_id="task-1", elapsed_ms=5,
        returned_row_count=0, server_request_id=None,
    )
    completed, failed = _patch(monkeypatch, lease=_lease(), memory=empty)
    worker.run_one_reconstruction_task(object(), worker_id="w", mcp_factory=_MCPCtx)
    assert not completed
    assert failed["error_code"] == "PRIOR_MEMORY_EMPTY"
    assert failed["terminal_state"] is ReconstructionState.NEEDS_EVIDENCE

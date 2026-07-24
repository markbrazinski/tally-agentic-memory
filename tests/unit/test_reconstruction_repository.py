"""Repository transaction tests against the in-memory reconstruction fake DB.

Proves the durable contract: one atomic reconstruction version + events + days +
coverage + public event + outbox per transaction; late-worker fencing (a lost
lease raises before any write); idempotent replay (a duplicated delivery returns
the existing version, writes nothing new); fail-closed with no version written.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.reconstruction import (
    ChargedDayResult,
    ChargedDayState,
    CoverageState,
    NormalizedEvent,
    ReconstructionState,
    ShipmentEventType,
)
from src.platform.intake_tasks import TaskLeaseLostError
from src.platform.reconstruction_repository import (
    ReconstructionTaskLease,
    complete_reconstruction,
    fail_reconstruction,
)
from tests.unit._recon_fakedb import FakeConn, make_dal, seed_running_task

CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _lease(worker="worker-1", attempt=1):
    return ReconstructionTaskLease(
        task_id="task-1",
        invoice_id="invoice-1",
        attempt=attempt,
        worker_id=worker,
        lease_expires_at=CUTOFF,
        knowledge_cutoff_at=CUTOFF,
        input_fingerprint="fp-1",
        claim_set_version=1,
        source_id="source-1",
        shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
        invoice_rate_minor=35000,
        currency="USD",
        charge_dates=tuple(f"2026-06-{d:02d}" for d in range(8, 15)),
        initiated_by=None,
        actor_display="Rachel Martinez",
    )


def _event(ref, event_type, occurred):
    return NormalizedEvent(
        public_ref=ref,
        event_type=ShipmentEventType(event_type),
        shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
        source_public_ref=f"SRC-{ref}",
        display_anchor=f"row {ref}",
        provenance_classification="DEMO_SCENARIO",
        occurred_at=datetime.fromisoformat(occurred),
        recorded_at=datetime.fromisoformat("2026-06-20T00:00:00+00:00"),
        observed_at=None,
        effective_from=None,
        effective_to=None,
        normalized_facts={},
    )


def _hero_events():
    return (
        _event("SE-001", "DISCHARGED", "2026-06-02T15:00:00+00:00"),
        _event("SE-002", "AVAILABLE", "2026-06-03T09:00:00+00:00"),
        _event("SE-004", "FREE_TIME_END", "2026-06-07T23:59:00+00:00"),
        _event("SE-005", "GATE_OUT", "2026-06-14T17:00:00+00:00"),
    )


def _seven_complete_days():
    from datetime import date

    return tuple(
        ChargedDayResult(
            charge_date=date(2026, 6, d),
            state=ChargedDayState.SOURCE_COMPLETE,
            chargeability="CHARGEABLE",
            coverage_state=CoverageState.PRESENT_VERIFIED,
            invoice_rate_minor=35000,
            currency="USD",
            event_refs=("SE-002", "SE-004", "SE-005"),
            missing_requirements=(),
        )
        for d in range(8, 15)
    )


def _coverage():
    return {
        "AVAILABILITY": CoverageState.PRESENT_VERIFIED,
        "GATE_OUT": CoverageState.PRESENT_VERIFIED,
    }


def test_complete_writes_one_atomic_version():
    conn = FakeConn()
    seed_running_task(conn)
    dal = make_dal(conn)
    result = complete_reconstruction(
        dal,
        lease=_lease(),
        events=_hero_events(),
        days=_seven_complete_days(),
        coverage=_coverage(),
        terminal_state=ReconstructionState.COMPLETE,
        day_event_roles={},
        mcp_correlation_id="corr-1",
        mcp_query_ref_private="srv-1",
        issue_codes=(),
    )
    assert result.state == "COMPLETE"
    assert result.version == 1
    assert result.event_count == 4
    assert result.days_total == 7
    assert result.days_complete == 7
    assert len(conn.reconstructions) == 1
    assert conn.counts["reconstruction_events"] == 4
    assert conn.counts["reconstruction_charged_days"] == 7
    # exactly one public event + its outbox row committed in the same txn
    assert conn.counts["invoice_events"] == 1
    assert conn.counts["event_outbox"] == 1
    assert conn.events[-1]["type"] == "reconstruction.completed"


def test_late_worker_is_fenced_before_any_write():
    conn = FakeConn()
    seed_running_task(conn, worker="worker-1", attempt=1)
    dal = make_dal(conn)
    stale = _lease(worker="worker-2", attempt=1)  # different owner -> lost lease
    with pytest.raises(TaskLeaseLostError):
        complete_reconstruction(
            dal,
            lease=stale,
            events=_hero_events(),
            days=_seven_complete_days(),
            coverage=_coverage(),
            terminal_state=ReconstructionState.COMPLETE,
            day_event_roles={},
            mcp_correlation_id="corr-1",
            mcp_query_ref_private="srv-1",
            issue_codes=(),
        )
    assert len(conn.reconstructions) == 0
    assert "reconstruction_events" not in conn.counts


def test_duplicate_delivery_replays_existing_version():
    conn = FakeConn()
    seed_running_task(conn)
    dal = make_dal(conn)
    first = complete_reconstruction(
        dal, lease=_lease(), events=_hero_events(), days=_seven_complete_days(),
        coverage=_coverage(), terminal_state=ReconstructionState.COMPLETE,
        day_event_roles={}, mcp_correlation_id="c", mcp_query_ref_private="s",
        issue_codes=(),
    )
    events_after_first = conn.counts["reconstruction_events"]
    # Re-run the same fingerprint (duplicated task delivery).
    seed_running_task(conn)  # reset the task to RUNNING for the same worker
    second = complete_reconstruction(
        dal, lease=_lease(), events=_hero_events(), days=_seven_complete_days(),
        coverage=_coverage(), terminal_state=ReconstructionState.COMPLETE,
        day_event_roles={}, mcp_correlation_id="c", mcp_query_ref_private="s",
        issue_codes=(),
    )
    assert second.reconstruction_id == first.reconstruction_id
    assert len(conn.reconstructions) == 1
    # no second set of event rows written
    assert conn.counts["reconstruction_events"] == events_after_first


def test_fail_writes_no_version_and_blocks():
    conn = FakeConn()
    seed_running_task(conn)
    dal = make_dal(conn)
    state = fail_reconstruction(
        dal,
        lease=_lease(),
        error_code="MCP_UNAVAILABLE",
        retryable=False,
        terminal_state=ReconstructionState.BLOCKED_MEMORY_UNAVAILABLE,
    )
    assert state == "BLOCKED"
    assert len(conn.reconstructions) == 0
    assert conn.events[-1]["type"] == "reconstruction.memory_retrieval_failed"
    assert conn.counts["invoice_events"] == 1
    assert conn.counts["event_outbox"] == 1


def test_retryable_failure_schedules_retry():
    conn = FakeConn()
    seed_running_task(conn, attempt=1)
    dal = make_dal(conn)
    state = fail_reconstruction(
        dal,
        lease=_lease(attempt=1),
        error_code="MCP_UNAVAILABLE",
        retryable=True,
        terminal_state=ReconstructionState.BLOCKED_MEMORY_UNAVAILABLE,
    )
    assert state == "RETRY_WAIT"
    assert len(conn.reconstructions) == 0

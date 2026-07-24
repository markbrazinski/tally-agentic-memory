"""Durable transactions for Gate 2 sourced reconstruction.

Leases the START_RECONSTRUCTION task created by Intake, and — in one atomic
transaction — persists an immutable reconstruction version with every accepted
event fully source/time/version/provenance bound, the charged-day ledger, the
categorical coverage set, the invoice status advance, and the public
event+outbox rows. Reuses the intake spine's leasing/fencing/event helpers; adds
no second orchestration mechanism.

No fixture, direct-SQL, or model fallback path exists here: a reconstruction row
is written only from validated MCP-derived events, and a failed/empty MCP read
routes through ``fail_reconstruction`` (BLOCKED/NEEDS_EVIDENCE), never a
substitute success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from src.core.reconstruction import (
    ChargedDayResult,
    CoverageState,
    NormalizedEvent,
    ReconstructionState,
)
from src.external.dal import DAL
from src.platform.intake_tasks import (
    MAX_AUTOMATIC_ATTEMPTS,
    TaskLeaseLostError,
    _lock_invoice_and_advance,
)

EFFECTIVE_TIMEZONE = "America/Los_Angeles"


@dataclass(frozen=True)
class ReconstructionTaskLease:
    task_id: str
    invoice_id: str
    attempt: int
    worker_id: str
    lease_expires_at: datetime
    knowledge_cutoff_at: datetime
    input_fingerprint: str
    claim_set_version: int
    source_id: str
    shipment_ref: str
    container_ref: str
    invoice_rate_minor: int
    currency: str
    charge_dates: tuple[str, ...]
    initiated_by: str | None
    actor_display: str


@dataclass(frozen=True)
class ReconstructionCompletion:
    reconstruction_id: str
    version: int
    state: str
    event_count: int
    days_total: int
    days_complete: int


def claim_next_reconstruction_task(
    dal: DAL,
    *,
    worker_id: str,
    lease_seconds: int = 90,
) -> ReconstructionTaskLease | None:
    """Lease one runnable START_RECONSTRUCTION task, gathering its inputs.

    Same SELECT ... FOR UPDATE OF w SKIP LOCKED leasing/fencing pattern as
    extraction, but joins the active claim set to read the normalized shipment
    identifiers, invoice rate, and charged dates the reconstruction needs. If the
    active claim set is missing the required charged-day claims the lease still
    forms; the worker records the resulting gap rather than inventing dates.
    """
    tenant_id = dal.tenant.tenant_id

    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.invoice_id, w.current_attempt,
                       w.knowledge_cutoff_at, w.initiated_by, w.actor_display,
                       w.input_fingerprint, w.input_object_refs
                FROM workflow_tasks w
                WHERE w.tenant_id=%s
                  AND w.task_type='START_RECONSTRUCTION'
                  AND (
                    w.state='PENDING'
                    OR (w.state='RETRY_WAIT'
                        AND (w.not_before IS NULL OR w.not_before <= now()))
                    OR (w.state IN ('LEASED','RUNNING')
                        AND w.lease_expires_at < now())
                  )
                ORDER BY w.created_at, w.id
                LIMIT 1
                FOR UPDATE OF w SKIP LOCKED;
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task_id = str(row[0])
            invoice_id = str(row[1])
            attempt = int(row[2]) + 1
            refs = row[7] if isinstance(row[7], list) else json.loads(row[7])
            source_ref = next(
                (r for r in refs if r.get("type") == "invoice_source"), {}
            )
            claim_ref = next((r for r in refs if r.get("type") == "claim_set"), {})
            claim_set_version = int(claim_ref.get("version", 0))

            inputs = _load_claim_inputs(cur, tenant_id, invoice_id, claim_set_version)

            cur.execute("SELECT now();")
            started_at = cur.fetchone()[0]
            lease_expires_at = started_at + timedelta(seconds=lease_seconds)
            cur.execute(
                """
                UPDATE workflow_tasks
                SET state='RUNNING', current_attempt=%s, lease_owner=%s,
                    lease_expires_at=%s, started_at=COALESCE(started_at, %s),
                    updated_at=%s
                WHERE tenant_id=%s AND id=%s;
                """,
                (attempt, worker_id, lease_expires_at, started_at, started_at,
                 tenant_id, task_id),
            )
            cur.execute(
                """
                INSERT INTO workflow_task_attempts
                    (tenant_id, task_id, attempt, state, lease_owner,
                     lease_expires_at, started_at)
                VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s);
                """,
                (tenant_id, task_id, attempt, worker_id, lease_expires_at, started_at),
            )
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status="RECONSTRUCTING",
                status="RECONSTRUCTING",
                increment=1,
                occurred_at=started_at,
            )
            _insert_recon_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                sequence=sequence,
                event_type="reconstruction.memory_retrieval_started",
                occurred_at=started_at,
                state="RUNNING",
                aggregate_status="RECONSTRUCTING",
                summary="Retrieving pre-invoice events through CockroachDB Managed MCP",
                initiated_by=str(row[4]) if row[4] else None,
                actor_display=row[5],
                input_refs=refs,
            )
            return ReconstructionTaskLease(
                task_id=task_id,
                invoice_id=invoice_id,
                attempt=attempt,
                worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                knowledge_cutoff_at=row[3],
                input_fingerprint=row[6],
                claim_set_version=claim_set_version,
                source_id=str(source_ref.get("id")),
                shipment_ref=inputs["container_ref"],
                container_ref=inputs["container_ref"],
                invoice_rate_minor=inputs["daily_rate_minor"],
                currency=inputs["currency"],
                charge_dates=inputs["charge_dates"],
                initiated_by=str(row[4]) if row[4] else None,
                actor_display=row[5],
            )

    return dal.run_with_retry(_claim)


def _load_claim_inputs(
    cur, tenant_id: str, invoice_id: str, claim_set_version: int
) -> dict[str, Any]:
    """Read the normalized identifiers/rate/dates from the active claim set."""
    cur.execute(
        """
        SELECT c.field_name, c.normalized_value, c.amount_minor, c.currency
        FROM extracted_claims c
        JOIN claim_sets s ON s.tenant_id=c.tenant_id AND s.id=c.claim_set_id
        WHERE c.tenant_id=%s AND s.invoice_id=%s AND s.claim_set_version=%s;
        """,
        (tenant_id, invoice_id, claim_set_version),
    )
    by_field: dict[str, tuple[Any, Any, Any]] = {}
    for field_name, normalized, amount_minor, currency in cur.fetchall():
        value = normalized if not isinstance(normalized, str) else json.loads(normalized)
        by_field[field_name] = (value, amount_minor, currency)

    container = by_field.get("container_number", (None, None, None))[0] or ""
    daily = by_field.get("daily_rate", (None, None, None))
    currency = daily[2] or "USD"
    daily_rate_minor = int(daily[1] or 0)
    charge_dates = _expand_charge_dates(by_field)
    return {
        "container_ref": str(container),
        "daily_rate_minor": daily_rate_minor,
        "currency": currency,
        "charge_dates": charge_dates,
    }


def _expand_charge_dates(by_field: dict[str, tuple[Any, Any, Any]]) -> tuple[str, ...]:
    from datetime import date

    start_raw = by_field.get("period_start", (None, None, None))[0]
    end_raw = by_field.get("period_end", (None, None, None))[0]
    if not start_raw or not end_raw:
        return ()
    start = date.fromisoformat(str(start_raw))
    end = date.fromisoformat(str(end_raw))
    if end < start:
        return ()
    days = (end - start).days + 1
    return tuple((start + timedelta(days=i)).isoformat() for i in range(days))


def complete_reconstruction(
    dal: DAL,
    *,
    lease: ReconstructionTaskLease,
    events: tuple[NormalizedEvent, ...],
    days: tuple[ChargedDayResult, ...],
    coverage: dict[str, CoverageState],
    terminal_state: ReconstructionState,
    day_event_roles: dict[str, dict[str, list[str]]],
    mcp_correlation_id: str,
    mcp_query_ref_private: str,
    issue_codes: tuple[str, ...],
) -> ReconstructionCompletion:
    """Atomically persist the immutable reconstruction version and advance state.

    All writes (reconstruction, events, charged days, day/event bindings,
    coverage, task completion, invoice status, public event, outbox) commit in
    one transaction. Re-entry is idempotent via the reconstruction
    fingerprint unique index — a duplicated delivery does not create a second
    version.
    """
    tenant_id = dal.tenant.tenant_id
    days_complete = sum(1 for d in days if d.state.value == "SOURCE_COMPLETE")

    def _complete(conn):
        with conn.cursor() as cur:
            _assert_recon_lease(cur, tenant_id, lease)
            # Idempotency: if a reconstruction version already exists for this
            # fingerprint, replay it instead of writing a second one.
            cur.execute(
                """
                SELECT id, version, state, event_count, days_total, days_complete
                FROM reconstructions
                WHERE tenant_id=%s AND invoice_id=%s AND input_fingerprint=%s;
                """,
                (tenant_id, lease.invoice_id, lease.input_fingerprint),
            )
            existing = cur.fetchone()
            if existing is not None:
                _finish_task(cur, tenant_id, lease, terminal_state.value)
                return ReconstructionCompletion(
                    reconstruction_id=str(existing[0]),
                    version=int(existing[1]),
                    state=str(existing[2]),
                    event_count=int(existing[3]),
                    days_total=int(existing[4]),
                    days_complete=int(existing[5]),
                )

            cur.execute("SELECT now();")
            completed_at = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COALESCE(max(version), 0) + 1 FROM reconstructions
                WHERE tenant_id=%s AND invoice_id=%s;
                """,
                (tenant_id, lease.invoice_id),
            )
            version = int(cur.fetchone()[0])
            reconstruction_id = str(uuid4())
            summary = _summary(terminal_state, days_complete, len(days))
            cur.execute(
                """
                INSERT INTO reconstructions
                    (tenant_id, id, invoice_id, version, task_id,
                     input_fingerprint, claim_set_version, knowledge_cutoff_at,
                     effective_timezone, state, event_count, days_total,
                     days_complete, mcp_correlation_id, mcp_query_ref_private,
                     public_summary, issue_codes, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s);
                """,
                (tenant_id, reconstruction_id, lease.invoice_id, version,
                 lease.task_id, lease.input_fingerprint, lease.claim_set_version,
                 lease.knowledge_cutoff_at, EFFECTIVE_TIMEZONE,
                 terminal_state.value, len(events), len(days), days_complete,
                 mcp_correlation_id, mcp_query_ref_private, summary,
                 json.dumps(list(issue_codes)), completed_at),
            )

            event_id_by_ref: dict[str, str] = {}
            for index, event in enumerate(events):
                event_row_id = str(uuid4())
                event_id_by_ref[event.public_ref] = event_row_id
                recorded_before = event.recorded_at <= lease.knowledge_cutoff_at
                cur.execute(
                    """
                    INSERT INTO reconstruction_events
                        (tenant_id, id, reconstruction_id, invoice_id, public_ref,
                         event_type, shipment_ref, container_ref,
                         source_artifact_id, source_version_ref_private,
                         source_anchor_private, display_anchor_public,
                         provenance_classification, occurred_at, effective_from,
                         effective_to, observed_at, recorded_at, received_at,
                         recorded_before_cutoff, normalized_facts, use_state,
                         verification_state, display_sequence)
                    SELECT %s, %s, %s, %s, %s, %s, %s, %s, a.id,
                           a.s3_version_id_private, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, 'USED', 'VERIFIED', %s
                    FROM reconstruction_source_artifacts a
                    WHERE a.tenant_id=%s AND a.public_ref=%s;
                    """,
                    (
                        tenant_id, event_row_id, reconstruction_id,
                        lease.invoice_id, event.public_ref, event.event_type.value,
                        event.shipment_ref, event.container_ref,
                        json.dumps({"anchor": event.display_anchor}),
                        event.display_anchor, event.provenance_classification,
                        event.occurred_at, event.effective_from, event.effective_to,
                        event.observed_at, event.recorded_at,
                        lease.knowledge_cutoff_at, recorded_before,
                        json.dumps(event.normalized_facts), index,
                        tenant_id, event.source_public_ref,
                    ),
                )

            for day in days:
                day_id = str(uuid4())
                cur.execute(
                    """
                    INSERT INTO reconstruction_charged_days
                        (tenant_id, id, reconstruction_id, invoice_id,
                         charge_date, invoice_claim_field, chargeability,
                         coverage_state, state, invoice_rate_minor,
                         applicable_rate_minor, currency, outcome,
                         dispute_amount_minor, missing_requirements)
                    VALUES (%s, %s, %s, %s, %s, 'daily_rate', %s, %s, %s, %s,
                            NULL, %s, 'PENDING', NULL, %s);
                    """,
                    (tenant_id, day_id, reconstruction_id, lease.invoice_id,
                     day.charge_date, day.chargeability, day.coverage_state.value,
                     day.state.value, day.invoice_rate_minor, day.currency,
                     json.dumps(list(day.missing_requirements))),
                )
                roles = day_event_roles.get(day.charge_date.isoformat(), {})
                for role, refs in roles.items():
                    for ref in refs:
                        event_row_id = event_id_by_ref.get(ref)
                        if event_row_id is None:
                            continue
                        cur.execute(
                            """
                            INSERT INTO reconstruction_day_event_bindings
                                (tenant_id, charged_day_id,
                                 reconstruction_event_id, role)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT DO NOTHING;
                            """,
                            (tenant_id, day_id, event_row_id, role),
                        )

            for requirement, state in coverage.items():
                cur.execute(
                    """
                    INSERT INTO reconstruction_coverage
                        (tenant_id, reconstruction_id, invoice_id,
                         requirement_code, coverage_state)
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    (tenant_id, reconstruction_id, lease.invoice_id,
                     requirement, state.value),
                )

            _finish_task(cur, tenant_id, lease, terminal_state.value)

            aggregate = (
                "RECONSTRUCTING"
                if terminal_state is ReconstructionState.COMPLETE
                else "NEEDS_EVIDENCE"
            )
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status=aggregate,
                status=aggregate,
                increment=1,
                occurred_at=completed_at,
            )
            event_type = (
                "reconstruction.completed"
                if terminal_state is ReconstructionState.COMPLETE
                else "reconstruction.needs_evidence"
            )
            _insert_recon_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                sequence=sequence,
                event_type=event_type,
                occurred_at=completed_at,
                state="COMPLETED" if terminal_state is ReconstructionState.COMPLETE
                else "BLOCKED",
                aggregate_status=aggregate,
                summary=summary,
                initiated_by=lease.initiated_by,
                actor_display=lease.actor_display,
                input_refs=[{"type": "workflow_task", "id": lease.task_id, "version": 1}],
                produced_refs=[
                    {"type": "reconstruction", "id": reconstruction_id, "version": version}
                ],
                output_count=len(events),
            )
            return ReconstructionCompletion(
                reconstruction_id=reconstruction_id,
                version=version,
                state=terminal_state.value,
                event_count=len(events),
                days_total=len(days),
                days_complete=days_complete,
            )

    return dal.run_with_retry(_complete)


def fail_reconstruction(
    dal: DAL,
    *,
    lease: ReconstructionTaskLease,
    error_code: str,
    retryable: bool,
    terminal_state: ReconstructionState,
) -> str:
    """Fail closed: no reconstruction version, invoice blocked/needs-evidence.

    A retryable MCP outage schedules a bounded backoff retry; a terminal failure
    (unauthorized, malformed, missing source, empty memory) blocks. No fallback
    reconstruction is ever written.
    """
    tenant_id = dal.tenant.tenant_id

    def _fail(conn):
        with conn.cursor() as cur:
            _assert_recon_lease(cur, tenant_id, lease)
            cur.execute("SELECT now();")
            failed_at = cur.fetchone()[0]
            will_retry = retryable and lease.attempt < MAX_AUTOMATIC_ATTEMPTS
            task_state = "RETRY_WAIT" if will_retry else "BLOCKED"
            not_before = (
                failed_at + timedelta(seconds=2 ** lease.attempt) if will_retry else None
            )
            cur.execute(
                """
                UPDATE workflow_tasks
                SET state=%s, not_before=%s, lease_owner=NULL,
                    lease_expires_at=NULL, private_error_code=%s,
                    public_summary=%s, updated_at=%s
                WHERE tenant_id=%s AND id=%s AND current_attempt=%s
                  AND lease_owner=%s;
                """,
                (task_state, not_before, error_code,
                 "Reconstruction will retry" if will_retry
                 else "Reconstruction is blocked", failed_at,
                 tenant_id, lease.task_id, lease.attempt, lease.worker_id),
            )
            cur.execute(
                """
                UPDATE workflow_task_attempts
                SET state=%s, completed_at=%s, private_error_code=%s
                WHERE tenant_id=%s AND task_id=%s AND attempt=%s
                  AND lease_owner=%s;
                """,
                (task_state, failed_at, error_code, tenant_id, lease.task_id,
                 lease.attempt, lease.worker_id),
            )
            aggregate = "BLOCKED" if not will_retry else "RECONSTRUCTING"
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status=aggregate,
                status=aggregate,
                increment=1,
                occurred_at=failed_at,
            )
            _insert_recon_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                sequence=sequence,
                event_type=(
                    "reconstruction.memory_retrieval_retry_scheduled"
                    if will_retry
                    else "reconstruction.memory_retrieval_failed"
                ),
                occurred_at=failed_at,
                state=task_state,
                aggregate_status=aggregate,
                summary=(
                    "Prior memory retrieval will retry" if will_retry
                    else "Prior memory unavailable. No substitute data is shown."
                ),
                initiated_by=lease.initiated_by,
                actor_display=lease.actor_display,
                input_refs=[{"type": "workflow_task", "id": lease.task_id, "version": 1}],
                public_error={"code": _public_error_code(error_code)},
            )
            return task_state

    return dal.run_with_retry(_fail)


def _public_error_code(error_code: str) -> str:
    if error_code.startswith("SOURCE_"):
        return "SOURCE_VERSION_UNAVAILABLE"
    if error_code in {"MCP_EMPTY", "PRIOR_MEMORY_EMPTY"}:
        return "PRIOR_MEMORY_EMPTY"
    return "PRIOR_MEMORY_UNAVAILABLE"


def _finish_task(cur, tenant_id: str, lease: ReconstructionTaskLease, state: str) -> None:
    summary = (
        "Pre-invoice events reconstructed"
        if state == "COMPLETE"
        else "Reconstruction needs evidence"
    )
    cur.execute(
        """
        UPDATE workflow_tasks
        SET state='COMPLETED', completed_at=now(), lease_owner=NULL,
            lease_expires_at=NULL, public_summary=%s, updated_at=now()
        WHERE tenant_id=%s AND id=%s AND current_attempt=%s AND lease_owner=%s;
        """,
        (summary, tenant_id, lease.task_id, lease.attempt, lease.worker_id),
    )
    cur.execute(
        """
        UPDATE workflow_task_attempts
        SET state='COMPLETED', completed_at=now()
        WHERE tenant_id=%s AND task_id=%s AND attempt=%s AND lease_owner=%s;
        """,
        (tenant_id, lease.task_id, lease.attempt, lease.worker_id),
    )


def _assert_recon_lease(cur, tenant_id: str, lease: ReconstructionTaskLease) -> None:
    cur.execute(
        """
        SELECT state, current_attempt, lease_owner FROM workflow_tasks
        WHERE tenant_id=%s AND id=%s FOR UPDATE;
        """,
        (tenant_id, lease.task_id),
    )
    row = cur.fetchone()
    if (
        row is None
        or row[0] != "RUNNING"
        or int(row[1]) != lease.attempt
        or row[2] != lease.worker_id
    ):
        raise TaskLeaseLostError("TASK_LEASE_LOST")


def _summary(state: ReconstructionState, days_complete: int, days_total: int) -> str:
    if state is ReconstructionState.COMPLETE:
        return f"{days_total} charged days reconstructed with complete source coverage"
    return f"{days_complete} of {days_total} charged days sourced; evidence required"


def _insert_recon_event(
    cur,
    *,
    tenant_id: str,
    invoice_id: str,
    sequence: int,
    event_type: str,
    occurred_at: datetime,
    state: str,
    aggregate_status: str,
    summary: str,
    initiated_by: str | None,
    actor_display: str,
    input_refs: list[dict[str, Any]],
    produced_refs: list[dict[str, Any]] | None = None,
    output_count: int | None = None,
    public_error: dict[str, Any] | None = None,
) -> None:
    """Insert a RECONSTRUCTION_AGENT event + outbox row (atomic with the txn)."""
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_events
            (tenant_id, id, invoice_id, sequence, event_type, schema_version,
             occurred_at, role, task, tool_display_name, state,
             aggregate_status, summary, initiated_by, actor_display,
             input_object_refs, produced_object_refs, output_count, public_error)
        VALUES (%s, %s, %s, %s, %s, 1, %s, 'RECONSTRUCTION_AGENT',
                'ASSEMBLE_RECONSTRUCTION', 'CockroachDB Managed MCP', %s, %s, %s,
                %s, %s, %s, %s, %s, %s);
        """,
        (tenant_id, event_id, invoice_id, sequence, event_type, occurred_at,
         state, aggregate_status, summary, initiated_by, actor_display,
         json.dumps(input_refs), json.dumps(produced_refs or []), output_count,
         json.dumps(public_error) if public_error else None),
    )
    cur.execute(
        """
        INSERT INTO event_outbox (tenant_id, invoice_id, event_id, state, available_at)
        VALUES (%s, %s, %s, 'PENDING', %s);
        """,
        (tenant_id, invoice_id, event_id, occurred_at),
    )

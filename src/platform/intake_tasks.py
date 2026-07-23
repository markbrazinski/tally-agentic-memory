"""Durable CockroachDB task leasing and Intake completion transactions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from src.core.intake import TaskType, task_input_fingerprint
from src.core.intake_claims import ClaimValidation
from src.external.dal import DAL
from src.external.intake_bedrock import MODEL_ID, SCHEMA_VERSION, TEMPLATE_VERSION
from src.external.invoice_source_store import StoredInvoiceSource

MAX_AUTOMATIC_ATTEMPTS = 3


class TaskLeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractionTaskLease:
    task_id: str
    invoice_id: str
    source_id: str
    attempt: int
    worker_id: str
    lease_expires_at: datetime
    knowledge_cutoff_at: datetime
    source: StoredInvoiceSource
    initiated_by: str | None
    actor_display: str


@dataclass(frozen=True)
class ExtractionCompletion:
    extraction_run_id: str
    claim_set_id: str
    claim_set_version: int
    reconstruction_task_id: str | None
    status: str


@dataclass(frozen=True)
class RetryResult:
    task_id: str
    task_version: int
    row_version: int
    replay: bool


def claim_next_extraction_task(
    dal: DAL,
    *,
    worker_id: str,
    lease_seconds: int = 90,
) -> ExtractionTaskLease | None:
    tenant_id = dal.tenant.tenant_id

    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.invoice_id, w.current_attempt,
                       w.knowledge_cutoff_at, w.initiated_by, w.actor_display,
                       s.id, s.s3_bucket_ref_private, s.s3_object_key_private,
                       s.s3_version_id_private, s.sha256, s.byte_length
                FROM workflow_tasks w
                JOIN invoice_sources s
                  ON s.tenant_id=w.tenant_id AND s.invoice_id=w.invoice_id
                 AND s.source_type='INVOICE_PDF'
                WHERE w.tenant_id=%s
                  AND w.task_type='EXTRACT_INVOICE_CLAIMS'
                  AND (
                    w.state='PENDING'
                    OR (
                      w.state='RETRY_WAIT'
                      AND (w.not_before IS NULL OR w.not_before <= now())
                    )
                    OR (
                      w.state IN ('LEASED', 'RUNNING')
                      AND w.lease_expires_at < now()
                    )
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
            attempt = int(row[2]) + 1
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
                (
                    attempt,
                    worker_id,
                    lease_expires_at,
                    started_at,
                    started_at,
                    tenant_id,
                    row[0],
                ),
            )
            cur.execute(
                """
                INSERT INTO workflow_task_attempts
                    (tenant_id, task_id, attempt, state, lease_owner,
                     lease_expires_at, started_at)
                VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s);
                """,
                (
                    tenant_id,
                    row[0],
                    attempt,
                    worker_id,
                    lease_expires_at,
                    started_at,
                ),
            )
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=str(row[1]),
                intake_state="INITIAL_PROCESSING",
                aggregate_status="INITIAL_PROCESSING",
                status="INITIAL_PROCESSING",
                increment=1,
                occurred_at=started_at,
            )
            _insert_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=str(row[1]),
                sequence=sequence,
                event_type="intake.extraction_started",
                occurred_at=started_at,
                task="EXTRACT_INVOICE_CLAIMS",
                tool_display_name="Amazon Bedrock",
                state="RUNNING",
                aggregate_status="INITIAL_PROCESSING",
                summary="Carrier claim extraction started",
                initiated_by=str(row[4]) if row[4] else None,
                actor_display=row[5],
                input_refs=[{"type": "invoice_source", "id": str(row[6]), "version": 1}],
            )
            return ExtractionTaskLease(
                task_id=str(row[0]),
                invoice_id=str(row[1]),
                source_id=str(row[6]),
                attempt=attempt,
                worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                knowledge_cutoff_at=row[3],
                source=StoredInvoiceSource(
                    bucket_ref_private=row[7],
                    object_key_private=row[8],
                    version_id_private=row[9],
                    sha256=row[10],
                    byte_length=int(row[11]),
                ),
                initiated_by=str(row[4]) if row[4] else None,
                actor_display=row[5],
            )

    return dal.run_with_retry(_claim)


def complete_extraction(
    dal: DAL,
    *,
    lease: ExtractionTaskLease,
    validation: ClaimValidation,
    raw_response_sha256: str,
    provider_request_ref_private: str | None,
) -> ExtractionCompletion:
    tenant_id = dal.tenant.tenant_id

    def _complete(conn):
        with conn.cursor() as cur:
            _assert_current_lease(cur, tenant_id, lease)
            cur.execute("SELECT now();")
            completed_at = cur.fetchone()[0]
            extraction_run_id = str(uuid4())
            claim_set_id = str(uuid4())
            cur.execute(
                """
                SELECT COALESCE(max(claim_set_version), 0) + 1
                FROM claim_sets
                WHERE tenant_id=%s AND invoice_id=%s;
                """,
                (tenant_id, lease.invoice_id),
            )
            claim_set_version = int(cur.fetchone()[0])
            validation_state = "VALIDATED" if validation.passed else "INVALID"
            cur.execute(
                """
                INSERT INTO extraction_runs
                    (tenant_id, id, invoice_id, source_id, source_sha256,
                     source_version_ref_private, model_id, schema_version,
                     template_version, attempt, requested_at, responded_at,
                     provider_request_ref_private, raw_response_sha256,
                     validation_state, issue_codes)
                SELECT %s, %s, %s, %s, s.sha256, s.s3_version_id_private,
                       %s, %s, %s, %s, a.started_at, %s, %s, %s, %s, %s
                FROM invoice_sources s
                JOIN workflow_task_attempts a
                  ON a.tenant_id=s.tenant_id AND a.task_id=%s AND a.attempt=%s
                WHERE s.tenant_id=%s AND s.id=%s;
                """,
                (
                    tenant_id,
                    extraction_run_id,
                    lease.invoice_id,
                    lease.source_id,
                    MODEL_ID,
                    SCHEMA_VERSION,
                    TEMPLATE_VERSION,
                    lease.attempt,
                    completed_at,
                    provider_request_ref_private,
                    raw_response_sha256,
                    validation_state,
                    json.dumps(validation.issue_codes),
                    lease.task_id,
                    lease.attempt,
                    tenant_id,
                    lease.source_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO claim_sets
                    (tenant_id, id, invoice_id, claim_set_version,
                     extraction_run_id, validation_state, issue_codes)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    tenant_id,
                    claim_set_id,
                    lease.invoice_id,
                    claim_set_version,
                    extraction_run_id,
                    validation_state,
                    json.dumps(validation.issue_codes),
                ),
            )
            for claim in validation.claims:
                cur.execute(
                    """
                    INSERT INTO extracted_claims
                        (tenant_id, claim_set_id, field_name, value_type,
                         raw_value, normalized_value, amount_minor, currency,
                         validation_state, page_number, bounding_box,
                         text_excerpt, text_excerpt_sha256)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'VALIDATED',
                            %s, %s, %s, %s);
                    """,
                    (
                        tenant_id,
                        claim_set_id,
                        claim.field_name,
                        claim.value_type,
                        claim.raw_value,
                        json.dumps(claim.normalized_value),
                        claim.amount_minor,
                        claim.currency,
                        claim.anchor.page_number,
                        json.dumps(claim.anchor.bounding_box),
                        claim.anchor.text_excerpt,
                        claim.anchor.text_excerpt_sha256,
                    ),
                )
            cur.execute(
                """
                UPDATE workflow_tasks
                SET state='COMPLETED', completed_at=%s, lease_owner=NULL,
                    lease_expires_at=NULL, public_summary=%s, updated_at=%s
                WHERE tenant_id=%s AND id=%s AND current_attempt=%s
                  AND lease_owner=%s;
                """,
                (
                    completed_at,
                    (
                        "Carrier claims extracted and validated"
                        if validation.passed
                        else "Carrier claims require evidence"
                    ),
                    completed_at,
                    tenant_id,
                    lease.task_id,
                    lease.attempt,
                    lease.worker_id,
                ),
            )
            cur.execute(
                """
                UPDATE workflow_task_attempts
                SET state='COMPLETED', completed_at=%s
                WHERE tenant_id=%s AND task_id=%s AND attempt=%s
                  AND lease_owner=%s;
                """,
                (
                    completed_at,
                    tenant_id,
                    lease.task_id,
                    lease.attempt,
                    lease.worker_id,
                ),
            )

            reconstruction_task_id = None
            if validation.passed:
                reconstruction_task_id = str(uuid4())
                reconstruction_refs = [
                    {
                        "type": "invoice_source",
                        "id": lease.source_id,
                        "version": 1,
                    },
                    {
                        "type": "claim_set",
                        "id": claim_set_id,
                        "version": claim_set_version,
                    },
                ]
                fingerprint = task_input_fingerprint(
                    task_type=TaskType.START_RECONSTRUCTION,
                    input_refs=reconstruction_refs,
                )
                cur.execute(
                    """
                    INSERT INTO workflow_tasks
                        (tenant_id, id, invoice_id, task_type, task_version,
                         state, initiated_by, actor_display, knowledge_cutoff_at,
                         input_fingerprint, input_object_refs, public_summary)
                    VALUES (%s, %s, %s, 'START_RECONSTRUCTION', 1, 'PENDING',
                            %s, %s, %s, %s, %s,
                            'Waiting to reconstruct pre-invoice events')
                    ON CONFLICT
                        (tenant_id, invoice_id, task_type, task_version,
                         input_fingerprint)
                    DO NOTHING;
                    """,
                    (
                        tenant_id,
                        reconstruction_task_id,
                        lease.invoice_id,
                        lease.initiated_by,
                        lease.actor_display,
                        lease.knowledge_cutoff_at,
                        fingerprint,
                        json.dumps(reconstruction_refs),
                    ),
                )
                first_sequence = _lock_invoice_and_advance(
                    cur,
                    tenant_id=tenant_id,
                    invoice_id=lease.invoice_id,
                    intake_state="READY_FOR_RECONSTRUCTION",
                    aggregate_status="RECONSTRUCTING",
                    status="RECONSTRUCTING",
                    increment=2,
                    occurred_at=completed_at,
                    active_claim_set_version=claim_set_version,
                )
                _insert_event(
                    cur,
                    tenant_id=tenant_id,
                    invoice_id=lease.invoice_id,
                    sequence=first_sequence,
                    event_type="intake.claims_validated",
                    occurred_at=completed_at,
                    task="VALIDATE_INVOICE_CLAIMS",
                    tool_display_name=None,
                    state="COMPLETED",
                    aggregate_status="RECONSTRUCTING",
                    summary=f"{len(validation.claims)} carrier claims validated",
                    initiated_by=lease.initiated_by,
                    actor_display=lease.actor_display,
                    input_refs=[
                        {"type": "invoice_source", "id": lease.source_id, "version": 1}
                    ],
                    produced_refs=[
                        {
                            "type": "claim_set",
                            "id": claim_set_id,
                            "version": claim_set_version,
                        }
                    ],
                    output_count=len(validation.claims),
                )
                _insert_event(
                    cur,
                    tenant_id=tenant_id,
                    invoice_id=lease.invoice_id,
                    sequence=first_sequence + 1,
                    event_type="invoice.reconstruction_started",
                    occurred_at=completed_at,
                    task="START_RECONSTRUCTION",
                    tool_display_name="CockroachDB Managed MCP",
                    state="PENDING",
                    aggregate_status="RECONSTRUCTING",
                    summary="Invoice handed to pre-invoice reconstruction",
                    initiated_by=lease.initiated_by,
                    actor_display=lease.actor_display,
                    input_refs=reconstruction_refs,
                    produced_refs=[
                        {
                            "type": "workflow_task",
                            "id": reconstruction_task_id,
                            "version": 1,
                        }
                    ],
                )
                status = "RECONSTRUCTING"
            else:
                sequence = _lock_invoice_and_advance(
                    cur,
                    tenant_id=tenant_id,
                    invoice_id=lease.invoice_id,
                    intake_state="REQUIRED_FIELD_MISSING",
                    aggregate_status="NEEDS_EVIDENCE",
                    status="NEEDS_EVIDENCE",
                    increment=1,
                    occurred_at=completed_at,
                    active_claim_set_version=claim_set_version,
                )
                _insert_event(
                    cur,
                    tenant_id=tenant_id,
                    invoice_id=lease.invoice_id,
                    sequence=sequence,
                    event_type="intake.claims_need_evidence",
                    occurred_at=completed_at,
                    task="VALIDATE_INVOICE_CLAIMS",
                    tool_display_name=None,
                    state="BLOCKED",
                    aggregate_status="NEEDS_EVIDENCE",
                    summary="Required carrier claims need evidence",
                    initiated_by=lease.initiated_by,
                    actor_display=lease.actor_display,
                    input_refs=[
                        {"type": "invoice_source", "id": lease.source_id, "version": 1}
                    ],
                    produced_refs=[
                        {
                            "type": "claim_set",
                            "id": claim_set_id,
                            "version": claim_set_version,
                        }
                    ],
                    output_count=len(validation.claims),
                )
                status = "NEEDS_EVIDENCE"

            return ExtractionCompletion(
                extraction_run_id=extraction_run_id,
                claim_set_id=claim_set_id,
                claim_set_version=claim_set_version,
                reconstruction_task_id=reconstruction_task_id,
                status=status,
            )

    return dal.run_with_retry(_complete)


def fail_extraction(
    dal: DAL,
    *,
    lease: ExtractionTaskLease,
    error_code: str,
    retryable: bool,
) -> str:
    tenant_id = dal.tenant.tenant_id

    def _fail(conn):
        with conn.cursor() as cur:
            _assert_current_lease(cur, tenant_id, lease)
            cur.execute("SELECT now();")
            failed_at = cur.fetchone()[0]
            will_retry = retryable and lease.attempt < MAX_AUTOMATIC_ATTEMPTS
            task_state = "RETRY_WAIT" if will_retry else "FAILED"
            not_before = (
                failed_at + timedelta(seconds=2 ** lease.attempt)
                if will_retry
                else None
            )
            intake_state = (
                "INITIAL_PROCESSING"
                if will_retry
                else (
                    "BLOCKED_SOURCE_VERSION_UNAVAILABLE"
                    if error_code.startswith("SOURCE_")
                    else "EXTRACTION_FAILED"
                )
            )
            aggregate_status = (
                "INITIAL_PROCESSING" if will_retry else "BLOCKED"
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
                (
                    task_state,
                    not_before,
                    error_code,
                    (
                        "Extraction will retry"
                        if will_retry
                        else "Extraction is blocked"
                    ),
                    failed_at,
                    tenant_id,
                    lease.task_id,
                    lease.attempt,
                    lease.worker_id,
                ),
            )
            cur.execute(
                """
                UPDATE workflow_task_attempts
                SET state=%s, completed_at=%s, private_error_code=%s
                WHERE tenant_id=%s AND task_id=%s AND attempt=%s
                  AND lease_owner=%s;
                """,
                (
                    task_state,
                    failed_at,
                    error_code,
                    tenant_id,
                    lease.task_id,
                    lease.attempt,
                    lease.worker_id,
                ),
            )
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                intake_state=intake_state,
                aggregate_status=aggregate_status,
                status=aggregate_status,
                increment=1,
                occurred_at=failed_at,
            )
            _insert_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=lease.invoice_id,
                sequence=sequence,
                event_type=(
                    "intake.extraction_retry_scheduled"
                    if will_retry
                    else "intake.extraction_blocked"
                ),
                occurred_at=failed_at,
                task="EXTRACT_INVOICE_CLAIMS",
                tool_display_name="Amazon Bedrock",
                state=task_state,
                aggregate_status=aggregate_status,
                summary=(
                    "Carrier claim extraction will retry"
                    if will_retry
                    else "Carrier claim extraction is blocked"
                ),
                initiated_by=lease.initiated_by,
                actor_display=lease.actor_display,
                input_refs=[
                    {"type": "invoice_source", "id": lease.source_id, "version": 1}
                ],
            )
            return task_state

    return dal.run_with_retry(_fail)


def retry_extraction_task(
    dal: DAL,
    *,
    invoice_id: str,
    idempotency_key: str,
    expected_row_version: int,
    initiated_by: str | None,
    actor_display: str,
) -> RetryResult:
    tenant_id = dal.tenant.tenant_id
    request_hash = task_input_fingerprint(
        task_type=TaskType.EXTRACT_INVOICE_CLAIMS,
        input_refs=[
            {"type": "invoice", "id": invoice_id, "version": expected_row_version}
        ],
    )

    def _retry(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_retry_requests
                    (tenant_id, idempotency_key, invoice_id, task_type,
                     request_hash, state, initiated_by, actor_display)
                VALUES (%s, %s, %s, 'EXTRACT_INVOICE_CLAIMS', %s, 'RESERVED',
                        %s, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
                """,
                (
                    tenant_id,
                    idempotency_key,
                    invoice_id,
                    request_hash,
                    initiated_by,
                    actor_display,
                ),
            )
            cur.execute(
                """
                SELECT invoice_id, request_hash, state, response_snapshot
                FROM workflow_retry_requests
                WHERE tenant_id=%s AND idempotency_key=%s
                FOR UPDATE;
                """,
                (tenant_id, idempotency_key),
            )
            request = cur.fetchone()
            if (
                request is None
                or str(request[0]) != invoice_id
                or request[1] != request_hash
            ):
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if request[2] == "COMPLETED":
                snapshot = (
                    request[3]
                    if isinstance(request[3], dict)
                    else json.loads(request[3])
                )
                return RetryResult(
                    task_id=snapshot["task_id"],
                    task_version=int(snapshot["task_version"]),
                    row_version=int(snapshot["row_version"]),
                    replay=True,
                )
            cur.execute(
                """
                SELECT row_version, status_sequence
                FROM invoices
                WHERE tenant_id=%s AND id=%s
                FOR UPDATE;
                """,
                (tenant_id, invoice_id),
            )
            invoice = cur.fetchone()
            if invoice is None:
                raise ValueError("INVOICE_NOT_FOUND")
            if int(invoice[0]) != expected_row_version:
                raise ValueError("STALE_INVOICE_VERSION")
            cur.execute(
                """
                SELECT task_version, state, input_fingerprint,
                       input_object_refs, knowledge_cutoff_at
                FROM workflow_tasks
                WHERE tenant_id=%s AND invoice_id=%s
                  AND task_type='EXTRACT_INVOICE_CLAIMS'
                ORDER BY task_version DESC
                LIMIT 1
                FOR UPDATE;
                """,
                (tenant_id, invoice_id),
            )
            previous = cur.fetchone()
            if previous is None or previous[1] != "FAILED":
                raise ValueError("INVALID_STATE")
            task_id = str(uuid4())
            task_version = int(previous[0]) + 1
            cur.execute(
                """
                INSERT INTO workflow_tasks
                    (tenant_id, id, invoice_id, task_type, task_version, state,
                     initiated_by, actor_display, knowledge_cutoff_at,
                     input_fingerprint, input_object_refs, public_summary)
                VALUES (%s, %s, %s, 'EXTRACT_INVOICE_CLAIMS', %s, 'PENDING',
                        %s, %s, %s, %s, %s,
                        'Waiting to retry carrier claim extraction');
                """,
                (
                    tenant_id,
                    task_id,
                    invoice_id,
                    task_version,
                    initiated_by,
                    actor_display,
                    previous[4],
                    previous[2],
                    json.dumps(previous[3]) if isinstance(previous[3], list) else previous[3],
                ),
            )
            cur.execute("SELECT now();")
            occurred_at = cur.fetchone()[0]
            sequence = _lock_invoice_and_advance(
                cur,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                intake_state="RECEIVED",
                aggregate_status="RECEIVED",
                status="RECEIVED",
                increment=1,
                occurred_at=occurred_at,
            )
            _insert_event(
                cur,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                sequence=sequence,
                event_type="intake.retry_requested",
                occurred_at=occurred_at,
                task="EXTRACT_INVOICE_CLAIMS",
                tool_display_name=None,
                state="PENDING",
                aggregate_status="RECEIVED",
                summary="Carrier claim extraction retry requested",
                initiated_by=initiated_by,
                actor_display=actor_display,
                input_refs=_json_refs(previous[3]),
                produced_refs=[
                    {"type": "workflow_task", "id": task_id, "version": task_version}
                ],
            )
            row_version = int(invoice[0]) + 1
            snapshot = {
                "task_id": task_id,
                "task_version": task_version,
                "row_version": row_version,
            }
            cur.execute(
                """
                UPDATE workflow_retry_requests
                SET state='COMPLETED', response_snapshot=%s, updated_at=%s
                WHERE tenant_id=%s AND idempotency_key=%s;
                """,
                (
                    json.dumps(snapshot),
                    occurred_at,
                    tenant_id,
                    idempotency_key,
                ),
            )
            return RetryResult(task_id, task_version, row_version, False)

    return dal.run_with_retry(_retry)


def _assert_current_lease(cur, tenant_id: str, lease: ExtractionTaskLease) -> None:
    cur.execute(
        """
        SELECT state, current_attempt, lease_owner, lease_expires_at
        FROM workflow_tasks
        WHERE tenant_id=%s AND id=%s
        FOR UPDATE;
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


def _lock_invoice_and_advance(
    cur,
    *,
    tenant_id: str,
    invoice_id: str,
    intake_state: str,
    aggregate_status: str,
    status: str,
    increment: int,
    occurred_at: datetime,
    active_claim_set_version: int | None = None,
) -> int:
    cur.execute(
        """
        SELECT status_sequence FROM invoices
        WHERE tenant_id=%s AND id=%s
        FOR UPDATE;
        """,
        (tenant_id, invoice_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("INVOICE_NOT_FOUND")
    first_sequence = int(row[0]) + 1
    cur.execute(
        """
        UPDATE invoices
        SET intake_state=%s, aggregate_status=%s, status=%s,
            status_sequence=status_sequence + %s,
            row_version=row_version + 1,
            active_claim_set_version=COALESCE(%s, active_claim_set_version),
            updated_at=%s
        WHERE tenant_id=%s AND id=%s;
        """,
        (
            intake_state,
            aggregate_status,
            status,
            increment,
            active_claim_set_version,
            occurred_at,
            tenant_id,
            invoice_id,
        ),
    )
    return first_sequence


def _insert_event(
    cur,
    *,
    tenant_id: str,
    invoice_id: str,
    sequence: int,
    event_type: str,
    occurred_at: datetime,
    task: str,
    tool_display_name: str | None,
    state: str,
    aggregate_status: str,
    summary: str,
    initiated_by: str | None,
    actor_display: str,
    input_refs: list[dict[str, Any]],
    produced_refs: list[dict[str, Any]] | None = None,
    output_count: int | None = None,
) -> None:
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_events
            (tenant_id, id, invoice_id, sequence, event_type, schema_version,
             occurred_at, role, task, tool_display_name, state,
             aggregate_status, summary, initiated_by, actor_display,
             input_object_refs, produced_object_refs, output_count)
        VALUES (%s, %s, %s, %s, %s, 1, %s, 'INTAKE_AGENT', %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s);
        """,
        (
            tenant_id,
            event_id,
            invoice_id,
            sequence,
            event_type,
            occurred_at,
            task,
            tool_display_name,
            state,
            aggregate_status,
            summary,
            initiated_by,
            actor_display,
            json.dumps(input_refs),
            json.dumps(produced_refs or []),
            output_count,
        ),
    )
    cur.execute(
        """
        INSERT INTO event_outbox
            (tenant_id, invoice_id, event_id, state, available_at)
        VALUES (%s, %s, %s, 'PENDING', %s);
        """,
        (tenant_id, invoice_id, event_id, occurred_at),
    )


def _json_refs(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else json.loads(value)

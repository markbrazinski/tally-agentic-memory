"""CockroachDB transactions for idempotent Intake receipt finalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.core.intake import TaskType, task_input_fingerprint
from src.external.dal import DAL
from src.external.invoice_source_store import StoredInvoiceSource


class IdempotencyConflictError(ValueError):
    pass


class IngestionStateError(ValueError):
    pass


@dataclass(frozen=True)
class IngestionReservation:
    invoice_id: str
    source_id: str
    state: str
    response_snapshot: dict[str, Any] | None
    is_new: bool
    stored_source: StoredInvoiceSource | None


@dataclass(frozen=True)
class FinalizeReceipt:
    idempotency_key: str
    request_hash: str
    carrier_id: str
    display_name: str
    mime_type: str
    stored_source: StoredInvoiceSource
    actor_id: str | None
    actor_display: str
    provenance_classification: str
    public_disclosure: str


def reserve_ingestion(
    dal: DAL,
    *,
    idempotency_key: str,
    request_hash: str,
    actor_id: str | None,
    actor_display: str,
) -> IngestionReservation:
    invoice_id = str(uuid4())
    source_id = str(uuid4())
    tenant_id = dal.tenant.tenant_id

    def _reserve(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_requests
                    (tenant_id, idempotency_key, request_hash, state, initiated_by,
                     actor_display, reserved_invoice_id, reserved_source_id)
                VALUES (%s, %s, %s, 'RESERVED', %s, %s, %s, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
                """,
                (
                    tenant_id,
                    idempotency_key,
                    request_hash,
                    actor_id,
                    actor_display,
                    invoice_id,
                    source_id,
                ),
            )
            cur.execute(
                """
                SELECT request_hash, state, reserved_invoice_id, reserved_source_id,
                       response_snapshot, s3_bucket_ref_private,
                       s3_object_key_private, s3_version_id_private,
                       source_sha256, source_byte_length
                FROM ingestion_requests
                WHERE tenant_id=%s AND idempotency_key=%s
                FOR UPDATE;
                """,
                (tenant_id, idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("ingestion reservation readback failed")
            if row[0] != request_hash:
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            snapshot = row[4] if isinstance(row[4], dict) or row[4] is None else json.loads(row[4])
            stored_source = None
            if row[5] is not None:
                stored_source = StoredInvoiceSource(
                    bucket_ref_private=row[5],
                    object_key_private=row[6],
                    version_id_private=row[7],
                    sha256=row[8],
                    byte_length=int(row[9]),
                )
            return IngestionReservation(
                invoice_id=str(row[2]),
                source_id=str(row[3]),
                state=row[1],
                response_snapshot=snapshot,
                is_new=str(row[2]) == invoice_id,
                stored_source=stored_source,
            )

    return dal.run_with_retry(_reserve)


def record_source_stored(
    dal: DAL,
    *,
    idempotency_key: str,
    request_hash: str,
    source: StoredInvoiceSource,
) -> None:
    tenant_id = dal.tenant.tenant_id

    def _record(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_hash, state FROM ingestion_requests
                WHERE tenant_id=%s AND idempotency_key=%s FOR UPDATE;
                """,
                (tenant_id, idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                raise IngestionStateError("INGESTION_REQUEST_NOT_FOUND")
            if row[0] != request_hash:
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            if row[1] == "COMPLETED":
                return
            cur.execute(
                """
                UPDATE ingestion_requests
                SET state='SOURCE_STORED_DB_PENDING',
                    s3_bucket_ref_private=%s, s3_object_key_private=%s,
                    s3_version_id_private=%s, source_sha256=%s,
                    source_byte_length=%s, updated_at=now()
                WHERE tenant_id=%s AND idempotency_key=%s;
                """,
                (
                    source.bucket_ref_private,
                    source.object_key_private,
                    source.version_id_private,
                    source.sha256,
                    source.byte_length,
                    tenant_id,
                    idempotency_key,
                ),
            )

    dal.run_with_retry(_record)


def finalize_received_invoice(dal: DAL, command: FinalizeReceipt) -> dict[str, Any]:
    tenant_id = dal.tenant.tenant_id

    def _finalize(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_hash, state, reserved_invoice_id, reserved_source_id,
                       response_snapshot, s3_bucket_ref_private, s3_object_key_private,
                       s3_version_id_private, source_sha256, source_byte_length
                FROM ingestion_requests
                WHERE tenant_id=%s AND idempotency_key=%s
                FOR UPDATE;
                """,
                (tenant_id, command.idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                raise IngestionStateError("INGESTION_REQUEST_NOT_FOUND")
            if row[0] != command.request_hash:
                raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
            if row[1] == "COMPLETED":
                return row[4] if isinstance(row[4], dict) else json.loads(row[4])
            if row[1] != "SOURCE_STORED_DB_PENDING":
                raise IngestionStateError(f"INGESTION_NOT_FINALIZABLE:{row[1]}")

            invoice_id, source_id = str(row[2]), str(row[3])
            stored_tuple = (
                row[5],
                row[6],
                row[7],
                row[8],
                int(row[9]),
            )
            command_tuple = (
                command.stored_source.bucket_ref_private,
                command.stored_source.object_key_private,
                command.stored_source.version_id_private,
                command.stored_source.sha256,
                command.stored_source.byte_length,
            )
            if stored_tuple != command_tuple:
                raise IngestionStateError("SOURCE_BINDING_MISMATCH")

            cur.execute("SELECT now();")
            received_at: datetime = cur.fetchone()[0]
            task_id, event_id, outbox_id = (str(uuid4()) for _ in range(3))
            input_refs = [
                {"type": "invoice_source", "id": source_id, "version": 1},
            ]
            fingerprint = task_input_fingerprint(
                task_type=TaskType.EXTRACT_INVOICE_CLAIMS,
                input_refs=input_refs,
            )
            produced_refs = [
                {"type": "invoice_source", "id": source_id, "version": 1},
            ]

            cur.execute(
                """
                INSERT INTO invoices
                    (tenant_id, id, carrier_id, received_at, s3_key, sha256,
                     source_version_id, status, display_name, intake_state,
                     aggregate_status, status_sequence, row_version, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'RECEIVED', %s, 'RECEIVED',
                     'RECEIVED', 1, 1, %s);
                """,
                (
                    tenant_id,
                    invoice_id,
                    command.carrier_id,
                    received_at,
                    command.stored_source.object_key_private,
                    command.stored_source.sha256,
                    command.stored_source.version_id_private,
                    command.display_name,
                    received_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO invoice_sources
                    (tenant_id, id, invoice_id, source_type, display_filename,
                     mime_type, byte_length, sha256, s3_bucket_ref_private,
                     s3_object_key_private, s3_version_id_private,
                     preservation_status, provenance_classification,
                     public_disclosure, verified_at, received_at)
                VALUES
                    (%s, %s, %s, 'INVOICE_PDF', %s, %s, %s, %s, %s, %s, %s,
                     'VERSION_VERIFIED', %s, %s, %s, %s);
                """,
                (
                    tenant_id,
                    source_id,
                    invoice_id,
                    command.display_name,
                    command.mime_type,
                    command.stored_source.byte_length,
                    command.stored_source.sha256,
                    command.stored_source.bucket_ref_private,
                    command.stored_source.object_key_private,
                    command.stored_source.version_id_private,
                    command.provenance_classification,
                    command.public_disclosure,
                    received_at,
                    received_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO workflow_tasks
                    (tenant_id, id, invoice_id, task_type, task_version, state,
                     initiated_by, actor_display, knowledge_cutoff_at,
                     input_fingerprint, input_object_refs, public_summary)
                VALUES (%s, %s, %s, 'EXTRACT_INVOICE_CLAIMS', 1, 'PENDING',
                        %s, %s, %s, %s, %s,
                        'Waiting to extract carrier claims');
                """,
                (
                    tenant_id,
                    task_id,
                    invoice_id,
                    command.actor_id,
                    command.actor_display,
                    received_at,
                    fingerprint,
                    json.dumps(input_refs),
                ),
            )
            cur.execute(
                """
                INSERT INTO invoice_events
                    (tenant_id, id, invoice_id, sequence, event_type, schema_version,
                     occurred_at, role, task, tool_display_name, state,
                     aggregate_status, summary, initiated_by, actor_display,
                     input_object_refs,
                     produced_object_refs)
                VALUES
                    (%s, %s, %s, 1, 'invoice.received', 1, %s, 'INTAKE_AGENT',
                     'PRESERVE_INVOICE_SOURCE', 'Amazon S3', 'COMPLETED',
                     'RECEIVED', 'Invoice received and original source verified',
                     %s, %s, '[]', %s);
                """,
                (
                    tenant_id,
                    event_id,
                    invoice_id,
                    received_at,
                    command.actor_id,
                    command.actor_display,
                    json.dumps(produced_refs),
                ),
            )
            cur.execute(
                """
                INSERT INTO event_outbox
                    (tenant_id, id, invoice_id, event_id, state, available_at)
                VALUES (%s, %s, %s, %s, 'PENDING', %s);
                """,
                (tenant_id, outbox_id, invoice_id, event_id, received_at),
            )

            snapshot = {
                "invoice": {
                    "invoice_id": invoice_id,
                    "display_name": command.display_name,
                    "status": "RECEIVED",
                    "status_sequence": 1,
                    "received_at": received_at.isoformat(),
                    "invoice_source": {
                        "source_id": source_id,
                        "filename": command.display_name,
                        "mime_type": command.mime_type,
                        "preservation_status": "VERSION_VERIFIED",
                        "claimed_total_minor": None,
                        "currency": None,
                    },
                    "recommendation": None,
                    "next_action": None,
                },
                "links": {
                    "self": f"/api/invoices/{invoice_id}",
                    "workbench": f"/invoices/{invoice_id}",
                    "events": f"/api/invoices/{invoice_id}/events",
                    "source": f"/api/invoices/{invoice_id}/sources/{source_id}/content",
                },
            }
            cur.execute(
                """
                UPDATE ingestion_requests
                SET state='COMPLETED', response_snapshot=%s, updated_at=%s
                WHERE tenant_id=%s AND idempotency_key=%s;
                """,
                (
                    json.dumps(snapshot),
                    received_at,
                    tenant_id,
                    command.idempotency_key,
                ),
            )
            return snapshot

    return dal.run_with_retry(_finalize)

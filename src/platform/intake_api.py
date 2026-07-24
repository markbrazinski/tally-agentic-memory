"""Controlled Intake HTTP service backed by exact S3 versions and CockroachDB."""

from __future__ import annotations

import hashlib
import json
import os
import time
from io import BytesIO
from typing import Any

import boto3
import pdfplumber
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from src.core.intake import PdfEnvelope, validate_pdf_envelope
from src.external.dal import DAL, Tenant
from src.external.invoice_source_store import (
    SourcePersistenceError,
    StoredInvoiceSource,
    VersionedInvoiceSourceStore,
)
from src.platform.auth import AuthedActor
from src.platform.authority_seal_api import register_authority_seal_routes
from src.platform.intake_events import load_event_history, sse_stream
from src.platform.intake_repository import (
    FinalizeReceipt,
    IdempotencyConflictError,
    IngestionStateError,
    complete_duplicate_ingestion,
    finalize_received_invoice,
    find_invoice_by_sha,
    record_source_stored,
    reserve_ingestion,
)
from src.platform.intake_tasks import retry_extraction_task
from src.platform.reconstruction_api import register_reconstruction_routes

MAX_PDF_PAGES = 10
MAX_PDF_PARSE_SECONDS = 5.0
ALLOWED_SCENARIOS = frozenset({"locked-inv-1048"})


class IntakeUnavailableError(RuntimeError):
    pass


def make_router(*, require_auth) -> APIRouter:
    router = APIRouter()

    @router.post("/api/demo/invoices")
    async def create_demo_invoice(
        file: UploadFile = File(...),
        demo_scenario: str = Form("locked-inv-1048"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: AuthedActor = Depends(require_auth),
    ) -> Response:
        if not idempotency_key or not idempotency_key.strip():
            raise _http_error(400, "IDEMPOTENCY_KEY_REQUIRED")
        if demo_scenario not in ALLOWED_SCENARIOS:
            raise _http_error(422, "INVALID_DEMO_SCENARIO")
        body = await file.read(15 * 1024 * 1024 + 1)
        try:
            envelope = validate_pdf_envelope(body, file.filename)
            validate_supported_pdf(body)
        except ValueError as exc:
            code = str(exc)
            status = {
                "FILE_TOO_LARGE": 413,
                "UNSUPPORTED_FILE": 415,
                "EMPTY_FILE": 415,
                "ENCRYPTED_OR_UNREADABLE_PDF": 422,
                "PDF_PAGE_LIMIT_EXCEEDED": 422,
                "PDF_PARSE_TIMEOUT": 422,
            }.get(code, 422)
            raise _http_error(status, code) from exc

        try:
            snapshot, replay = receive_invoice(
                body=body,
                envelope=envelope,
                idempotency_key=idempotency_key.strip(),
                demo_scenario=demo_scenario,
                actor=actor,
            )
        except IdempotencyConflictError as exc:
            raise _http_error(409, "IDEMPOTENCY_CONFLICT") from exc
        except IngestionStateError as exc:
            code = str(exc).split(":", 1)[0]
            status = 409 if code == "REQUEST_IN_PROGRESS" else 503
            raise _http_error(status, code) from exc
        except SourcePersistenceError as exc:
            raise _http_error(503, str(exc)) from exc
        except IntakeUnavailableError as exc:
            raise _http_error(503, str(exc)) from exc

        invoice_id = snapshot["invoice"]["invoice_id"]
        response = JSONResponse(snapshot, status_code=200 if replay else 201)
        response.headers["Location"] = f"/api/invoices/{invoice_id}"
        response.headers["Idempotent-Replay"] = "true" if replay else "false"
        return response

    @router.get("/api/invoices")
    def list_invoices(limit: int = Query(default=50, ge=1, le=50)) -> dict[str, Any]:
        with DAL.connect(Tenant(_tenant_id(), "public-queue-reader")) as dal:
            with dal.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM invoices
                    WHERE tenant_id=%s
                    ORDER BY received_at DESC, id DESC
                    LIMIT %s;
                    """,
                    (dal.tenant.tenant_id, limit),
                )
                invoice_ids = [str(row[0]) for row in cur.fetchall()]
        return {
            "invoices": [
                load_invoice_projection(invoice_id)[0]["invoice"]
                for invoice_id in invoice_ids
            ],
            "next_cursor": None,
        }

    @router.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: str) -> Response:
        snapshot, row_version = load_invoice_projection(invoice_id)
        response = JSONResponse(snapshot)
        response.headers["ETag"] = f'"invoice-{invoice_id}-v{row_version}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.get("/api/invoices/{invoice_id}/events")
    def get_invoice_events(
        invoice_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with DAL.connect(Tenant(_tenant_id(), "public-event-reader")) as dal:
            events, _ = load_event_history(
                dal,
                invoice_id=invoice_id,
                after_sequence=after_sequence,
            )
        return {"events": events}

    @router.post("/api/invoices/{invoice_id}/intake/retry")
    def retry_invoice_intake(
        invoice_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
        actor: AuthedActor = Depends(require_auth),
    ) -> Response:
        if not idempotency_key:
            raise _http_error(400, "IDEMPOTENCY_KEY_REQUIRED")
        expected_version = _parse_invoice_etag(invoice_id, if_match)
        try:
            with DAL.connect(Tenant(_tenant_id(), actor.display_name)) as dal:
                result = retry_extraction_task(
                    dal,
                    invoice_id=invoice_id,
                    idempotency_key=idempotency_key,
                    expected_row_version=expected_version,
                    initiated_by=actor.user_id,
                    actor_display=actor.display_name,
                )
        except ValueError as exc:
            code = str(exc)
            status = {
                "STALE_INVOICE_VERSION": 412,
                "INVOICE_NOT_FOUND": 404,
                "INVALID_STATE": 409,
                "IDEMPOTENCY_CONFLICT": 409,
            }.get(code, 409)
            raise _http_error(status, code) from exc
        response = JSONResponse(
            {
                "task_id": result.task_id,
                "task_version": result.task_version,
                "row_version": result.row_version,
            },
            status_code=200 if result.replay else 202,
        )
        response.headers["Idempotent-Replay"] = (
            "true" if result.replay else "false"
        )
        return response

    @router.get("/api/invoices/{invoice_id}/sources/{source_id}/content")
    def get_invoice_source(invoice_id: str, source_id: str) -> Response:
        source, filename = load_source_binding(invoice_id, source_id)
        try:
            body = _source_store().get_exact(source)
        except SourcePersistenceError as exc:
            mark_source_unavailable(invoice_id, source_id)
            raise _http_error(503, str(exc)) from exc
        safe_name = filename.replace('"', "")
        return Response(
            body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/api/stream")
    def stream_events(
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        tenant_id = _tenant_id()

        def dal_factory():
            return DAL.connect(Tenant(tenant_id, "public-event-stream"))

        return StreamingResponse(
            sse_stream(dal_factory, last_event_id=last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # Gate 2 downstream projection — the one reconstruction contract for Gate 3/4.
    register_reconstruction_routes(router, tenant_id_getter=_tenant_id)
    # Gate 5 human approval + atomic seal.
    register_authority_seal_routes(
        router, tenant_id_getter=_tenant_id, require_auth=require_auth
    )

    return router


def validate_supported_pdf(body: bytes) -> None:
    started = time.monotonic()
    try:
        with pdfplumber.open(BytesIO(body)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ValueError("PDF_PAGE_LIMIT_EXCEEDED")
            for page in pdf.pages:
                page.extract_text()
                if time.monotonic() - started > MAX_PDF_PARSE_SECONDS:
                    raise ValueError("PDF_PARSE_TIMEOUT")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("ENCRYPTED_OR_UNREADABLE_PDF") from exc


def receive_invoice(
    *,
    body: bytes,
    envelope: PdfEnvelope,
    idempotency_key: str,
    demo_scenario: str,
    actor: AuthedActor,
) -> tuple[dict[str, Any], bool]:
    tenant_id = _tenant_id()
    request_hash = hashlib.sha256(
        body + b"\0" + demo_scenario.encode("utf-8")
    ).hexdigest()
    tenant = Tenant(tenant_id=tenant_id, actor=actor.display_name)
    with DAL.connect(tenant) as dal:
        reservation = reserve_ingestion(
            dal,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            actor_id=actor.user_id,
            actor_display=actor.display_name,
        )
        if reservation.state == "COMPLETED":
            if reservation.response_snapshot is None:
                raise IntakeUnavailableError("INGESTION_SNAPSHOT_UNAVAILABLE")
            return reservation.response_snapshot, True
        if reservation.state == "RESERVED" and not reservation.is_new:
            raise IngestionStateError("REQUEST_IN_PROGRESS")

        duplicate = find_invoice_by_sha(dal, envelope.sha256)
        if duplicate is not None:
            duplicate_invoice_id, duplicate_source_id = duplicate
            snapshot, _ = load_invoice_projection(duplicate_invoice_id)
            if snapshot["invoice"]["invoice_source"]["source_id"] != duplicate_source_id:
                raise IngestionStateError("DUPLICATE_SOURCE_BINDING_MISMATCH")
            snapshot = complete_duplicate_ingestion(
                dal,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                invoice_id=duplicate_invoice_id,
                source_id=duplicate_source_id,
                source_sha256=envelope.sha256,
                response_snapshot=snapshot,
            )
            return snapshot, True

        source = reservation.stored_source
        if source is None:
            source = _source_store().preserve(
                source_id=reservation.source_id,
                body=body,
            )
            record_source_stored(
                dal,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                source=source,
            )
        carrier_id = _load_demo_carrier_id(dal)
        snapshot = finalize_received_invoice(
            dal,
            FinalizeReceipt(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                carrier_id=carrier_id,
                display_name=envelope.display_filename,
                mime_type="application/pdf",
                stored_source=source,
                actor_id=actor.user_id,
                actor_display=actor.display_name,
                provenance_classification="SYNTHETIC_DEMO",
                public_disclosure="Fictional hackathon demonstration invoice.",
            ),
        )
    return snapshot, False


def load_invoice_projection(invoice_id: str) -> tuple[dict[str, Any], int]:
    with DAL.connect(Tenant(_tenant_id(), "public-intake-reader")) as dal:
        with dal.conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.display_name, i.status, i.status_sequence, i.received_at,
                       i.intake_state, i.aggregate_status, i.row_version,
                       i.active_claim_set_version,
                       s.id, s.display_filename, s.mime_type,
                       s.preservation_status
                FROM invoices i
                JOIN invoice_sources s
                  ON s.tenant_id=i.tenant_id AND s.invoice_id=i.id
                 AND s.source_type='INVOICE_PDF'
                WHERE i.tenant_id=%s AND i.id=%s;
                """,
                (dal.tenant.tenant_id, invoice_id),
            )
            row = cur.fetchone()
            if row is None:
                raise _http_error(404, "INVOICE_NOT_FOUND")
            cur.execute(
                """
                SELECT c.field_name, c.value_type, c.normalized_value,
                       c.amount_minor, c.currency, c.validation_state,
                       c.page_number, c.bounding_box,
                       text_excerpt
                FROM extracted_claims c
                JOIN claim_sets s
                  ON s.tenant_id=c.tenant_id AND s.id=c.claim_set_id
                WHERE c.tenant_id=%s AND s.invoice_id=%s
                  AND s.claim_set_version=%s
                ORDER BY field_name;
                """,
                (
                    dal.tenant.tenant_id,
                    invoice_id,
                    row[7] if row[7] is not None else -1,
                ),
            )
            claims = {
                claim[0]: {
                    "value_type": claim[1],
                    "value": _json_value(claim[2]),
                    "amount_minor": claim[3],
                    "currency": claim[4],
                    "validation_state": claim[5],
                    "anchor": {
                        "page_number": int(claim[6]),
                        "bounding_box": _json_value(claim[7]),
                        "text_excerpt": claim[8],
                    },
                }
                for claim in cur.fetchall()
            }
            cur.execute(
                """
                SELECT id, task_type, state, current_attempt, public_summary
                FROM workflow_tasks
                WHERE tenant_id=%s AND invoice_id=%s
                ORDER BY created_at, id;
                """,
                (dal.tenant.tenant_id, invoice_id),
            )
            tasks = [
                {
                    "task_id": str(task[0]),
                    "task_type": task[1],
                    "state": task[2],
                    "attempt": int(task[3]),
                    "summary": task[4],
                }
                for task in cur.fetchall()
            ]
    source_id = str(row[8])
    snapshot = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "invoice": {
            "invoice_id": invoice_id,
            "display_name": row[0],
            "status": row[1],
            "status_sequence": int(row[2]),
            "received_at": row[3].isoformat(),
            "intake_state": row[4],
            "aggregate_status": row[5],
            "active_claim_set_version": row[7],
            "invoice_source": {
                "source_id": source_id,
                "filename": row[9],
                "mime_type": row[10],
                "preservation_status": row[11],
            },
            "claims": claims,
            "tasks": tasks,
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
    return snapshot, int(row[6])


def load_source_binding(
    invoice_id: str, source_id: str
) -> tuple[StoredInvoiceSource, str]:
    with DAL.connect(Tenant(_tenant_id(), "public-source-reader")) as dal:
        with dal.conn.cursor() as cur:
            cur.execute(
                """
                SELECT s3_bucket_ref_private, s3_object_key_private,
                       s3_version_id_private, sha256, byte_length,
                       display_filename
                FROM invoice_sources
                WHERE tenant_id=%s AND invoice_id=%s AND id=%s;
                """,
                (dal.tenant.tenant_id, invoice_id, source_id),
            )
            row = cur.fetchone()
    if row is None:
        raise _http_error(404, "SOURCE_NOT_FOUND")
    return (
        StoredInvoiceSource(
            bucket_ref_private=row[0],
            object_key_private=row[1],
            version_id_private=row[2],
            sha256=row[3],
            byte_length=int(row[4]),
        ),
        row[5],
    )


def mark_source_unavailable(invoice_id: str, source_id: str) -> None:
    with DAL.connect(Tenant(_tenant_id(), "source-verifier")) as dal:
        with dal.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE invoice_sources
                SET preservation_status='VERSION_UNAVAILABLE'
                WHERE tenant_id=%s AND invoice_id=%s AND id=%s;
                """,
                (dal.tenant.tenant_id, invoice_id, source_id),
            )


def _load_demo_carrier_id(dal: DAL) -> str:
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM carriers
            WHERE tenant_id=%s AND scac='NOLU'
            LIMIT 1;
            """,
            (dal.tenant.tenant_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise IntakeUnavailableError("DEMO_CARRIER_NOT_CONFIGURED")
    return str(row[0])


def _source_store() -> VersionedInvoiceSourceStore:
    bucket = os.environ.get("TALLY_INTAKE_BUCKET", "").strip()
    if not bucket:
        raise IntakeUnavailableError("INTAKE_BUCKET_NOT_CONFIGURED")
    region = os.environ.get("AWS_REGION", "us-east-1")
    return VersionedInvoiceSourceStore(
        boto3.client("s3", region_name=region),
        bucket=bucket,
        key_prefix=os.environ.get("TALLY_INTAKE_KEY_PREFIX", "intake/invoice-sources"),
    )


def _tenant_id() -> str:
    tenant_id = os.environ.get("TALLY_TENANT_ID", "").strip()
    if not tenant_id:
        raise IntakeUnavailableError("INTAKE_TENANT_NOT_CONFIGURED")
    return tenant_id


def _http_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return str(value)


def _parse_invoice_etag(invoice_id: str, value: str | None) -> int:
    prefix = f'"invoice-{invoice_id}-v'
    if not value or not value.startswith(prefix) or not value.endswith('"'):
        raise _http_error(428, "IF_MATCH_REQUIRED")
    try:
        return int(value[len(prefix) : -1])
    except ValueError as exc:
        raise _http_error(400, "INVALID_IF_MATCH") from exc

"""Correspondence HTTP routes: draft from the sealed decision, then a second-
authorization gated controlled send, plus a public read of the latest draft +
send projection.

- ``POST /api/invoices/{invoice_id}/correspondence/draft`` (auth): draft the
  adjustment request from the invoice's latest sealed decision. 404 if unsealed.
- ``POST /api/invoices/{invoice_id}/correspondence/send`` (auth, Idempotency-Key
  required): run the fresh send gates and, if all pass, call the controlled
  provider once. 201 on first send, 200 on idempotent replay.
- ``GET /api/invoices/{invoice_id}/correspondence`` (public): latest draft +
  latest send-attempt projection (or null), for the UI's Sealed→Sending→Sent
  beat and the provider message id.

Mirrors the Gate-5 seal route style (Depends(require_auth), DAL.connect(Tenant)).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.external.controlled_mail import DemonstrationInboxProvider
from src.external.correspondence_bedrock import BedrockDraftGenerator
from src.external.dal import DAL, Tenant
from src.platform.auth import AuthedActor
from src.platform.correspondence_repository import (
    DraftNotFoundError,
    SendConflictError,
    approve_and_send,
    draft_from_sealed,
)
from src.platform.send_gates import build_fresh_gate_checks


def _latest_decision_seal_id(dal: DAL, *, invoice_id: str) -> str | None:
    with dal.conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM decision_seals WHERE tenant_id=%s AND invoice_id=%s "
            "ORDER BY revision DESC LIMIT 1;",
            (dal.tenant.tenant_id, invoice_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _latest_draft_id(dal: DAL, *, invoice_id: str) -> str | None:
    with dal.conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM correspondence_drafts WHERE tenant_id=%s AND invoice_id=%s "
            "ORDER BY created_at DESC LIMIT 1;",
            (dal.tenant.tenant_id, invoice_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def load_correspondence_projection(
    dal: DAL, *, invoice_id: str
) -> dict[str, Any] | None:
    """Latest draft joined to its latest send attempt (public-safe fields)."""
    tenant_id = dal.tenant.tenant_id
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject, body_prose, validation_state, decision_seal_id
            FROM correspondence_drafts
            WHERE tenant_id=%s AND invoice_id=%s ORDER BY created_at DESC LIMIT 1;
            """,
            (tenant_id, invoice_id),
        )
        draft = cur.fetchone()
        if draft is None:
            return None
        draft_id = str(draft[0])
        cur.execute(
            """
            SELECT send_state, gate_state, provider_message_id, blocked_reason
            FROM send_attempts
            WHERE tenant_id=%s AND draft_id=%s ORDER BY created_at DESC LIMIT 1;
            """,
            (tenant_id, draft_id),
        )
        send = cur.fetchone()
    return {
        "draft_id": draft_id,
        "subject": draft[1],
        "body_prose": draft[2],
        "validation_state": draft[3],
        "decision_seal_id": str(draft[4]),
        "send_state": send[0] if send else None,
        "gate_state": send[1] if send else None,
        "provider_message_id": send[2] if send else None,
        "blocked_reason": send[3] if send else None,
    }


def register_correspondence_routes(
    router: APIRouter, *, tenant_id_getter, require_auth
) -> None:
    @router.post("/api/invoices/{invoice_id}/correspondence/draft")
    def draft(
        invoice_id: str,
        actor: AuthedActor = Depends(require_auth),
    ) -> JSONResponse:
        with DAL.connect(Tenant(tenant_id_getter(), actor.display_name)) as dal:
            seal_id = _latest_decision_seal_id(dal, invoice_id=invoice_id)
            if seal_id is None:
                raise HTTPException(status_code=404, detail={"error": "NOT_SEALED"})
            # Bedrock writes the PROSE from the sealed fact pack; the locked
            # financial/identifier fields are always re-derived from the seal, and
            # the generated prose is fact-checked against it. If Bedrock is
            # unavailable, draft_from_sealed falls back to the deterministic
            # generator and records which writer ran.
            result = draft_from_sealed(
                dal,
                decision_seal_id=seal_id,
                draft_generator=BedrockDraftGenerator(),
            )
        return JSONResponse(
            {
                "draft_id": result.draft_id,
                "validation_state": result.validation_state,
                "seal_digest": result.seal_digest,
            },
            status_code=201,
        )

    @router.post("/api/invoices/{invoice_id}/correspondence/send")
    def send(
        invoice_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: AuthedActor = Depends(require_auth),
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(
                status_code=400, detail={"error": "IDEMPOTENCY_KEY_REQUIRED"})
        with DAL.connect(Tenant(tenant_id_getter(), actor.display_name)) as dal:
            draft_id = _latest_draft_id(dal, invoice_id=invoice_id)
            if draft_id is None:
                raise HTTPException(status_code=404, detail={"error": "DRAFT_NOT_FOUND"})
            seal_id = _latest_decision_seal_id(dal, invoice_id=invoice_id)
            gate_checks = build_fresh_gate_checks(
                dal, invoice_id=invoice_id, decision_seal_id=seal_id)
            try:
                # ponytail: DemonstrationInboxProvider is the controlled in-process
                # provider. Swap SesControlledMailProvider(...) here for real SES
                # send (needs the SES SSM params + ses:SendEmail IAM — deferred).
                result = approve_and_send(
                    dal, draft_id=draft_id, idempotency_key=idempotency_key,
                    second_approver_display=actor.display_name,
                    gate_checks=gate_checks, provider=DemonstrationInboxProvider(),
                )
            except SendConflictError as exc:
                raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
            except DraftNotFoundError as exc:
                raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        return JSONResponse(
            {
                "send_state": result.send_state,
                "provider_message_id": result.provider_message_id,
                "blocked_reason": result.blocked_reason,
            },
            status_code=200 if result.duplicate else 201,
        )

    @router.get("/api/invoices/{invoice_id}/correspondence")
    def get_correspondence(invoice_id: str) -> dict[str, Any] | None:
        with DAL.connect(Tenant(tenant_id_getter(), "public-correspondence-reader")) as dal:
            return load_correspondence_projection(dal, invoice_id=invoice_id)

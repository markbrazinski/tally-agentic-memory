"""Gate 5 approval + seal HTTP route and public seal projection.

`POST /api/invoices/{invoice_id}/recommendations/{version}/approve` binds a human
approval to one exact immutable recommendation version and atomically seals the
decision. The recommendation ETag (`If-Match`) supplies the expected
version+digest for optimistic concurrency; a stale ETag is rejected. Repeated
approval with the same `Idempotency-Key` replays the existing seal.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.external.dal import DAL, Tenant
from src.platform.auth import AuthedActor
from src.platform.authority_seal_repository import (
    ApprovalConflictError,
    RecommendationNotApprovableError,
    StaleRecommendationError,
    approve_and_seal,
)


def recommendation_etag(recommendation_id: str, version: int, digest: str) -> str:
    return f'"rec-{recommendation_id}-v{version}-{digest}"'


def _parse_etag(if_match: str | None) -> tuple[int, str]:
    if not if_match:
        raise HTTPException(status_code=428, detail={"error": "IF_MATCH_REQUIRED"})
    raw = if_match.strip().strip('"')
    # rec-<id>-v<version>-<digest>
    try:
        _, rest = raw.split("rec-", 1)
        _id, tail = rest.rsplit("-v", 1)
        version_str, digest = tail.split("-", 1)
        return int(version_str), digest
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=400, detail={"error": "BAD_ETAG"}) from exc


def load_seal_projection(dal: DAL, *, invoice_id: str) -> dict[str, Any] | None:
    """Public-safe seal projection: verification facts, no private source ref."""
    tenant_id = dal.tenant.tenant_id
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, recommendation_id, recommendation_version, revision,
                   seal_digest, approver_display, public_summary, sealed_at,
                   bound_object_refs
            FROM decision_seals
            WHERE tenant_id=%s AND invoice_id=%s ORDER BY revision DESC LIMIT 1;
            """,
            (tenant_id, invoice_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    import json as _json

    refs = row[8] if isinstance(row[8], list) else _json.loads(row[8])
    return {
        "seal_id": str(row[0]),
        "recommendation_id": str(row[1]),
        "recommendation_version": int(row[2]),
        "revision": int(row[3]),
        "seal_digest": row[4],
        "approver": row[5],
        "summary": row[6],
        "sealed_at_txn": str(row[7]),
        "bound_object_refs": refs,
        "immutable": True,
    }


def register_authority_seal_routes(
    router: APIRouter, *, tenant_id_getter, require_auth
) -> None:
    @router.post("/api/invoices/{invoice_id}/recommendations/{recommendation_id}/approve")
    def approve(
        invoice_id: str,
        recommendation_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
        actor: AuthedActor = Depends(require_auth),
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail={"error": "IDEMPOTENCY_KEY_REQUIRED"})
        expected_version, expected_digest = _parse_etag(if_match)
        try:
            with DAL.connect(Tenant(tenant_id_getter(), actor.display_name)) as dal:
                result = approve_and_seal(
                    dal, recommendation_id=recommendation_id,
                    expected_version=expected_version, expected_digest=expected_digest,
                    idempotency_key=idempotency_key, approver_user_id=actor.user_id,
                    approver_display=actor.display_name, approver_kind="SYNTHETIC_DEMO",
                )
        except StaleRecommendationError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except RecommendationNotApprovableError as exc:
            raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
        response = JSONResponse(
            {
                "approval_id": result.approval_id,
                "seal_id": result.seal_id,
                "revision": result.revision,
                "seal_digest": result.seal_digest,
                "recommendation_type": result.recommendation_type,
            },
            status_code=200 if result.already_sealed else 201,
        )
        response.headers["Idempotent-Replay"] = "true" if result.already_sealed else "false"
        return response

    @router.get("/api/invoices/{invoice_id}/decision")
    def get_seal(invoice_id: str) -> dict[str, Any]:
        with DAL.connect(Tenant(tenant_id_getter(), "public-seal-reader")) as dal:
            projection = load_seal_projection(dal, invoice_id=invoice_id)
        if projection is None:
            raise HTTPException(status_code=404, detail={"error": "NOT_SEALED"})
        return projection

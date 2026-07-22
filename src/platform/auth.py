"""Bearer auth for mutating routes (TDD §8: "demo grade" posture).

Read routes are public (gate 7: logged-out judge sees the identical
product). Mutating routes require Authorization: Bearer <token> - the
deployed demo carries one fixed scoped token bound to the fixed identity
rachel.martinez (TDD §8: "in demo mode the deploy carries a fixed demo
identity"). Every mutation still writes a real audit row regardless -
this is identity resolution for the audit trail, not real per-user auth.

Token resolution follows src/external/db.py's get_dsn() pattern exactly:
SSM SecureString first, env var fallback, so local dev works without AWS
credentials.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

from src.external.db import connect

SSM_DEMO_TOKEN_PARAMETER_NAME = "/example/tally/demo-token"
DEMO_ACTOR_EMAIL = "rachel.martinez@meridianhomeandhardware.example"


def get_demo_token() -> str:
    """SSM SecureString first, env var fallback - same resolution order
    as db.py's get_dsn(), for the same reason (local dev without AWS
    creds must still work)."""
    try:
        import boto3

        ssm = boto3.client("ssm")
        response = ssm.get_parameter(Name=SSM_DEMO_TOKEN_PARAMETER_NAME, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - any SSM failure falls back to the env var
        pass

    token = os.environ.get("TALLY_DEMO_TOKEN")
    if not token:
        raise RuntimeError(
            "No demo bearer token available: SSM parameter "
            f"{SSM_DEMO_TOKEN_PARAMETER_NAME} could not be read, and "
            "TALLY_DEMO_TOKEN is not set."
        )
    return token


class AuthedActor:
    """Resolved identity for an authenticated mutation: the real
    users.id row (for sealed_by, foreign-keyed), plus display fields for
    the audit-log line."""

    def __init__(self, user_id: str, display_name: str):
        self.user_id = user_id
        self.display_name = display_name


def _resolve_rachel_user_id(tenant_id: str) -> str:
    """Looks up rachel.martinez's real users.id row - sealed_by is a
    foreign key (users.id), never a literal string, per TDD §2.12."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE tenant_id=%s AND email=%s;",
                (tenant_id, DEMO_ACTOR_EMAIL),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError(
            f"Demo user {DEMO_ACTOR_EMAIL} not found for tenant {tenant_id} - "
            "run src.external.seed_demo_tenant first."
        )
    return str(row[0])


def make_require_bearer_auth(tenant_id: str):
    """Returns a FastAPI dependency bound to one fixed tenant_id (this
    session's demo scope: one tenant). FastAPI's Depends() calls the
    dependency with no way to pass extra static args directly, so routes
    use Depends(make_require_bearer_auth(DEMO_TENANT_ID)) - a small
    factory, not a design that implies real multi-tenant auth exists yet.
    """

    def require_bearer_auth(authorization: str | None = Header(default=None)) -> AuthedActor:
        """Missing/malformed/wrong token -> 401. A valid token resolves
        to rachel's real users.id."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="missing or malformed Authorization header"
            )

        presented_token = authorization.removeprefix("Bearer ").strip()
        expected_token = get_demo_token()
        # constant-time compare - a plain != leaks token bytes via timing
        if not hmac.compare_digest(presented_token, expected_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

        user_id = _resolve_rachel_user_id(tenant_id)
        return AuthedActor(user_id=user_id, display_name="rachel.martinez")

    return require_bearer_auth

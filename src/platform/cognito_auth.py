"""Cognito JWT authentication for the judge-demo environment.

The judge-demo App Runner service is protected by an Amazon Cognito User Pool
(username + password, self-registration disabled, one manually provisioned judge
account). Cognito issues a signed JWT on login; every protected route validates
that JWT here before serving anything — pages, /api reads, PDF bytes, the SSE
stream, and the import endpoint.

Validation (RS256, per the Cognito token-verification guidance):
- signature verified against the pool's live JWKS (PyJWKClient caches it),
- issuer == the pool's issuer URL,
- expiry / not-before enforced,
- token_use == "id" (id token) or "access" (access token) accepted,
- audience/client_id matches the configured app client.

Configuration comes from the environment (set by App Runner from SSM / runtime
vars); nothing secret is hard-coded. When Cognito is not configured (local dev),
the app falls back to the existing static-bearer auth so tests and local runs
keep working without AWS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

from src.platform.auth import AuthedActor, resolve_actor_for_email


@dataclass(frozen=True)
class CognitoConfig:
    region: str
    user_pool_id: str
    client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> "CognitoConfig | None":
        region = os.environ.get("TALLY_COGNITO_REGION") or os.environ.get("AWS_REGION")
        pool = os.environ.get("TALLY_COGNITO_USER_POOL_ID")
        client = os.environ.get("TALLY_COGNITO_CLIENT_ID")
        if not (region and pool and client):
            return None
        return cls(region=region, user_pool_id=pool, client_id=client)


class CognitoAuthError(HTTPException):
    def __init__(self, detail: str = "unauthorized"):
        super().__init__(status_code=401, detail={"error": detail})
        self.headers = {"WWW-Authenticate": "Bearer"}


# Module-level JWKS client, built once per config (it caches signing keys and
# refreshes on rotation).
_jwk_clients: dict[str, PyJWKClient] = {}


def _jwk_client(config: CognitoConfig) -> PyJWKClient:
    client = _jwk_clients.get(config.jwks_url)
    if client is None:
        client = PyJWKClient(config.jwks_url, cache_keys=True)
        _jwk_clients[config.jwks_url] = client
    return client


def verify_cognito_jwt(token: str, config: CognitoConfig) -> dict:
    """Verify a Cognito-issued JWT and return its claims, or raise 401."""
    try:
        signing_key = _jwk_client(config).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=config.issuer,
            # Id tokens carry `aud`; if we let PyJWT auto-verify it without an
            # expected audience it raises InvalidAudienceError on every valid id
            # token. Disable the built-in aud check and bind the client_id
            # ourselves below (covers both id `aud` and access `client_id`).
            options={"require": ["exp", "iss", "token_use"], "verify_aud": False},
            leeway=10,
        )
    except jwt.ExpiredSignatureError as exc:
        raise CognitoAuthError("token_expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise CognitoAuthError("invalid_issuer") from exc
    except jwt.PyJWTError as exc:
        raise CognitoAuthError("invalid_token") from exc

    token_use = claims.get("token_use")
    if token_use not in ("id", "access"):
        raise CognitoAuthError("invalid_token_use")
    # Bind the token to our app client: id tokens use `aud`, access tokens use
    # `client_id`. Either must equal the configured client.
    bound_client = claims.get("aud") or claims.get("client_id")
    if bound_client != config.client_id:
        raise CognitoAuthError("wrong_client")
    return claims


def _bearer_token(authorization: str | None, cookie_token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    # The login flow stores the token in an httpOnly cookie for page/SSE requests
    # that cannot set an Authorization header (e.g. EventSource, <img>, links).
    return cookie_token


def make_require_cognito_auth(tenant_id: str, config: CognitoConfig):
    """FastAPI dependency: require a valid Cognito JWT, resolve the audit actor.

    Accepts the token from either the Authorization: Bearer header or the
    `tally_session` httpOnly cookie (needed for SSE/PDF/page GETs).
    """

    from fastapi import Cookie

    def _require(
        authorization: str | None = Header(default=None),
        tally_session: str | None = Cookie(default=None),
    ) -> AuthedActor:
        token = _bearer_token(authorization, tally_session)
        if not token:
            raise CognitoAuthError("missing_token")
        claims = verify_cognito_jwt(token, config)
        # Map the Cognito identity to the demo audit user. The judge account is
        # the single provisioned user; audit rows attribute to the demo actor.
        email = claims.get("email") or claims.get("username") or claims.get("cognito:username")
        return resolve_actor_for_email(tenant_id, email)

    return _require

"""One authorized PKCE bootstrap that seals a renewable read-only token in SSM."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

import boto3
import httpx

from scripts.gate3_oauth import (
    READ_SCOPE,
    REDIRECT_URI,
    OAuthSetupError,
    _authorization_metadata_url,
    _https_endpoint,
    _mapping,
    _pkce_pair,
    _resource_metadata_url,
    _wait_for_callback,
)
from src.external.cockroach_mcp import MCP_ENDPOINT, PROTOCOL_VERSION
from src.external.oauth_tokens import (
    OAuthTokenError,
    SSMTokenStore,
    validated_token_bundle,
)

PARAMETER_NAME = "/tally/gate5/oauth-token-bundle"


@dataclass(frozen=True)
class BootstrapSummary:
    phase_status: str
    claim: str
    access_token_received: bool
    refresh_token_received: bool
    access_token_ttl_seconds: int | None
    granted_read_scope: bool
    granted_write_scope: bool
    token_stored_in_encrypted_ssm: bool
    error_code: str | None = None


def _string_list(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _revoke_tokens(client, endpoint: str | None, token, *, client_id: str) -> bool:
    """Best-effort RFC 7009 cleanup; never expose provider response details."""
    if not endpoint:
        return False
    attempted = False
    succeeded = True
    for name, hint in (("refresh_token", "refresh_token"), ("access_token", "access_token")):
        value = token.get(name)
        if isinstance(value, str) and value:
            attempted = True
            try:
                response = client.post(
                    endpoint,
                    data={"token": value, "token_type_hint": hint, "client_id": client_id},
                )
            except httpx.HTTPError:
                succeeded = False
            else:
                succeeded = succeeded and 200 <= response.status_code < 300
    return attempted and succeeded


def bootstrap(
    *,
    cluster_id: str,
    ssm_client=None,
    http_client: httpx.Client | None = None,
    callback=_wait_for_callback,
    timeout_seconds: int = 300,
) -> BootstrapSummary:
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        challenge = client.post(
            MCP_ENDPOINT,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "mcp-cluster-id": cluster_id,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "tally-gate5b-bootstrap", "version": "1.0"},
                },
            },
        )
        if challenge.status_code != 401:
            raise OAuthSetupError("oauth_challenge_failed")
        resource_metadata_url = _resource_metadata_url(
            challenge.headers.get("www-authenticate", "")
        )
        resource_metadata = _mapping(
            client.get(resource_metadata_url), stage="resource_metadata"
        )
        resource = _https_endpoint(resource_metadata.get("resource"), field="resource")
        if resource.rstrip("/") != MCP_ENDPOINT.rstrip("/"):
            raise OAuthSetupError("oauth_resource_mismatch")
        servers = _string_list(resource_metadata.get("authorization_servers"))
        if len(servers) != 1 or READ_SCOPE not in _string_list(
            resource_metadata.get("scopes_supported")
        ):
            raise OAuthSetupError("oauth_resource_metadata_invalid")
        metadata = _mapping(
            client.get(_authorization_metadata_url(servers[0])),
            stage="authorization_metadata",
        )
        issuer = _https_endpoint(metadata.get("issuer"), field="issuer")
        if issuer.rstrip("/") != servers[0].rstrip("/"):
            raise OAuthSetupError("oauth_issuer_mismatch")
        grants = _string_list(metadata.get("grant_types_supported"))
        scopes = _string_list(metadata.get("scopes_supported"))
        if "refresh_token" not in grants:
            return BootstrapSummary(
                "FAIL — NO REFRESH", "NOT-SUPPORTED", False, False, None, False, False, False,
                "refresh_grant_not_advertised",
            )
        if "S256" not in _string_list(metadata.get("code_challenge_methods_supported")):
            raise OAuthSetupError("oauth_pkce_s256_unavailable")
        if "none" not in _string_list(metadata.get("token_endpoint_auth_methods_supported")):
            raise OAuthSetupError("oauth_public_client_unsupported")
        authorization_endpoint = _https_endpoint(
            metadata.get("authorization_endpoint"), field="authorization_endpoint"
        )
        token_endpoint = _https_endpoint(
            metadata.get("token_endpoint"), field="token_endpoint"
        )
        registration_endpoint = _https_endpoint(
            metadata.get("registration_endpoint"), field="registration_endpoint"
        )
        revocation_value = metadata.get("revocation_endpoint")
        revocation_endpoint = (
            _https_endpoint(revocation_value, field="revocation_endpoint")
            if revocation_value
            else None
        )
        requested_scopes = [READ_SCOPE]
        if "offline_access" in scopes:
            requested_scopes.append("offline_access")
        registration = _mapping(
            client.post(
                registration_endpoint,
                json={
                    "client_name": "Tally Gate 5B unattended read-only runtime",
                    "redirect_uris": [REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            ),
            stage="client_registration",
        )
        client_id = registration.get("client_id")
        if (
            not isinstance(client_id, str)
            or not client_id
            or "client_secret" in registration
            or registration.get("grant_types") != ["authorization_code", "refresh_token"]
            or registration.get("response_types") != ["code"]
            or registration.get("token_endpoint_auth_method") != "none"
            or registration.get("redirect_uris") != [REDIRECT_URI]
        ):
            raise OAuthSetupError("oauth_client_registration_invalid")
        verifier, challenge_value = _pkce_pair()
        state = os.urandom(32).hex()
        from urllib.parse import urlencode

        authorization_url = authorization_endpoint + "?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": " ".join(requested_scopes),
                "state": state,
                "code_challenge": challenge_value,
                "code_challenge_method": "S256",
                "resource": resource,
            }
        )
        callback_values = callback(authorization_url, timeout_seconds=timeout_seconds)
        if callback_values.get("error"):
            raise OAuthSetupError("oauth_authorization_denied")
        if callback_values.get("state") != state or not callback_values.get("code"):
            raise OAuthSetupError("oauth_callback_invalid")
        token = _mapping(
            client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": callback_values["code"],
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": resource,
                },
            ),
            stage="token_exchange",
        )
        try:
            bundle = validated_token_bundle(
                token,
                requested_scopes=frozenset(requested_scopes),
                token_endpoint=token_endpoint,
                client_id=client_id,
                resource=resource,
            )
        except OAuthTokenError as exc:
            if str(exc) == "oauth_refresh_token_missing":
                _revoke_tokens(client, revocation_endpoint, token, client_id=client_id)
                returned_scopes = set(str(token.get("scope", "")).split())
                return BootstrapSummary(
                    "FAIL — NO REFRESH",
                    "OBSERVED-LIVE",
                    True,
                    False,
                    int(token.get("expires_in", 0)) or None,
                    READ_SCOPE in returned_scopes,
                    "mcp:write" in returned_scopes,
                    False,
                    "refresh_token_not_issued",
                )
            _revoke_tokens(client, revocation_endpoint, token, client_id=client_id)
            raise
        store_client = ssm_client or boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "gate5-deployer")
        ).client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        try:
            SSMTokenStore(store_client, parameter_name=PARAMETER_NAME).save(bundle)
        except OAuthTokenError:
            revoked = _revoke_tokens(
                client, revocation_endpoint, token, client_id=client_id
            )
            return BootstrapSummary(
                "FAIL — NO REFRESH",
                "OBSERVED-LIVE",
                True,
                True,
                bundle.expires_at - int(time.time()),
                True,
                False,
                False,
                "oauth_token_store_failed"
                if revoked
                else "oauth_token_store_failed_revocation_uncertain",
            )
        return BootstrapSummary(
            "AUTHORIZATION_COMPLETE_REFRESH_PROOF_PENDING",
            "OBSERVED-LIVE",
            True,
            True,
            bundle.expires_at - int(time.time()),
            True,
            False,
            True,
        )
    except OAuthSetupError as exc:
        human_codes = {
            "oauth_browser_open_failed",
            "oauth_callback_timeout",
            "oauth_authorization_denied",
            "oauth_callback_invalid",
        }
        if str(exc) in human_codes:
            return BootstrapSummary(
                "BLOCKED — HUMAN AUTHORIZATION",
                "NOT-DETERMINED",
                False,
                False,
                None,
                False,
                False,
                False,
                "owner_authorization_incomplete",
            )
        return BootstrapSummary(
            "FAIL — NO REFRESH", "NOT-DETERMINED", False, False, None, False, False, False,
            "oauth_bootstrap_failed",
        )
    except (OAuthTokenError, httpx.HTTPError, OSError):
        return BootstrapSummary(
            "FAIL — NO REFRESH", "NOT-DETERMINED", False, False, None, False, False, False,
            "oauth_bootstrap_failed",
        )
    finally:
        if owns_client:
            client.close()


def main() -> int:
    summary = bootstrap(cluster_id=os.environ.get("TALLY_MCP_CLUSTER_ID", ""))
    print(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))
    return 0 if summary.refresh_token_received and summary.token_stored_in_encrypted_ssm else 1


if __name__ == "__main__":
    raise SystemExit(main())

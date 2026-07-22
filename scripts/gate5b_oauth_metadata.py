"""Discover Gate 5B OAuth capabilities without credentials or browser interaction."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from scripts.gate3_oauth import (
    READ_SCOPE,
    OAuthSetupError,
    _authorization_metadata_url,
    _https_endpoint,
    _mapping,
    _resource_metadata_url,
)
from src.external.cockroach_mcp import MCP_ENDPOINT, PROTOCOL_VERSION
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-5b/oauth-metadata.private.json")


@dataclass(frozen=True)
class MetadataSummary:
    challenge_received: bool
    protected_resource_metadata_valid: bool
    authorization_metadata_valid: bool
    read_scope_advertised: bool
    authorization_code_supported: bool
    pkce_s256_supported: bool
    dynamic_registration_supported: bool
    public_client_supported: bool
    offline_access_advertised: bool
    refresh_grant_advertised: bool
    refresh_classification: str
    may_proceed_to_authorization: bool
    error_code: str | None = None


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _challenge_structure(value: str) -> dict[str, object]:
    scheme = value.split(maxsplit=1)[0].lower() if value.strip() else ""
    parameter_names = sorted(set(re.findall(r"([A-Za-z][A-Za-z0-9_-]*)\s*=", value)))
    return {"scheme": scheme, "parameter_names": parameter_names}


def discover_metadata(
    cluster_id: str,
    *,
    output_path: Path = DEFAULT_OUTPUT,
    http_client: httpx.Client | None = None,
) -> MetadataSummary:
    """Discover and privately record standards metadata; return no identifiers."""
    if not cluster_id.strip():
        return MetadataSummary(*([False] * 10), "NOT-DETERMINED", False, "cluster_id_missing")
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        response = client.post(
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
                    "clientInfo": {"name": "tally-gate5b-metadata", "version": "1.0"},
                },
            },
        )
        if response.status_code != 401:
            raise OAuthSetupError("oauth_challenge_failed")
        header = response.headers.get("www-authenticate", "")
        resource_metadata_url = _resource_metadata_url(header)
        resource = _mapping(client.get(resource_metadata_url), stage="resource_metadata")
        resource_value = _https_endpoint(resource.get("resource"), field="resource")
        if resource_value.rstrip("/") != MCP_ENDPOINT.rstrip("/"):
            raise OAuthSetupError("oauth_resource_mismatch")
        resource_scopes = _string_list(resource.get("scopes_supported"))
        servers = _string_list(resource.get("authorization_servers"))
        if READ_SCOPE not in resource_scopes:
            raise OAuthSetupError("oauth_read_scope_unavailable")
        if len(servers) != 1:
            raise OAuthSetupError("oauth_authorization_server_ambiguous")
        metadata_url = _authorization_metadata_url(servers[0])
        metadata = _mapping(client.get(metadata_url), stage="authorization_metadata")
        issuer = _https_endpoint(metadata.get("issuer"), field="issuer")
        if issuer.rstrip("/") != servers[0].rstrip("/"):
            raise OAuthSetupError("oauth_issuer_mismatch")
        authorization_endpoint = _https_endpoint(
            metadata.get("authorization_endpoint"), field="authorization_endpoint"
        )
        token_endpoint = _https_endpoint(
            metadata.get("token_endpoint"), field="token_endpoint"
        )
        registration_value = metadata.get("registration_endpoint")
        registration_endpoint = (
            _https_endpoint(registration_value, field="registration_endpoint")
            if registration_value
            else None
        )
        response_types = _string_list(metadata.get("response_types_supported"))
        grant_types_value = metadata.get("grant_types_supported")
        grant_types = _string_list(grant_types_value)
        challenge_methods = _string_list(metadata.get("code_challenge_methods_supported"))
        auth_methods = _string_list(
            metadata.get("token_endpoint_auth_methods_supported")
        )
        authorization_scopes = _string_list(metadata.get("scopes_supported"))

        authorization_code_supported = "code" in response_types and (
            "authorization_code" in grant_types or grant_types_value is None
        )
        refresh_advertised = "refresh_token" in grant_types
        refresh_classification = (
            "METADATA-DISCOVERED"
            if refresh_advertised
            else ("NOT-SUPPORTED" if isinstance(grant_types_value, list) else "NOT-DETERMINED")
        )
        summary = MetadataSummary(
            challenge_received=True,
            protected_resource_metadata_valid=True,
            authorization_metadata_valid=True,
            read_scope_advertised=True,
            authorization_code_supported=authorization_code_supported,
            pkce_s256_supported="S256" in challenge_methods,
            dynamic_registration_supported=registration_endpoint is not None,
            public_client_supported="none" in auth_methods,
            offline_access_advertised="offline_access" in authorization_scopes,
            refresh_grant_advertised=refresh_advertised,
            refresh_classification=refresh_classification,
            may_proceed_to_authorization=(
                authorization_code_supported
                and "S256" in challenge_methods
                and registration_endpoint is not None
                and "none" in auth_methods
                and refresh_classification == "METADATA-DISCOVERED"
            ),
        )
        write_private_json(
            output_path,
            {
                "classification": "PRIVATE GATE 5B OAUTH METADATA",
                "challenge": _challenge_structure(header),
                "protected_resource": resource,
                "authorization_server": {
                    "issuer": issuer,
                    "authorization_endpoint": authorization_endpoint,
                    "token_endpoint": token_endpoint,
                    "registration_endpoint": registration_endpoint,
                    "scopes_supported": authorization_scopes,
                    "response_types_supported": response_types,
                    "grant_types_supported": grant_types,
                    "code_challenge_methods_supported": challenge_methods,
                    "token_endpoint_auth_methods_supported": auth_methods,
                    "revocation_endpoint": metadata.get("revocation_endpoint"),
                },
                "summary": asdict(summary),
            },
        )
        return summary
    except (OAuthSetupError, httpx.HTTPError, OSError):
        return MetadataSummary(
            *([False] * 10), "NOT-DETERMINED", False, "metadata_discovery_failed"
        )
    finally:
        if owns_client:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = discover_metadata(
        os.environ.get("TALLY_MCP_CLUSTER_ID", ""), output_path=args.output
    )
    print(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))
    return 0 if summary.may_proceed_to_authorization else 1


if __name__ == "__main__":
    raise SystemExit(main())

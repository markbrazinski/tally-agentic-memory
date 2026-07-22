"""Acquire a bounded-lifetime ``mcp:read`` token for the Gate 3 demo runtime.

The helper follows the MCP protected-resource metadata challenge, registers a
public loopback client, uses Authorization Code + PKCE S256, and requests only
``mcp:read``.  Tokens are written to ignored mode-0600 private storage and are
never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from src.external.cockroach_mcp import MCP_ENDPOINT, PROTOCOL_VERSION
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-3/oauth-token.private.json")
REDIRECT_URI = "http://127.0.0.1:8765/callback"
READ_SCOPE = "mcp:read"
MAX_TOKEN_LIFETIME_SECONDS = 86_400


class OAuthSetupError(RuntimeError):
    """Public-safe OAuth failure; messages contain no token or private identifier."""


def _https_endpoint(value: Any, *, field: str) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OAuthSetupError(f"invalid_{field}")
    return parsed.geturl()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _resource_metadata_url(header: str) -> str:
    match = re.search(r'resource_metadata="([^"]+)"', header)
    if match is None:
        raise OAuthSetupError("oauth_resource_metadata_missing")
    return _https_endpoint(match.group(1), field="resource_metadata_url")


def _authorization_metadata_url(issuer: str) -> str:
    parsed = urlparse(_https_endpoint(issuer, field="authorization_server"))
    suffix = parsed.path.strip("/")
    well_known = "/.well-known/oauth-authorization-server"
    if suffix:
        well_known += f"/{suffix}"
    return parsed._replace(path=well_known, params="", query="", fragment="").geturl()


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        values = parse_qs(parsed.query)
        self.__class__.result = {key: entries[0] for key, entries in values.items() if entries}
        body = b"Gate 3 read-only authorization received. You may close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _wait_for_callback(authorization_url: str, *, timeout_seconds: int) -> dict[str, str]:
    _CallbackHandler.result = {}
    server = HTTPServer(("127.0.0.1", 8765), _CallbackHandler)
    server.timeout = timeout_seconds
    try:
        if not webbrowser.open(authorization_url, new=1, autoraise=True):
            raise OAuthSetupError("oauth_browser_open_failed")
        server.handle_request()
    finally:
        server.server_close()
    if not _CallbackHandler.result:
        raise OAuthSetupError("oauth_callback_timeout")
    result = dict(_CallbackHandler.result)
    _CallbackHandler.result = {}
    return result


def _mapping(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OAuthSetupError(f"oauth_{stage}_failed") from exc
    if not isinstance(value, Mapping):
        raise OAuthSetupError(f"oauth_{stage}_invalid")
    return dict(value)


def _validated_token(token: Mapping[str, Any]) -> tuple[str, int, set[str]]:
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthSetupError("oauth_access_token_missing")
    if str(token.get("token_type", "")).lower() != "bearer":
        raise OAuthSetupError("oauth_token_type_invalid")
    expires_in = token.get("expires_in")
    if isinstance(expires_in, bool):
        raise OAuthSetupError("oauth_token_lifetime_invalid")
    try:
        lifetime = int(expires_in)
    except (TypeError, ValueError) as exc:
        raise OAuthSetupError("oauth_token_lifetime_invalid") from exc
    if lifetime <= 0 or lifetime > MAX_TOKEN_LIFETIME_SECONDS:
        raise OAuthSetupError("oauth_token_lifetime_invalid")
    granted_scopes = set(str(token.get("scope", "")).split())
    if READ_SCOPE not in granted_scopes or "mcp:write" in granted_scopes:
        raise OAuthSetupError("oauth_token_not_read_only")
    return access_token, lifetime, granted_scopes


def acquire_read_token(
    *,
    cluster_id: str,
    output_path: Path,
    timeout_seconds: int = 240,
    http_client: httpx.Client | None = None,
) -> None:
    if not cluster_id.strip():
        raise OAuthSetupError("cluster_id_missing")
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
                    "clientInfo": {"name": "tally-gate3-oauth", "version": "1.0"},
                },
            },
        )
        if challenge.status_code != 401:
            raise OAuthSetupError("oauth_challenge_failed")
        metadata_url = _resource_metadata_url(challenge.headers.get("www-authenticate", ""))
        resource = _mapping(client.get(metadata_url), stage="resource_metadata")
        scopes = resource.get("scopes_supported")
        servers = resource.get("authorization_servers")
        if not isinstance(scopes, list) or READ_SCOPE not in scopes:
            raise OAuthSetupError("oauth_read_scope_unavailable")
        if not isinstance(servers, list) or len(servers) != 1:
            raise OAuthSetupError("oauth_authorization_server_ambiguous")

        metadata = _mapping(
            client.get(_authorization_metadata_url(str(servers[0]))),
            stage="authorization_metadata",
        )
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise OAuthSetupError("oauth_pkce_s256_unavailable")
        registration_endpoint = _https_endpoint(
            metadata.get("registration_endpoint"), field="registration_endpoint"
        )
        authorization_endpoint = _https_endpoint(
            metadata.get("authorization_endpoint"), field="authorization_endpoint"
        )
        token_endpoint = _https_endpoint(metadata.get("token_endpoint"), field="token_endpoint")
        registration = _mapping(
            client.post(
                registration_endpoint,
                json={
                    "client_name": "Tally Gate 3 read-only runtime",
                    "redirect_uris": [REDIRECT_URI],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            ),
            stage="client_registration",
        )
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthSetupError("oauth_client_registration_invalid")

        verifier, challenge_value = _pkce_pair()
        state = secrets.token_urlsafe(32)
        authorization_url = (
            authorization_endpoint
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "scope": READ_SCOPE,
                    "state": state,
                    "code_challenge": challenge_value,
                    "code_challenge_method": "S256",
                    "resource": MCP_ENDPOINT,
                }
            )
        )
        callback = _wait_for_callback(authorization_url, timeout_seconds=timeout_seconds)
        if callback.get("error"):
            raise OAuthSetupError("oauth_authorization_denied")
        if callback.get("state") != state or not callback.get("code"):
            raise OAuthSetupError("oauth_callback_invalid")

        token = _mapping(
            client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": callback["code"],
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": MCP_ENDPOINT,
                },
            ),
            stage="token_exchange",
        )
        access_token, lifetime, granted_scopes = _validated_token(token)
        write_private_json(
            output_path,
            {
                "classification": "private short-lived Gate 3 OAuth credential",
                "access_token": access_token,
                "token_type": "Bearer",
                "scope": sorted(granted_scopes),
                "expires_in": lifetime,
                "acquired_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
    except OAuthSetupError:
        raise
    except httpx.HTTPError as exc:
        raise OAuthSetupError("oauth_transport_failed") from exc
    except OSError as exc:
        raise OAuthSetupError("oauth_local_io_failed") from exc
    finally:
        if owns_client:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    try:
        acquire_read_token(
            cluster_id=os.environ.get("TALLY_MCP_CLUSTER_ID", ""),
            output_path=args.output,
            timeout_seconds=args.timeout_seconds,
        )
    except OAuthSetupError as exc:
        print(f"oauth_error={exc}")
        return 1
    print("oauth_read_only_acquired=true")
    print("token_output=private_ignored_mode_0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

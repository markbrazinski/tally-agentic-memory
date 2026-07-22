from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import scripts.gate3_oauth as gate3_oauth
from scripts.gate3_oauth import (
    OAuthSetupError,
    _authorization_metadata_url,
    _https_endpoint,
    _pkce_pair,
    _resource_metadata_url,
    _validated_token,
    acquire_read_token,
)
from src.external.cockroach_mcp import MCP_ENDPOINT


def test_pkce_verifier_and_s256_challenge_are_nonempty_and_distinct():
    verifier, challenge = _pkce_pair()
    assert len(verifier) >= 43
    assert len(challenge) == 43
    assert verifier != challenge


def test_resource_metadata_is_discovered_from_bearer_challenge():
    value = _resource_metadata_url(
        'Bearer realm="mcp", resource_metadata="https://example.test/oauth-resource"'
    )
    assert value == "https://example.test/oauth-resource"


def test_authorization_metadata_preserves_issuer_path_per_rfc_shape():
    assert _authorization_metadata_url("https://auth.example.test/issuer") == (
        "https://auth.example.test/.well-known/oauth-authorization-server/issuer"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://auth.example.test/token",
        "https://user:password@auth.example.test/token",
        "not-a-url",
    ],
)
def test_oauth_service_endpoints_must_be_https_without_userinfo(value):
    with pytest.raises(OAuthSetupError):
        _https_endpoint(value, field="test")


def test_challenge_without_metadata_fails_with_public_safe_error():
    with pytest.raises(OAuthSetupError, match="metadata_missing"):
        _resource_metadata_url('Bearer realm="mcp"')


def test_token_must_be_bounded_bearer_and_read_only():
    access_token, lifetime, scopes = _validated_token(
        {
            "access_token": "private-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp:read",
        }
    )
    assert access_token == "private-token"
    assert lifetime == 3600
    assert scopes == {"mcp:read"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_type": "mac"},
        {"expires_in": None},
        {"expires_in": 0},
        {"expires_in": 86_401},
        {"scope": "mcp:read mcp:write"},
    ],
)
def test_unsafe_token_response_is_rejected(overrides):
    token = {
        "access_token": "private-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp:read",
    }
    token.update(overrides)
    with pytest.raises(OAuthSetupError):
        _validated_token(token)


class _OAuthClient:
    def _response(self, status, *, url, headers=None, value=None):
        return httpx.Response(
            status,
            headers=headers,
            json=value,
            request=httpx.Request("GET", url),
        )

    def post(self, url, **_kwargs):
        if url == MCP_ENDPOINT:
            return self._response(
                401,
                url=url,
                headers={
                    "www-authenticate": (
                        'Bearer resource_metadata="https://mcp.example.test/resource"'
                    )
                },
                value={"error": "unauthorized"},
            )
        if url == "https://auth.example.test/register":
            return self._response(201, url=url, value={"client_id": "public-client"})
        if url == "https://auth.example.test/token":
            return self._response(
                200,
                url=url,
                value={
                    "access_token": "private-test-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp:read",
                },
            )
        raise AssertionError(f"unexpected POST URL: {url}")

    def get(self, url):
        if url == "https://mcp.example.test/resource":
            return self._response(
                200,
                url=url,
                value={
                    "scopes_supported": ["mcp:read", "mcp:write"],
                    "authorization_servers": ["https://auth.example.test"],
                },
            )
        if url == "https://auth.example.test/.well-known/oauth-authorization-server":
            return self._response(
                200,
                url=url,
                value={
                    "code_challenge_methods_supported": ["S256"],
                    "registration_endpoint": "https://auth.example.test/register",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                },
            )
        raise AssertionError(f"unexpected GET URL: {url}")


def test_complete_oauth_flow_writes_only_validated_private_token(monkeypatch):
    captured = {}

    def callback(authorization_url, *, timeout_seconds):
        query = parse_qs(urlparse(authorization_url).query)
        assert query["scope"] == ["mcp:read"]
        assert "mcp:write" not in authorization_url
        assert timeout_seconds == 30
        return {"state": query["state"][0], "code": "one-time-code"}

    def writer(path, value):
        captured["path"] = path
        captured["value"] = value

    monkeypatch.setattr(gate3_oauth, "_wait_for_callback", callback)
    monkeypatch.setattr(gate3_oauth, "write_private_json", writer)
    output = gate3_oauth.DEFAULT_OUTPUT

    acquire_read_token(
        cluster_id="synthetic-cluster-placeholder",
        output_path=output,
        timeout_seconds=30,
        http_client=_OAuthClient(),
    )

    assert captured["path"] == output
    assert captured["value"]["access_token"] == "private-test-token"
    assert captured["value"]["scope"] == ["mcp:read"]
    assert captured["value"]["expires_in"] == 3600

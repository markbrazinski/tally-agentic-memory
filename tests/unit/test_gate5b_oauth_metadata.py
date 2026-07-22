from __future__ import annotations

import httpx
import pytest

import scripts.gate5b_oauth_metadata as metadata_module
from scripts.gate5b_oauth_metadata import discover_metadata
from src.external.cockroach_mcp import MCP_ENDPOINT


@pytest.fixture(autouse=True)
def _no_private_file_writes(monkeypatch):
    monkeypatch.setattr(metadata_module, "write_private_json", lambda *_args, **_kwargs: None)


class MetadataClient:
    def __init__(self, *, grants=None, scopes=None):
        self.grants = grants
        self.scopes = scopes or ["mcp:read"]

    @staticmethod
    def _response(status, url, *, headers=None, value=None):
        return httpx.Response(
            status,
            headers=headers,
            json=value,
            request=httpx.Request("GET", url),
        )

    def post(self, url, **_kwargs):
        assert url == MCP_ENDPOINT
        return self._response(
            401,
            url,
            headers={
                "www-authenticate": (
                    'Bearer realm="mcp", resource_metadata='
                    '"https://mcp.example.test/resource"'
                )
            },
            value={"error": "unauthorized"},
        )

    def get(self, url):
        if url == "https://mcp.example.test/resource":
            return self._response(
                200,
                url,
                value={
                    "resource": MCP_ENDPOINT,
                    "scopes_supported": ["mcp:read", "mcp:write"],
                    "authorization_servers": ["https://auth.example.test"],
                },
            )
        value = {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "registration_endpoint": "https://auth.example.test/register",
            "scopes_supported": self.scopes,
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
        if self.grants is not None:
            value["grant_types_supported"] = self.grants
        return self._response(200, url, value=value)


def test_refresh_and_offline_access_are_metadata_discovered(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        metadata_module,
        "write_private_json",
        lambda path, value: captured.update(path=path, value=value),
    )
    summary = discover_metadata(
        "synthetic-cluster",
        http_client=MetadataClient(
            grants=["authorization_code", "refresh_token"],
            scopes=["mcp:read", "offline_access"],
        ),
    )
    assert summary.may_proceed_to_authorization is True
    assert summary.refresh_classification == "METADATA-DISCOVERED"
    assert summary.offline_access_advertised is True
    assert "resource_metadata" in captured["value"]["challenge"]["parameter_names"]


def test_explicit_grant_list_without_refresh_stops():
    summary = discover_metadata(
        "synthetic-cluster",
        http_client=MetadataClient(grants=["authorization_code"]),
    )
    assert summary.refresh_classification == "NOT-SUPPORTED"
    assert summary.may_proceed_to_authorization is False


def test_omitted_grant_metadata_is_not_sufficient_to_authorize_bootstrap():
    summary = discover_metadata(
        "synthetic-cluster",
        http_client=MetadataClient(grants=None),
    )
    assert summary.refresh_classification == "NOT-DETERMINED"
    assert summary.may_proceed_to_authorization is False


def test_identifier_never_appears_in_summary():
    identifier = "private-cluster-identifier"
    summary = discover_metadata(identifier, http_client=MetadataClient())
    assert identifier not in str(summary)

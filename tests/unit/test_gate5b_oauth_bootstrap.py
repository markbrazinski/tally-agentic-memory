from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME, bootstrap
from src.external.cockroach_mcp import MCP_ENDPOINT


class FakeSSM:
    def __init__(self):
        self.calls = []
        self.version = 0

    def put_parameter(self, **kwargs):
        self.calls.append(kwargs)
        self.version += 1
        self.value = kwargs["Value"]
        return {"Version": self.version}

    def get_parameter(self, **_kwargs):
        return {
            "Parameter": {
                "Value": self.value,
                "Type": "SecureString",
                "Version": self.version,
            }
        }


class BootstrapClient:
    def __init__(
        self, *, token=None, offline=False, registration=None, revocation_status=200
    ):
        self.token = token or {
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp:read",
        }
        self.offline = offline
        self.registration = registration
        self.revocation_status = revocation_status
        self.posts = []

    @staticmethod
    def response(status, url, *, headers=None, value=None):
        return httpx.Response(
            status,
            headers=headers,
            json=value,
            request=httpx.Request("GET", url),
        )

    def get(self, url):
        if url == "https://mcp.example.test/resource":
            return self.response(
                200,
                url,
                value={
                    "resource": MCP_ENDPOINT,
                    "scopes_supported": ["mcp:read", "mcp:write"],
                    "authorization_servers": ["https://cockroachlabs.cloud/mcp"],
                },
            )
        return self.response(
            200,
            url,
            value={
                "issuer": "https://cockroachlabs.cloud/mcp",
                "authorization_endpoint": "https://cockroachlabs.cloud/mcp/oauth/authorize",
                "token_endpoint": "https://cockroachlabs.cloud/mcp/oauth/token",
                "registration_endpoint": "https://cockroachlabs.cloud/mcp/oauth/register",
                "revocation_endpoint": "https://cockroachlabs.cloud/mcp/oauth/revoke",
                "scopes_supported": ["mcp:read"] + (["offline_access"] if self.offline else []),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            },
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url == MCP_ENDPOINT:
            return self.response(
                401,
                url,
                headers={
                    "www-authenticate": (
                        'Bearer resource_metadata="https://mcp.example.test/resource"'
                    )
                },
                value={"error": "unauthorized"},
            )
        if url == "https://cockroachlabs.cloud/mcp/oauth/register":
            return self.response(
                201,
                url,
                value=self.registration
                or {
                    "client_id": "private-client",
                    "redirect_uris": ["http://127.0.0.1:8765/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if url == "https://cockroachlabs.cloud/mcp/oauth/revoke":
            return self.response(self.revocation_status, url, value={})
        return self.response(200, url, value=self.token)


def callback_capture(captured):
    def callback(url, *, timeout_seconds):
        assert timeout_seconds == 12
        query = parse_qs(urlparse(url).query)
        captured.update(query)
        return {"state": query["state"][0], "code": "private-one-time-code"}

    return callback


def test_bootstrap_advertises_refresh_and_seals_token_without_offline_scope():
    client = BootstrapClient()
    ssm = FakeSSM()
    authorization = {}
    result = bootstrap(
        cluster_id="private-cluster",
        ssm_client=ssm,
        http_client=client,
        callback=callback_capture(authorization),
        timeout_seconds=12,
    )
    assert result.refresh_token_received is True
    assert result.granted_write_scope is False
    assert authorization["scope"] == ["mcp:read"]
    assert authorization["resource"] == [MCP_ENDPOINT]
    registration = next(kwargs["json"] for url, kwargs in client.posts if url.endswith("register"))
    assert registration["grant_types"] == ["authorization_code", "refresh_token"]
    exchange = next(kwargs["data"] for url, kwargs in client.posts if url.endswith("token"))
    assert exchange["resource"] == MCP_ENDPOINT
    assert ssm.calls[0]["Name"] == PARAMETER_NAME
    assert ssm.calls[0]["Type"] == "SecureString"


def test_offline_scope_is_requested_only_when_metadata_advertises_it():
    client = BootstrapClient(offline=True)
    authorization = {}
    result = bootstrap(
        cluster_id="private-cluster",
        ssm_client=FakeSSM(),
        http_client=client,
        callback=callback_capture(authorization),
        timeout_seconds=12,
    )
    assert result.refresh_token_received is True
    assert set(authorization["scope"][0].split()) == {"mcp:read", "offline_access"}


def test_missing_refresh_token_is_fail_no_refresh_and_not_stored():
    client = BootstrapClient(
        token={
            "access_token": "private-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp:read",
        }
    )
    ssm = FakeSSM()
    result = bootstrap(
        cluster_id="private-cluster",
        ssm_client=ssm,
        http_client=client,
        callback=callback_capture({}),
        timeout_seconds=12,
    )
    assert result.phase_status == "FAIL — NO REFRESH"
    assert result.error_code == "refresh_token_not_issued"
    assert ssm.calls == []


def test_write_scope_fails_closed_without_storage():
    client = BootstrapClient(
        token={
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp:read mcp:write",
        }
    )
    ssm = FakeSSM()
    result = bootstrap(
        cluster_id="private-cluster",
        ssm_client=ssm,
        http_client=client,
        callback=callback_capture({}),
        timeout_seconds=12,
    )
    assert result.phase_status == "FAIL — NO REFRESH"
    assert result.granted_write_scope is False
    assert ssm.calls == []
    revocations = [kwargs["data"] for url, kwargs in client.posts if url.endswith("revoke")]
    assert {item["token_type_hint"] for item in revocations} == {
        "access_token",
        "refresh_token",
    }


def test_dcr_response_must_confirm_public_refresh_registration():
    client = BootstrapClient(
        registration={
            "client_id": "private-client",
            "client_secret": "",
            "redirect_uris": ["http://127.0.0.1:8765/callback"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    )
    result = bootstrap(
        cluster_id="private-cluster",
        ssm_client=FakeSSM(),
        http_client=client,
        callback=callback_capture({}),
        timeout_seconds=12,
    )
    assert result.error_code == "oauth_bootstrap_failed"
    assert not any(url.endswith("token") for url, _kwargs in client.posts)


def test_ssm_failure_revokes_issued_tokens_or_reports_uncertain_cleanup():
    class FailedSSM(FakeSSM):
        def put_parameter(self, **_kwargs):
            raise RuntimeError("private store failure")

    revoked = bootstrap(
        cluster_id="private-cluster",
        ssm_client=FailedSSM(),
        http_client=BootstrapClient(),
        callback=callback_capture({}),
        timeout_seconds=12,
    )
    assert revoked.error_code == "oauth_token_store_failed"
    assert revoked.token_stored_in_encrypted_ssm is False

    uncertain = bootstrap(
        cluster_id="private-cluster",
        ssm_client=FailedSSM(),
        http_client=BootstrapClient(revocation_status=503),
        callback=callback_capture({}),
        timeout_seconds=12,
    )
    assert uncertain.error_code == "oauth_token_store_failed_revocation_uncertain"
    assert "private store failure" not in str(uncertain)

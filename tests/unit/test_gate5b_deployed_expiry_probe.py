from __future__ import annotations

from dataclasses import replace

from scripts.gate5b_deployed_expiry_probe import run_probe
from src.external.oauth_tokens import TokenBundle


def bundle(*, access="access-one", refresh="refresh-one", expires_at=4_600):
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        token_type="Bearer",
        scopes=frozenset({"mcp:read"}),
        expires_at=expires_at,
        token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
        client_id="synthetic-client",
        resource="https://cockroachlabs.cloud/mcp",
    )


class Store:
    def __init__(self):
        self.value = bundle()

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


class Client:
    def __init__(self, store, *, status=200):
        self.store = store
        self.status = status

    def get(self, _url):
        if self.status == 200:
            self.store.value = replace(
                self.store.value,
                access_token="access-two",
                refresh_token="refresh-two",
                expires_at=4_600,
            )
        return type(
            "Response",
            (),
            {
                "status_code": self.status,
                "json": lambda _self: {
                    "status": "executed",
                    "mock_fallback": False,
                    "managed_mcp": {"status": "verified_read"},
                    "replay": {"receipt": {"exact_versioned_s3_verified": True}},
                },
            },
        )()


def test_deployed_probe_requires_hero_receipt_rotation_and_safe_ttl():
    store = Store()
    result = run_probe(
        store=store,
        hero_url="https://demo.example/public/demo/hero",
        http_client=Client(store),
        clock=lambda: 1_000,
    )
    assert result.passed is True
    assert result.bundle_rotated is True


def test_deployed_probe_fails_when_public_request_does_not_refresh():
    store = Store()
    result = run_probe(
        store=store,
        hero_url="https://demo.example/public/demo/hero",
        http_client=Client(store, status=503),
        clock=lambda: 1_000,
    )
    assert result.passed is False
    assert result.hero_executed is False

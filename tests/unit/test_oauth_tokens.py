from __future__ import annotations

import json

import pytest

from src.external.oauth_tokens import (
    DynamoDBRefreshLease,
    OAuthTokenError,
    SSMTokenStore,
    validate_stored_bundle,
    validated_token_bundle,
)


def token_response(**overrides):
    value = {
        "access_token": "private-access-one",
        "refresh_token": "private-refresh-one",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp:read",
    }
    value.update(overrides)
    return value


def validate(value=None, **kwargs):
    return validated_token_bundle(
        value or token_response(),
        requested_scopes=frozenset({"mcp:read"}),
        token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
        client_id="private-client",
        resource="https://cockroachlabs.cloud/mcp",
        now=1_000,
        **kwargs,
    )


def test_initial_refresh_token_is_required_and_repr_redacts_secrets():
    bundle = validate()
    assert bundle.expires_at == 4_600
    rendered = repr(bundle)
    assert "private-access" not in rendered
    assert "private-refresh" not in rendered
    assert "private-client" not in rendered
    assert "auth.example" not in rendered

    with pytest.raises(OAuthTokenError, match="refresh_token_missing"):
        validate(token_response(refresh_token=None))


def test_refresh_rotation_replaces_token_and_omission_retains_prior():
    rotated = validate(
        token_response(refresh_token="private-refresh-two"),
        prior_refresh_token="private-refresh-one",
    )
    assert rotated.refresh_token == "private-refresh-two"
    retained = validate(
        token_response(refresh_token=None),
        prior_refresh_token="private-refresh-one",
    )
    assert retained.refresh_token == "private-refresh-one"


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "mcp:read mcp:write"},
        {"scope": "mcp:read unrelated"},
        {"token_type": "mac"},
        {"expires_in": 0},
        {"refresh_token": ""},
    ],
)
def test_unsafe_token_response_fails_closed(overrides):
    with pytest.raises(OAuthTokenError):
        validate(token_response(**overrides))


class FakeSSM:
    def __init__(self):
        self.value = None
        self.version = 0

    def put_parameter(self, **kwargs):
        assert kwargs["Type"] == "SecureString"
        assert kwargs["Overwrite"] is True
        self.value = kwargs["Value"]
        self.version += 1
        return {"Version": self.version}

    def get_parameter(self, **kwargs):
        assert kwargs["WithDecryption"] is True
        return {
            "Parameter": {
                "Value": self.value,
                "Type": "SecureString",
                "Version": self.version,
            }
        }


def test_ssm_store_round_trip_is_secure_string_and_validated():
    client = FakeSSM()
    store = SSMTokenStore(client, parameter_name="/test/private")
    bundle = validate()
    store.save(bundle)
    assert json.loads(client.value)["refresh_token"] == "private-refresh-one"
    assert store.load() == bundle


def test_invalid_ssm_payload_is_public_safe_failure():
    client = FakeSSM()
    client.value = "not-json"
    store = SSMTokenStore(client, parameter_name="/test/private")
    with pytest.raises(OAuthTokenError, match="bundle_invalid"):
        store.load()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", None),
        ("refresh_token", {"unexpected": "object"}),
        ("token_type", "mac"),
        ("scopes", ["mcp:read", "mcp:write"]),
        ("token_endpoint", "https://attacker.invalid/token"),
        ("resource", "https://attacker.invalid/mcp"),
    ],
)
def test_tampered_persisted_bundle_is_rejected(field, value):
    client = FakeSSM()
    original = json.loads(validate().to_json())
    original[field] = value
    client.value = json.dumps(original)
    client.version = 1
    with pytest.raises(OAuthTokenError, match="bundle_invalid"):
        SSMTokenStore(client, parameter_name="/test/private").load()


def test_non_string_scope_response_is_rejected_not_inferred():
    with pytest.raises(OAuthTokenError, match="scope_invalid"):
        validate(token_response(scope=["mcp:read"]))


def test_stored_bundle_rejects_any_scope_beyond_exact_read():
    payload = token_response(scope="mcp:read offline_access")
    bundle = validated_token_bundle(
        payload,
        requested_scopes=frozenset({"mcp:read", "offline_access"}),
        token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
        client_id="synthetic-public-client",
        resource="https://cockroachlabs.cloud/mcp",
        now=1_000,
    )
    with pytest.raises(OAuthTokenError, match="bundle_invalid"):
        validate_stored_bundle(bundle, now=1_000)


class FakeDynamoDB:
    def __init__(self):
        self.item = None
        self.put_calls = []
        self.delete_calls = []

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)
        now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
        if self.item and int(self.item["expires_at"]["N"]) > now:
            raise RuntimeError("conditional_check_failed")
        self.item = kwargs["Item"]

    def delete_item(self, **kwargs):
        self.delete_calls.append(kwargs)
        owner = kwargs["ExpressionAttributeValues"][":owner"]["S"]
        if not self.item or self.item["owner"]["S"] != owner:
            raise RuntimeError("conditional_check_failed")
        self.item = None


def test_dynamodb_refresh_lease_is_conditional_keyed_and_owner_safe():
    client = FakeDynamoDB()
    lease = DynamoDBRefreshLease(
        client,
        table_name="oauth-refresh-leases",
        bundle_key="/test/private",
        lease_seconds=30,
        clock=lambda: 1_000,
        owner_factory=lambda: "owner-one",
    )
    assert lease.acquire() == "owner-one"
    assert lease.acquire() is None
    assert client.put_calls[0]["ConditionExpression"] == (
        "attribute_not_exists(#bundle_key) OR #expires_at <= :now"
    )
    assert client.item == {
        "bundle_key": {"S": "/test/private"},
        "owner": {"S": "owner-one"},
        "expires_at": {"N": "1030"},
    }
    assert lease.release("another-owner") is False
    assert client.item is not None
    assert lease.release("owner-one") is True
    assert client.item is None


def test_dynamodb_refresh_lease_can_take_expired_lease_and_fails_closed_on_error():
    client = FakeDynamoDB()
    client.item = {
        "bundle_key": {"S": "/test/private"},
        "owner": {"S": "stale-owner"},
        "expires_at": {"N": "999"},
    }
    lease = DynamoDBRefreshLease(
        client,
        table_name="oauth-refresh-leases",
        bundle_key="/test/private",
        clock=lambda: 1_000,
        owner_factory=lambda: "owner-two",
    )
    assert lease.acquire() == "owner-two"

    class BrokenDynamoDB:
        def put_item(self, **_kwargs):
            raise RuntimeError("unavailable")

    unavailable = DynamoDBRefreshLease(
        BrokenDynamoDB(),
        table_name="oauth-refresh-leases",
        bundle_key="/test/private",
    )
    assert unavailable.acquire() is None

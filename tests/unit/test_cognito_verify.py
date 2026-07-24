"""Real (unmocked) verify_cognito_jwt tests.

These sign tokens with a locally-generated RSA key and monkeypatch only the JWKS
lookup — the JWT decode/claim-validation path runs for real. This catches bugs
the mocked judge_auth tests can't: notably that PyJWT auto-verifies an id
token's `aud` claim unless verify_aud is disabled, which once 401'd every valid
judge login token at the middleware.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.platform.cognito_auth import (
    CognitoAuthError,
    CognitoConfig,
    verify_cognito_jwt,
)

CLIENT_ID = "45flk5tc649ujf2tdf6epvfof9"
POOL = "us-east-1_TESTPOOL"
REGION = "us-east-1"


@pytest.fixture()
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def config():
    return CognitoConfig(region=REGION, user_pool_id=POOL, client_id=CLIENT_ID)


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, rsa_key):
    # Return our local public key for any token, so the signature check passes
    # against the key we sign with. Everything else in verify runs for real.
    class _Key:
        key = rsa_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    import src.platform.cognito_auth as cog

    monkeypatch.setattr(cog, "_jwk_client", lambda config: _Client())


def _make_token(rsa_key, config, **overrides):
    now = int(time.time())
    claims = {
        "iss": config.issuer,
        "aud": config.client_id,  # id tokens carry aud
        "token_use": "id",
        "email": "judge@tally-demo.example",
        "exp": now + 3600,
        "iat": now,
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256")


def test_valid_id_token_verifies(rsa_key, config):
    # The regression: a real id token with an aud claim must verify. Before the
    # verify_aud=False fix this raised InvalidAudienceError -> 401.
    token = _make_token(rsa_key, config)
    claims = verify_cognito_jwt(token, config)
    assert claims["token_use"] == "id"
    assert claims["email"] == "judge@tally-demo.example"


def test_access_token_verifies_via_client_id(rsa_key, config):
    # Access tokens have no aud; they bind via client_id.
    token = _make_token(rsa_key, config, token_use="access", aud=None, client_id=CLIENT_ID)
    claims = verify_cognito_jwt(token, config)
    assert claims["token_use"] == "access"


def test_wrong_client_rejected(rsa_key, config):
    token = _make_token(rsa_key, config, aud="some-other-client")
    with pytest.raises(CognitoAuthError):
        verify_cognito_jwt(token, config)


def test_expired_token_rejected(rsa_key, config):
    token = _make_token(rsa_key, config, exp=int(time.time()) - 100)
    with pytest.raises(CognitoAuthError):
        verify_cognito_jwt(token, config)


def test_wrong_issuer_rejected(rsa_key, config):
    token = _make_token(rsa_key, config, iss="https://evil.example/pool")
    with pytest.raises(CognitoAuthError):
        verify_cognito_jwt(token, config)

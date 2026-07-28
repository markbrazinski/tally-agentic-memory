"""OAuth refresh canary handler tests. Zero network — the manager is faked.

Proves the handler forces one refresh and returns a public-safe summary (rotation
flag + access-token expiry only, never token material), and fails loud on a
refresh error so a bad fire surfaces rather than silently drifting to expiry.
"""

from __future__ import annotations

import pytest

from src.external.oauth_tokens import OAuthTokenError, TokenBundle
from src.platform import oauth_refresh_canary as canary

_BUNDLE = TokenBundle(
    access_token="new-access", refresh_token="new-refresh", token_type="Bearer",
    scopes=("mcp:read",), expires_at=1_800_000_000,
    token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
    client_id="client-1", resource="crn:...",
)


class _FakeManager:
    def __init__(self, *, rotated=True, raise_exc=None):
        self._rotated = rotated
        self._raise = raise_exc
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1
        if self._raise:
            raise self._raise
        return _BUNDLE, self._rotated


def test_handler_refreshes_and_returns_public_safe_summary(monkeypatch):
    fake = _FakeManager(rotated=True)
    monkeypatch.setattr(canary, "_build_manager", lambda: fake)
    out = canary.handler({}, None)
    assert fake.refresh_calls == 1
    assert out == {
        "ok": True, "rotated": True,
        "access_token_expires_at": 1_800_000_000,
    }
    # never leak token material
    assert "new-access" not in str(out) and "new-refresh" not in str(out)


def test_handler_reports_no_rotation(monkeypatch):
    fake = _FakeManager(rotated=False)
    monkeypatch.setattr(canary, "_build_manager", lambda: fake)
    out = canary.handler()
    assert out["rotated"] is False


def test_handler_fails_loud_on_refresh_error(monkeypatch):
    fake = _FakeManager(raise_exc=OAuthTokenError("oauth_refresh_failed"))
    monkeypatch.setattr(canary, "_build_manager", lambda: fake)
    with pytest.raises(OAuthTokenError, match="oauth_refresh_failed"):
        canary.handler({}, None)

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from src.external.oauth_tokens import (
    OAuthTokenError,
    OAuthTokenManager,
    TokenBundle,
)


class MemoryStore:
    def __init__(self, bundle, *, fail_save=False):
        self.bundle = bundle
        self.fail_save = fail_save
        self.saved = []

    def load(self):
        return self.bundle

    def save(self, bundle):
        if self.fail_save:
            raise OAuthTokenError("oauth_token_store_write_failed")
        self.bundle = bundle
        self.saved.append(bundle)


class Lease:
    def __init__(self, owner="lease-owner"):
        self.owner = owner
        self.acquired = 0
        self.released = []

    def acquire(self):
        self.acquired += 1
        return self.owner

    def release(self, owner):
        self.released.append(owner)
        return True


def bundle(*, access="access-one", refresh="refresh-one", expires_at=4_600):
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        token_type="Bearer",
        scopes=frozenset({"mcp:read"}),
        expires_at=expires_at,
        token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
        client_id="private-client",
        resource="https://cockroachlabs.cloud/mcp",
    )


class RefreshTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=next(self.responses), request=request)


def response(access, *, refresh=None):
    value = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp:read",
    }
    if refresh is not None:
        value["refresh_token"] = refresh
    return value


def test_valid_cached_access_token_does_not_refresh():
    store = MemoryStore(bundle())
    transport = RefreshTransport([])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    token, refreshed = manager.access_token()
    assert token == "access-one"
    assert refreshed is False
    assert transport.requests == []


def test_locally_expired_access_refreshes_before_return():
    store = MemoryStore(bundle(expires_at=1_100))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    token, refreshed = manager.access_token()
    assert (token, refreshed) == ("access-two", True)
    assert store.bundle.refresh_token == "refresh-two"


def test_simulated_decision_clock_does_not_timestamp_real_token_in_the_future():
    store = MemoryStore(bundle(expires_at=1_100))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 9_999,
        token_clock=lambda: 1_000,
    )
    _token, refreshed = manager.access_token()
    assert refreshed is True
    assert store.bundle.expires_at == 4_600


def test_two_concurrent_expiry_checks_share_one_refresh():
    store = MemoryStore(bundle(expires_at=1_100))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: manager.access_token(), range(2)))
    assert sorted(results) == [("access-two", False), ("access-two", True)]
    assert len(transport.requests) == 1


def test_cross_process_lease_reloads_and_skips_refresh_when_peer_already_refreshed():
    previous = bundle(expires_at=1_100)
    replacement = bundle(access="access-two", refresh="refresh-two", expires_at=4_600)

    class ReloadingStore(MemoryStore):
        def __init__(self):
            super().__init__(previous)
            self.loads = 0

        def load(self):
            self.loads += 1
            return previous if self.loads == 1 else replacement

    store = ReloadingStore()
    lease = Lease()
    transport = RefreshTransport([])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        refresh_lease=lease,
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    assert manager.access_token() == ("access-two", False)
    assert lease.acquired == 1
    assert lease.released == ["lease-owner"]
    assert transport.requests == []


def test_unavailable_cross_process_lease_fails_closed_for_expired_token():
    store = MemoryStore(bundle(expires_at=1_100))
    lease = Lease(owner=None)
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(RefreshTransport([]))),
        refresh_lease=lease,
        clock=lambda: 1_000,
    )
    with pytest.raises(OAuthTokenError, match="lease_unavailable"):
        manager.access_token()
    assert lease.released == []


def test_401_lease_contention_reuses_bundle_rotated_by_peer():
    previous = bundle(access="access-one", refresh="refresh-one")
    replacement = bundle(access="access-two", refresh="refresh-two")

    class ReloadingStore(MemoryStore):
        def __init__(self):
            super().__init__(previous)
            self.loads = 0

        def load(self):
            self.loads += 1
            return previous if self.loads == 1 else replacement

    manager = OAuthTokenManager(
        ReloadingStore(),
        http_client=httpx.Client(transport=httpx.MockTransport(RefreshTransport([]))),
        refresh_lease=Lease(owner=None),
    )
    current, did_refresh = manager.refresh_after_unauthorized("access-one")
    assert current == replacement
    assert did_refresh is False


def test_cross_process_lease_is_released_when_refresh_fails():
    store = MemoryStore(bundle(expires_at=1_100))
    lease = Lease()

    def denied(request):
        return httpx.Response(400, json={"error": "invalid_grant"}, request=request)

    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(denied)),
        refresh_lease=lease,
        clock=lambda: 1_000,
    )
    with pytest.raises(OAuthTokenError, match="refresh_failed"):
        manager.access_token()
    assert lease.released == ["lease-owner"]


def test_exact_refresh_margin_triggers_refresh():
    store = MemoryStore(bundle(expires_at=1_300))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        refresh_margin_seconds=300,
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    assert manager.access_token() == ("access-two", True)


def test_401_retry_reuses_token_already_refreshed_by_another_caller():
    store = MemoryStore(bundle(access="access-two", refresh="refresh-two"))
    transport = RefreshTransport([])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    current, refreshed = manager.refresh_after_unauthorized("access-one")
    assert current.access_token == "access-two"
    assert refreshed is False
    assert transport.requests == []


def test_401_retry_refreshes_exactly_once_for_current_rejected_token():
    store = MemoryStore(bundle(access="access-one", refresh="refresh-one"))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    current, refreshed = manager.refresh_after_unauthorized("access-one")
    assert current.access_token == "access-two"
    assert refreshed is True
    assert len(transport.requests) == 1


def test_two_refreshes_use_rotated_token_then_retain_when_not_rotated():
    store = MemoryStore(bundle())
    transport = RefreshTransport(
        [
            response("access-two", refresh="refresh-two"),
            response("access-three"),
        ]
    )
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    first, first_rotated = manager.refresh()
    second, second_rotated = manager.refresh()
    assert first_rotated is True
    assert second_rotated is False
    assert second.refresh_token == "refresh-two"
    bodies = [request.content.decode() for request in transport.requests]
    assert "refresh_token=refresh-one" in bodies[0]
    assert "refresh_token=refresh-two" in bodies[1]
    assert all("resource=" in body for body in bodies)


def test_process_restart_uses_only_the_current_persisted_bundle():
    store = MemoryStore(bundle(expires_at=1_100))
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    first_process = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    assert first_process.access_token() == ("access-two", True)

    restarted_process = OAuthTokenManager(
        store,
        http_client=httpx.Client(
            transport=httpx.MockTransport(RefreshTransport([]))
        ),
        clock=lambda: 1_000,
        token_clock=lambda: 1_000,
    )
    assert restarted_process.access_token() == ("access-two", False)


def test_rotation_with_ssm_write_failure_is_uncertain_and_fails_closed():
    store = MemoryStore(bundle(), fail_save=True)
    transport = RefreshTransport([response("access-two", refresh="refresh-two")])
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(OAuthTokenError, match="persistence_uncertain"):
        manager.refresh()


def test_invalid_grant_or_scope_expansion_fails_closed_without_save():
    def denied(request):
        return httpx.Response(400, json={"error": "invalid_grant"}, request=request)

    store = MemoryStore(bundle())
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(denied)),
    )
    with pytest.raises(OAuthTokenError, match="refresh_failed"):
        manager.refresh()
    assert store.saved == []

    transport = RefreshTransport(
        [
            {
                "access_token": "access-two",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "mcp:read mcp:write",
            }
        ]
    )
    manager = OAuthTokenManager(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(OAuthTokenError, match="scope_invalid"):
        manager.refresh()
    assert store.saved == []

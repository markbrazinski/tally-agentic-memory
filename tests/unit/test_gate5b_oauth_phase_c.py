from __future__ import annotations

from scripts.gate5b_oauth_phase_c import AccessCheck, run_phase_c
from src.external.oauth_tokens import TokenBundle


def bundle(*, access, refresh, expires_at):
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


class Store:
    def __init__(self):
        self.value = bundle(access="initial", refresh="refresh-one", expires_at=3_600)

    def load(self):
        return self.value


class Manager:
    calls = 0

    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def refresh(self):
        Manager.calls += 1
        self.store.value = bundle(
            access=f"access-{Manager.calls}",
            refresh=f"refresh-{Manager.calls + 1}",
            expires_at=7_200 + Manager.calls,
        )
        return self.store.value, True

    def access_token(self):
        assert self.clock is not None
        refreshed, _ = self.refresh()
        return refreshed.access_token, True


def test_immediate_phase_requires_two_refreshes_and_all_access_checks(tmp_path, monkeypatch):
    Manager.calls = 0
    monkeypatch.setattr(
        "scripts.gate5b_oauth_phase_c.write_private_json", lambda *_args, **_kwargs: None
    )
    seen_access = []

    def check_access(current, **_kwargs):
        seen_access.append(current.access_token)
        return AccessCheck(True, True, True)

    summary = run_phase_c(
        store=Store(),
        cluster_id="cluster",
        database="database",
        tenant_id="tenant",
        case_id="case",
        contest_id="contest",
        output_path=tmp_path / "private.json",
        manager_factory=Manager,
        check_access=check_access,
    )
    assert Manager.calls == 2
    assert seen_access == ["initial", "access-1", "access-2"]
    assert summary.refresh_grants_succeeded == 2
    assert summary.rotation_observed is True
    assert summary.simulated_expiry_triggered_refresh is True
    assert summary.passed_immediate_phase is True


def test_any_missing_read_or_denial_prevents_pass(tmp_path, monkeypatch):
    Manager.calls = 0
    monkeypatch.setattr(
        "scripts.gate5b_oauth_phase_c.write_private_json", lambda *_args, **_kwargs: None
    )
    checks = iter(
        [
            AccessCheck(True, True, True),
            AccessCheck(False, False, True),
            AccessCheck(True, True, True),
        ]
    )
    summary = run_phase_c(
        store=Store(),
        cluster_id="cluster",
        database="database",
        tenant_id="tenant",
        case_id="case",
        contest_id="contest",
        output_path=tmp_path / "private.json",
        manager_factory=Manager,
        check_access=lambda *_args, **_kwargs: next(checks),
    )
    assert summary.passed_immediate_phase is False

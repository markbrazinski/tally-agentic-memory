from __future__ import annotations

from scripts.gate5b_oauth_phase_c import AccessCheck
from scripts.gate5b_oauth_soak import run_soak
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
        self.value = bundle(access="one", refresh="refresh-one", expires_at=3_600)

    def load(self):
        return self.value


class Manager:
    def __init__(self, store, **_kwargs):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def access_token(self):
        self.store.value = bundle(
            access="two", refresh="refresh-two", expires_at=7_000
        )
        return "two", True


def test_real_clock_progression_reaches_margin_refreshes_and_rechecks(tmp_path, monkeypatch):
    now = [0.0]
    heartbeats = []

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(
        "scripts.gate5b_oauth_soak.write_private_json", lambda *_args, **_kwargs: None
    )
    summary = run_soak(
        store=Store(),
        cluster_id="cluster",
        database="database",
        tenant_id="tenant",
        case_id="case",
        contest_id="contest",
        output_path=tmp_path / "private.json",
        clock=lambda: now[0],
        sleeper=sleep,
        heartbeat=heartbeats.append,
        manager_factory=Manager,
        check_access=lambda *_args, **_kwargs: AccessCheck(True, True, True),
    )
    assert summary.actual_provider_ttl_seconds == 3_600
    assert summary.real_time_elapsed_seconds == 3_300
    assert summary.near_expiry_refresh_triggered is True
    assert summary.rotation_observed is True
    assert summary.passed is True
    assert heartbeats[-1] == 300

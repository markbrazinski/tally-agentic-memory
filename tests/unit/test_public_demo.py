from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.external.cockroach_mcp import (
    MCPAuthenticationError,
    MCPForbiddenError,
    MCPUnavailableError,
)
from src.platform import public_demo
from src.platform.public_demo import (
    RETENTION_LANGUAGE,
    PublicDemoConfig,
    PublicDemoUnavailableError,
    _public_projection,
    unavailable_projection,
)


def _replay(*, match=True, ttl_days=90):
    return {
        "then": {
            "as_of": "1780000000000000000.0000000000",
            "state": "FILED",
            "tariff_rate": 250,
            "version_label": "private-version",
            "evidence_hash": "sha256:" + "a" * 64,
        },
        "now": {
            "state": "CONTESTED",
            "tariff_rate": 250,
            "evidence_hash_recomputed": "sha256:" + "a" * 64,
        },
        "tamper_check": {"match": match},
        "retention": {"ttl_days": ttl_days, "target_queryable": True},
        "queries": ["private query detail"],
    }


def _outcome(*, status="found"):
    memory = SimpleNamespace(
        current_state="CONTESTED",
        recorded_rate="250.00",
        claimed_rate="350.00",
        rate_currency="USD",
        rate_unit="day",
    )
    return SimpleNamespace(status=status, memory=memory if status == "found" else None)


def test_public_projection_is_executed_and_contains_only_safe_memory_facts():
    result = _public_projection(_replay(), {"passed": True}, _outcome())

    assert result["status"] == "executed"
    assert result["mock_fallback"] is False
    assert result["replay"]["then"]["state"] == "FILED"
    assert result["replay"]["now"]["state"] == "CONTESTED"
    assert result["managed_mcp"]["recorded_rate"] == 250.0
    assert result["managed_mcp"]["later_claimed_rate"] == 350.0
    assert result["replay"]["retention"]["language"] == RETENTION_LANGUAGE

    encoded = json.dumps(result)
    for private_value in (
        "1780000000000000000.0000000000",
        "private-version",
        "sha256:",
        "private query detail",
    ):
        assert private_value not in encoded
    for prohibited_key in (
        '"tenant_id"',
        '"case_id"',
        '"contest_id"',
        '"version_id"',
        '"evidence_hash"',
        '"sealed_txn_ts"',
        '"queries"',
    ):
        assert prohibited_key not in encoded


@pytest.mark.parametrize(
    ("receipt", "outcome", "replay", "code"),
    [
        ({"passed": False}, _outcome(), _replay(), "evidence_verification_failed"),
        ({"passed": True}, _outcome(status="unavailable"), _replay(), "mcp_memory_unavailable"),
        ({"passed": True}, _outcome(), _replay(match=False), "evidence_binding_mismatch"),
        ({"passed": True}, _outcome(), _replay(ttl_days=1), "retention_validation_failed"),
    ],
)
def test_public_projection_fails_closed(receipt, outcome, replay, code):
    with pytest.raises(PublicDemoUnavailableError) as exc:
        _public_projection(replay, receipt, outcome)
    assert exc.value.safe_code == code


def test_server_config_rejects_missing_or_non_uuid_ids():
    with pytest.raises(PublicDemoUnavailableError, match="configuration_unavailable"):
        PublicDemoConfig(tenant_id="not-a-uuid", case_id="", contest_id="").validate()


def test_unavailable_projection_never_claims_a_fallback():
    assert unavailable_projection("mcp_memory_unavailable") == {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "status": "unavailable",
        "error_code": "mcp_memory_unavailable",
        "mock_fallback": False,
    }


class _Context:
    def __init__(self, token):
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _TokenManager:
    def __init__(self):
        self.access_calls = 0
        self.unauthorized_calls = []

    def access_token(self):
        self.access_calls += 1
        return "synthetic-access-one", False

    def refresh_after_unauthorized(self, rejected_token):
        self.unauthorized_calls.append(rejected_token)
        return SimpleNamespace(access_token="synthetic-access-two"), True


class _DALContext:
    def __enter__(self):
        return SimpleNamespace()

    def __exit__(self, *_args):
        return None


def _live_inputs(monkeypatch, retrieve):
    monkeypatch.setattr(public_demo, "replay_case", lambda *_args, **_kwargs: _replay())
    monkeypatch.setattr(
        public_demo,
        "verify_case_receipt",
        lambda *_args, **_kwargs: {"passed": True},
    )
    monkeypatch.setattr(public_demo, "retrieve_contest_memory", retrieve)
    return PublicDemoConfig(
        tenant_id="40000000-0000-4000-8000-000000000010",
        case_id="40000000-0000-4000-8000-000000000011",
        contest_id="40000000-0000-4000-8000-000000000012",
    )


def test_first_401_refreshes_once_and_replays_the_entire_fixed_retrieval(monkeypatch):
    manager = _TokenManager()
    tokens = []

    def retrieve(mcp, **_kwargs):
        tokens.append(mcp.token)
        if len(tokens) == 1:
            raise MCPAuthenticationError("unauthorized")
        return _outcome()

    result = public_demo.run_public_demo(
        _live_inputs(monkeypatch, retrieve),
        dal_factory=_DALContext,
        s3_client=object(),
        token_manager=manager,
        mcp_factory=_Context,
    )

    assert result["status"] == "executed"
    assert tokens == ["synthetic-access-one", "synthetic-access-two"]
    assert manager.access_calls == 1
    assert manager.unauthorized_calls == ["synthetic-access-one"]


def test_403_fails_closed_without_refresh(monkeypatch):
    manager = _TokenManager()
    tokens = []

    def retrieve(mcp, **_kwargs):
        tokens.append(mcp.token)
        raise MCPForbiddenError("forbidden")

    with pytest.raises(PublicDemoUnavailableError) as exc:
        public_demo.run_public_demo(
            _live_inputs(monkeypatch, retrieve),
            dal_factory=_DALContext,
            s3_client=object(),
            token_manager=manager,
            mcp_factory=_Context,
        )

    assert exc.value.safe_code == "mcp_memory_unavailable"
    assert tokens == ["synthetic-access-one"]
    assert manager.unauthorized_calls == []


def test_second_401_fails_closed_without_another_refresh(monkeypatch):
    manager = _TokenManager()
    tokens = []

    def retrieve(mcp, **_kwargs):
        tokens.append(mcp.token)
        raise MCPAuthenticationError("unauthorized")

    with pytest.raises(PublicDemoUnavailableError) as exc:
        public_demo.run_public_demo(
            _live_inputs(monkeypatch, retrieve),
            dal_factory=_DALContext,
            s3_client=object(),
            token_manager=manager,
            mcp_factory=_Context,
        )

    assert exc.value.safe_code == "mcp_memory_unavailable"
    assert tokens == ["synthetic-access-one", "synthetic-access-two"]
    assert manager.unauthorized_calls == ["synthetic-access-one"]


def test_mcp_outage_after_successful_refresh_fails_closed_without_loop(monkeypatch):
    manager = _TokenManager()
    attempts = 0

    def retrieve(_mcp, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise MCPAuthenticationError("unauthorized")
        raise MCPUnavailableError("provider unavailable")

    with pytest.raises(PublicDemoUnavailableError, match="mcp_memory_unavailable"):
        public_demo.run_public_demo(
            _live_inputs(monkeypatch, retrieve),
            dal_factory=_DALContext,
            s3_client=object(),
            token_manager=manager,
            mcp_factory=_Context,
        )
    assert attempts == 2
    assert manager.unauthorized_calls == ["synthetic-access-one"]


def test_default_runtime_factory_uses_manager_token_not_static_environment(monkeypatch):
    manager = _TokenManager()
    seen = []
    monkeypatch.setenv("TALLY_MCP_CLUSTER_ID", "synthetic-cluster")
    monkeypatch.setenv("TALLY_MCP_DATABASE", "synthetic_database")
    monkeypatch.delenv("TALLY_MCP_SERVICE_IDENTITY", raising=False)
    monkeypatch.delenv("TALLY_MCP_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("TALLY_MCP_ACCESS_TOKEN", raising=False)

    class Client(_Context):
        def __init__(self, config):
            seen.append(config.access_token)
            super().__init__(config.access_token)

    monkeypatch.setattr(public_demo, "CockroachManagedMCP", Client)
    result = public_demo.run_public_demo(
        _live_inputs(monkeypatch, lambda *_args, **_kwargs: _outcome()),
        dal_factory=_DALContext,
        s3_client=object(),
        token_manager=manager,
    )

    assert result["status"] == "executed"
    assert seen == ["synthetic-access-one"]

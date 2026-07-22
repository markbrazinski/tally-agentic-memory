from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.gate3_mcp_retrieval as runner
from scripts.gate3_mcp_retrieval import (
    Gate3Inputs,
    Gate3RunnerError,
    _execute,
    _load_gate1_seal,
    _preconditions_match,
)
from src.external.cockroach_mcp import ManagedMCPConfig, MCPCallTrace, MCPSelectResult
from tests.unit.test_sealed_memory import (
    CASE_ID,
    CONTEST_ID,
    TENANT_ID,
    sealed_rows,
)

WRONG_TENANT_ID = "50000000-0000-4000-8000-000000000003"
UNKNOWN_CASE_ID = "50000000-0000-4000-8000-000000000004"
UNSEALED_CASE_ID = "50000000-0000-4000-8000-000000000005"
CORRELATION_ID = "50000000-0000-4000-8000-000000000006"
UNKNOWN_CONTEST_ID = "50000000-0000-4000-8000-000000000007"
UNSEALED_CONTEST_ID = "50000000-0000-4000-8000-000000000008"


def _set_inputs(monkeypatch):
    values = {
        "TALLY_GATE3_TENANT_ID": TENANT_ID,
        "TALLY_GATE3_HERO_CASE_ID": CASE_ID,
        "TALLY_GATE3_WRONG_TENANT_ID": WRONG_TENANT_ID,
        "TALLY_GATE3_UNKNOWN_CASE_ID": UNKNOWN_CASE_ID,
        "TALLY_GATE3_UNSEALED_CASE_ID": UNSEALED_CASE_ID,
        "TALLY_GATE3_HERO_CONTEST_ID": CONTEST_ID,
        "TALLY_GATE3_UNKNOWN_CONTEST_ID": UNKNOWN_CONTEST_ID,
        "TALLY_GATE3_UNSEALED_CONTEST_ID": UNSEALED_CONTEST_ID,
        "TALLY_GATE3_CORRELATION_ID": CORRELATION_ID,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def _gate1_seal(path: Path) -> Path:
    row = sealed_rows()[0]
    path.write_text(
        json.dumps(
            {
                "first": {
                    "state": "FILED",
                    "manifest_version": 1,
                    "evidence_count": 1,
                    "evidence_hash": row["evidence_hash"],
                    "evidence_manifest": row["evidence_manifest"],
                }
            }
        )
    )
    return path


def _inputs(tmp_path: Path) -> Gate3Inputs:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(
        json.dumps(
            {
                "database": "synthetic_database",
                "tenant_id": TENANT_ID,
                "hero_case_id": CASE_ID,
                "hero_contest_id": CONTEST_ID,
                "wrong_tenant_id": WRONG_TENANT_ID,
                "unknown_case_id": UNKNOWN_CASE_ID,
                "unknown_contest_id": UNKNOWN_CONTEST_ID,
                "unsealed_case_id": UNSEALED_CASE_ID,
                "unsealed_contest_id": UNSEALED_CONTEST_ID,
                "preconditions": {
                    "hero_contest_recorded": True,
                    "wrong_tenant_exists": True,
                    "unknown_case_absent": True,
                    "unsealed_case_exists_and_is_unsealed": True,
                    "normal_workflow_rejected_unsealed_contest": True,
                    "adversarial_unsealed_contest_bound": True,
                },
            }
        )
    )
    return Gate3Inputs(
        tenant_id=TENANT_ID,
        hero_case_id=CASE_ID,
        wrong_tenant_id=WRONG_TENANT_ID,
        unknown_case_id=UNKNOWN_CASE_ID,
        unsealed_case_id=UNSEALED_CASE_ID,
        hero_contest_id=CONTEST_ID,
        unknown_contest_id=UNKNOWN_CONTEST_ID,
        unsealed_contest_id=UNSEALED_CONTEST_ID,
        hero_correlation_id=CORRELATION_ID,
        private_output=tmp_path / "unused.json",
        reference_seal_path=_gate1_seal(tmp_path / "gate1.json"),
        prepared_inputs_path=prepared,
    )


class _FakeMCP:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def verify_known_write_tool_denied(self):
        return True

    def select_query(self, query, *, correlation_id):
        self.calls.append((query, correlation_id))
        rows = (
            sealed_rows()
            if f"ca.id = '{CASE_ID}'::UUID" in query
            and f"co.id = '{CONTEST_ID}'::UUID" in query
            and f"ca.tenant_id = '{TENANT_ID}'::UUID" in query
            else []
        )
        trace = MCPCallTrace(
            started_at="2026-07-20T18:01:00Z",
            elapsed_ms=12,
            correlation_id=correlation_id,
            tool_name="select_query",
            service_identity="synthetic-readonly-runtime",
            cluster_id="synthetic-cluster-placeholder",
            database="synthetic_database",
            permission_mode="oauth-read-only",
            request_id="7",
            server_request_id="synthetic-server-request",
            row_count=len(rows),
            advertised_tools=("create_database", "create_table", "insert_rows", "select_query"),
        )
        return MCPSelectResult(tuple(rows), trace)


def _config() -> ManagedMCPConfig:
    return ManagedMCPConfig(
        cluster_id="synthetic-cluster-placeholder",
        database="synthetic_database",
        access_token="test-only-token",
        service_identity="synthetic-readonly-runtime",
        permission_mode="oauth-read-only",
    )


def test_private_inputs_are_environment_only_and_uuid_validated(monkeypatch):
    values = _set_inputs(monkeypatch)
    inputs = Gate3Inputs.from_env()
    assert inputs.hero_case_id == values["TALLY_GATE3_HERO_CASE_ID"]
    assert inputs.private_output == Path("runtime-artifacts/gate-3/executed-retrieval.json")


def test_missing_private_inputs_fail_with_no_values_in_error(monkeypatch):
    monkeypatch.delenv("TALLY_GATE3_TENANT_ID", raising=False)
    with pytest.raises(Gate3RunnerError) as exc:
        Gate3Inputs.from_env()
    assert str(exc.value) == "missing_private_gate3_inputs"


def test_gate1_seal_requires_canonical_manifest_and_exact_object_versions(tmp_path):
    path = _gate1_seal(tmp_path / "gate1.json")
    identity = _load_gate1_seal(path)
    assert identity["case_id"] == CASE_ID
    value = json.loads(path.read_text())
    del value["first"]["evidence_manifest"]["evidence"][0]["invoice_s3_version_id"]
    from src.core.receipt import canonical_json_bytes, prefixed_sha256

    value["first"]["evidence_hash"] = prefixed_sha256(
        canonical_json_bytes(value["first"]["evidence_manifest"])
    )
    path.write_text(json.dumps(value))
    with pytest.raises(Gate3RunnerError, match="incomplete"):
        _load_gate1_seal(path)


def test_execute_covers_full_runtime_matrix_without_claiming_server_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CockroachManagedMCP", _FakeMCP)
    summary, private = _execute(_config(), _inputs(tmp_path))
    assert summary.functional_passed is True
    assert summary.known_write_tools_not_advertised is False
    assert summary.write_tool_denial_observed is True
    assert private["executions"]["hero"]["memory"]["contest_id"] == CONTEST_ID
    assert private["requests"]["wrong_tenant"] == {
        "tenant_id": WRONG_TENANT_ID,
        "case_id": CASE_ID,
        "contest_id": CONTEST_ID,
        "correlation_id": private["executions"]["wrong_tenant"]["correlation_id"],
        "query_template": "gate3-sealed-memory-v1",
    }
    assert private["requests"]["unsealed"]["case_id"] == UNSEALED_CASE_ID
    assert private["requests"]["unsealed"]["contest_id"] == UNSEALED_CONTEST_ID
    assert private["audit_status"] == "pending-independent-server-correlation"
    assert "strict_passed" not in summary.as_dict()
    assert "server_audit_correlated" not in summary.as_dict()
    assert "test-only-token" not in repr(private)


def test_execute_fails_cross_gate_identity_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CockroachManagedMCP", _FakeMCP)
    inputs = _inputs(tmp_path)
    value = json.loads(inputs.reference_seal_path.read_text())
    value["first"]["evidence_manifest"]["evidence"][0]["s3_version_id"] = "different-version"
    from src.core.receipt import canonical_json_bytes, prefixed_sha256

    value["first"]["evidence_hash"] = prefixed_sha256(
        canonical_json_bytes(value["first"]["evidence_manifest"])
    )
    inputs.reference_seal_path.write_text(json.dumps(value))
    summary, _ = _execute(_config(), inputs)
    assert summary.reference_sealed_receipt_match is False
    assert summary.functional_passed is False


def test_prepared_inputs_must_match_runtime_database(tmp_path):
    inputs = _inputs(tmp_path)
    value = json.loads(inputs.prepared_inputs_path.read_text())
    value["database"] = "different_database"
    inputs.prepared_inputs_path.write_text(json.dumps(value))
    assert _preconditions_match(inputs.prepared_inputs_path, inputs, _config()) is False

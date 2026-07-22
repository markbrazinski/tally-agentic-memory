"""Execute Gate 3 through the application Managed MCP retrieval path.

Detailed values are written only under the ignored ``runtime-artifacts``
tree.  Standard output contains booleans and counts only, so it is safe to
capture in a public report after a separate history scan.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.external.cockroach_mcp import (
    WRITE_TOOLS,
    CockroachManagedMCP,
    ManagedMCPConfig,
    MCPUnavailableError,
)
from src.platform.contest_memory import QUERY_TEMPLATE, retrieve_contest_memory
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-3/executed-retrieval.json")


class Gate3RunnerError(RuntimeError):
    """Public-safe runner failure; messages must never contain private values."""


@dataclass(frozen=True)
class Gate3Inputs:
    tenant_id: str
    hero_case_id: str
    wrong_tenant_id: str
    unknown_case_id: str
    unsealed_case_id: str
    hero_contest_id: str
    unknown_contest_id: str
    unsealed_contest_id: str
    hero_correlation_id: str
    private_output: Path
    reference_seal_path: Path
    prepared_inputs_path: Path

    @classmethod
    def from_env(cls) -> Gate3Inputs:
        names = {
            "tenant_id": "TALLY_GATE3_TENANT_ID",
            "hero_case_id": "TALLY_GATE3_HERO_CASE_ID",
            "wrong_tenant_id": "TALLY_GATE3_WRONG_TENANT_ID",
            "unknown_case_id": "TALLY_GATE3_UNKNOWN_CASE_ID",
            "unsealed_case_id": "TALLY_GATE3_UNSEALED_CASE_ID",
            "hero_contest_id": "TALLY_GATE3_HERO_CONTEST_ID",
            "unknown_contest_id": "TALLY_GATE3_UNKNOWN_CONTEST_ID",
            "unsealed_contest_id": "TALLY_GATE3_UNSEALED_CONTEST_ID",
        }
        values = {field: os.environ.get(name, "") for field, name in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise Gate3RunnerError("missing_private_gate3_inputs")
        for field, value in values.items():
            try:
                UUID(value)
            except ValueError as exc:
                raise Gate3RunnerError(f"invalid_uuid_input:{field}") from exc
        correlation = os.environ.get("TALLY_GATE3_CORRELATION_ID", str(uuid4()))
        try:
            correlation = str(UUID(correlation))
        except ValueError as exc:
            raise Gate3RunnerError("invalid_uuid_input:hero_correlation_id") from exc
        return cls(
            **values,
            hero_correlation_id=correlation,
            private_output=Path(os.environ.get("TALLY_GATE3_PRIVATE_OUTPUT", str(DEFAULT_OUTPUT))),
            reference_seal_path=Path(
                os.environ.get(
                    "TALLY_GATE3_REFERENCE_SEAL",
                    "runtime-artifacts/gate-1/private-seal.json",
                )
            ),
            prepared_inputs_path=Path(
                os.environ.get(
                    "TALLY_GATE3_PREPARED_INPUTS",
                    "runtime-artifacts/gate-3/prepared-inputs.private.json",
                )
            ),
        )


@dataclass(frozen=True)
class Gate3Summary:
    functional_passed: bool
    hero_receipt_found: bool
    cross_tenant_query_empty: bool
    unknown_not_found: bool
    unsealed_not_presented: bool
    known_write_tools_not_advertised: bool
    write_tool_denial_observed: bool
    outage_recoverable: bool
    exact_version_bound: bool
    reference_sealed_receipt_match: bool
    manifest_bound: bool
    application_trace_present: bool
    client_request_id_present: bool
    server_request_id_present: bool
    fixture_preconditions_verified: bool

    def as_dict(self) -> dict[str, bool]:
        return dict(vars(self))


class _UnavailableSelector:
    def select_query(self, query: str, *, correlation_id: str):
        raise MCPUnavailableError("injected outage")


def _load_gate1_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate3RunnerError("gate1_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise Gate3RunnerError("gate1_seal_invalid")
    first = value.get("first")
    if not isinstance(first, Mapping):
        raise Gate3RunnerError("gate1_seal_invalid")
    manifest = first.get("evidence_manifest")
    evidence_hash = first.get("evidence_hash")
    if not isinstance(manifest, Mapping) or not isinstance(evidence_hash, str):
        raise Gate3RunnerError("gate1_seal_invalid")
    evidence = manifest.get("evidence")
    if (
        first.get("state") != "FILED"
        or first.get("manifest_version") != 1
        or not isinstance(evidence, list)
        or not evidence
        or first.get("evidence_count") != len(evidence)
        or prefixed_sha256(canonical_json_bytes(manifest)) != evidence_hash
    ):
        raise Gate3RunnerError("gate1_seal_not_verified")
    tariff = next(
        (item for item in evidence if isinstance(item, Mapping) and item.get("clause_id")),
        None,
    )
    if not isinstance(tariff, Mapping):
        raise Gate3RunnerError("gate1_seal_invalid")
    evidence_ids = sorted(
        str(item.get("evidence_id")) for item in evidence if isinstance(item, Mapping)
    )
    fields = {
        "tenant_id": manifest.get("tenant_id"),
        "case_id": manifest.get("case_id"),
        "invoice_id": manifest.get("invoice_id"),
        "finding_id": manifest.get("finding_id"),
        "clause_id": tariff.get("clause_id"),
        "tariff_version_id": tariff.get("s3_version_id"),
        "tariff_sha256": tariff.get("source_sha256"),
        "invoice_version_id": tariff.get("invoice_s3_version_id"),
        "invoice_sha256": tariff.get("invoice_sha256"),
        "approved_by": manifest.get("approved_by"),
        "approved_at": manifest.get("approved_at"),
        "evidence_hash": evidence_hash,
        "evidence_ids": evidence_ids,
    }
    scalars = [item for key, item in fields.items() if key != "evidence_ids"]
    if not all(isinstance(item, str) and item for item in scalars) or not all(evidence_ids):
        raise Gate3RunnerError("gate1_seal_incomplete")
    return fields


def _preconditions_match(
    path: Path,
    inputs: Gate3Inputs,
    config: ManagedMCPConfig,
) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate3RunnerError("prepared_inputs_unreadable") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("preconditions"), Mapping):
        raise Gate3RunnerError("prepared_inputs_invalid")
    expected = {
        "database": config.database,
        "tenant_id": inputs.tenant_id,
        "hero_case_id": inputs.hero_case_id,
        "hero_contest_id": inputs.hero_contest_id,
        "wrong_tenant_id": inputs.wrong_tenant_id,
        "unknown_case_id": inputs.unknown_case_id,
        "unknown_contest_id": inputs.unknown_contest_id,
        "unsealed_case_id": inputs.unsealed_case_id,
        "unsealed_contest_id": inputs.unsealed_contest_id,
    }
    return all(value.get(key) == item for key, item in expected.items()) and all(
        value["preconditions"].get(key) is True
        for key in (
            "hero_contest_recorded",
            "wrong_tenant_exists",
            "unknown_case_absent",
            "unsealed_case_exists_and_is_unsealed",
            "normal_workflow_rejected_unsealed_contest",
            "adversarial_unsealed_contest_bound",
        )
    )


def _execute(config: ManagedMCPConfig, inputs: Gate3Inputs) -> tuple[Gate3Summary, dict[str, Any]]:
    gate1 = _load_gate1_seal(inputs.reference_seal_path)
    fixture_preconditions = _preconditions_match(
        inputs.prepared_inputs_path,
        inputs,
        config,
    )
    with CockroachManagedMCP(config) as mcp:
        write_tool_denied = mcp.verify_known_write_tool_denied()
        hero = retrieve_contest_memory(
            mcp,
            tenant_id=inputs.tenant_id,
            case_id=inputs.hero_case_id,
            contest_id=inputs.hero_contest_id,
            correlation_id=inputs.hero_correlation_id,
        )
        wrong_tenant = retrieve_contest_memory(
            mcp,
            tenant_id=inputs.wrong_tenant_id,
            case_id=inputs.hero_case_id,
            contest_id=inputs.hero_contest_id,
        )
        unknown = retrieve_contest_memory(
            mcp,
            tenant_id=inputs.tenant_id,
            case_id=inputs.unknown_case_id,
            contest_id=inputs.unknown_contest_id,
        )
        unsealed = retrieve_contest_memory(
            mcp,
            tenant_id=inputs.tenant_id,
            case_id=inputs.unsealed_case_id,
            contest_id=inputs.unsealed_contest_id,
        )
    outage = retrieve_contest_memory(
        _UnavailableSelector(),
        tenant_id=inputs.tenant_id,
        case_id=inputs.hero_case_id,
        contest_id=inputs.hero_contest_id,
    )

    memory = hero.memory
    advertised = set(hero.mcp_trace.advertised_tools) if hero.mcp_trace else set()
    hero_receipt_found = hero.status == "found" and memory is not None
    known_writes_absent = bool(hero.mcp_trace) and not WRITE_TOOLS.intersection(advertised)
    exact_version_bound = bool(
        memory
        and memory.source_version_id
        and memory.source_sha256
        and memory.capture_id
        and memory.invoice_source_version_id
        and memory.invoice_source_sha256
    )
    manifest_bound = bool(memory and memory.evidence_ids and memory.evidence_hash)
    gate1_match = bool(
        memory
        and gate1["tenant_id"] == inputs.tenant_id
        and gate1["case_id"] == memory.case_id
        and gate1["invoice_id"] == memory.invoice_id
        and gate1["finding_id"] == memory.finding_id
        and gate1["clause_id"] == memory.clause_id
        and gate1["tariff_version_id"] == memory.source_version_id
        and gate1["tariff_sha256"] == memory.source_sha256
        and gate1["invoice_version_id"] == memory.invoice_source_version_id
        and gate1["invoice_sha256"] == memory.invoice_source_sha256
        and gate1["approved_by"] == memory.approved_by
        and gate1["approved_at"] == memory.sealed_at
        and gate1["evidence_hash"] == memory.evidence_hash
        and gate1["evidence_ids"] == list(memory.evidence_ids)
    )
    checks = {
        "hero_receipt_found": hero_receipt_found,
        "cross_tenant_query_empty": wrong_tenant.status == "not_found",
        "unknown_not_found": unknown.status == "not_found",
        "unsealed_not_presented": unsealed.status == "not_found",
        "known_write_tools_not_advertised": known_writes_absent,
        "write_tool_denial_observed": write_tool_denied,
        "outage_recoverable": outage.status == "unavailable" and outage.memory is None,
        "exact_version_bound": exact_version_bound,
        "reference_sealed_receipt_match": gate1_match,
        "manifest_bound": manifest_bound,
        "application_trace_present": bool(
            memory
            and memory.contest_id == inputs.hero_contest_id
            and hero.mcp_trace
            and hero.mcp_trace.correlation_id == inputs.hero_correlation_id
        ),
        "client_request_id_present": bool(hero.mcp_trace and hero.mcp_trace.request_id),
        "server_request_id_present": bool(hero.mcp_trace and hero.mcp_trace.server_request_id),
        "fixture_preconditions_verified": fixture_preconditions,
    }
    informational = {
        "known_write_tools_not_advertised",
        "server_request_id_present",
    }
    functional_passed = all(value for key, value in checks.items() if key not in informational)
    summary = Gate3Summary(
        functional_passed=functional_passed,
        **checks,
    )
    private = {
        "classification": "private Gate 3 runtime evidence",
        "summary": summary.as_dict(),
        "configuration": {
            "endpoint": config.endpoint,
            "cluster_id": config.cluster_id,
            "database": config.database,
            "service_identity": config.service_identity,
            "permission_mode": config.permission_mode,
        },
        "requests": {
            "hero": {
                "tenant_id": inputs.tenant_id,
                "case_id": inputs.hero_case_id,
                "contest_id": inputs.hero_contest_id,
                "correlation_id": inputs.hero_correlation_id,
                "query_template": QUERY_TEMPLATE,
            },
            "wrong_tenant": {
                "tenant_id": inputs.wrong_tenant_id,
                "case_id": inputs.hero_case_id,
                "contest_id": inputs.hero_contest_id,
                "correlation_id": wrong_tenant.correlation_id,
                "query_template": QUERY_TEMPLATE,
            },
            "unknown": {
                "tenant_id": inputs.tenant_id,
                "case_id": inputs.unknown_case_id,
                "contest_id": inputs.unknown_contest_id,
                "correlation_id": unknown.correlation_id,
                "query_template": QUERY_TEMPLATE,
            },
            "unsealed": {
                "tenant_id": inputs.tenant_id,
                "case_id": inputs.unsealed_case_id,
                "contest_id": inputs.unsealed_contest_id,
                "correlation_id": unsealed.correlation_id,
                "query_template": QUERY_TEMPLATE,
            },
        },
        "executions": {
            "hero": hero.as_private_dict(),
            "wrong_tenant": wrong_tenant.as_private_dict(),
            "unknown": unknown.as_private_dict(),
            "unsealed": unsealed.as_private_dict(),
            "outage": outage.as_private_dict(),
        },
        "audit_status": "pending-independent-server-correlation",
    }
    return summary, private


def run() -> Gate3Summary:
    config = ManagedMCPConfig.from_env()
    inputs = Gate3Inputs.from_env()
    summary, private = _execute(config, inputs)
    write_private_json(inputs.private_output, private)
    return summary


def main() -> int:
    try:
        summary = run()
    except (Gate3RunnerError, MCPUnavailableError) as exc:
        print(f"gate3_runtime_error={exc}", file=sys.stderr)
        return 2
    for key, value in summary.as_dict().items():
        print(f"{key}={str(value).lower()}")
    return 0 if summary.functional_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

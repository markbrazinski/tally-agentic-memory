"""Execute the immediate Gate 5B read/denial and repeated-refresh proof."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import boto3

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME
from src.external.cockroach_mcp import CockroachManagedMCP, ManagedMCPConfig
from src.external.oauth_tokens import OAuthTokenManager, SSMTokenStore, TokenBundle
from src.platform.contest_memory import retrieve_contest_memory
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-5b/phase-c-immediate.private.json")


@dataclass(frozen=True)
class AccessCheck:
    hero_query_succeeded: bool
    sealed_receipt_verified: bool
    write_scope_explicitly_denied: bool


@dataclass(frozen=True)
class PhaseCSummary:
    initial_access: AccessCheck
    refresh_grants_succeeded: int
    rotation_observed: bool
    refresh_one_access: AccessCheck
    simulated_expiry_triggered_refresh: bool
    refresh_two_access: AccessCheck
    passed_immediate_phase: bool
    real_time_expiry_soak_pending: bool


def _check_access(
    bundle: TokenBundle,
    *,
    cluster_id: str,
    database: str,
    tenant_id: str,
    case_id: str,
    contest_id: str,
    client_factory: Callable[[ManagedMCPConfig], Any] = CockroachManagedMCP,
) -> AccessCheck:
    config = ManagedMCPConfig(
        cluster_id=cluster_id,
        database=database,
        access_token=bundle.access_token,
        service_identity="gate5b-oauth-read",
        permission_mode="oauth-read-only",
    )
    with client_factory(config) as mcp:
        result = retrieve_contest_memory(
            mcp,
            tenant_id=tenant_id,
            case_id=case_id,
            contest_id=contest_id,
            correlation_id=str(uuid4()),
        )
        hero_ok = result.status == "found" and result.memory is not None
        receipt_ok = bool(
            result.memory
            and str(result.memory.recorded_rate) == "250.00"
            and str(result.memory.claimed_rate) == "350.00"
            and result.memory.evidence_hash
            and result.memory.source_version_id
            and result.memory.invoice_source_version_id
        )
        denied = mcp.verify_known_write_tool_denied()
    return AccessCheck(hero_ok, receipt_ok, denied)


def run_phase_c(
    *,
    store: SSMTokenStore,
    cluster_id: str,
    database: str,
    tenant_id: str,
    case_id: str,
    contest_id: str,
    output_path: Path = DEFAULT_OUTPUT,
    manager_factory: Callable[..., OAuthTokenManager] = OAuthTokenManager,
    check_access: Callable[..., AccessCheck] = _check_access,
) -> PhaseCSummary:
    initial_bundle = store.load()
    initial = check_access(
        initial_bundle,
        cluster_id=cluster_id,
        database=database,
        tenant_id=tenant_id,
        case_id=case_id,
        contest_id=contest_id,
    )
    with manager_factory(store) as manager:
        refreshed_one, rotated_one = manager.refresh()
    first_check = check_access(
        refreshed_one,
        cluster_id=cluster_id,
        database=database,
        tenant_id=tenant_id,
        case_id=case_id,
        contest_id=contest_id,
    )
    def simulated_clock() -> int:
        return refreshed_one.expires_at + 1
    with manager_factory(store, clock=simulated_clock) as manager:
        _token, refreshed_for_expiry = manager.access_token()
    refreshed_two = store.load()
    rotated_two = refreshed_two.refresh_token != refreshed_one.refresh_token
    second_check = check_access(
        refreshed_two,
        cluster_id=cluster_id,
        database=database,
        tenant_id=tenant_id,
        case_id=case_id,
        contest_id=contest_id,
    )
    checks = (initial, first_check, second_check)
    passed = bool(
        refreshed_for_expiry
        and (rotated_one or rotated_two)
        and all(
            item.hero_query_succeeded
            and item.sealed_receipt_verified
            and item.write_scope_explicitly_denied
            for item in checks
        )
    )
    summary = PhaseCSummary(
        initial_access=initial,
        refresh_grants_succeeded=2,
        rotation_observed=rotated_one or rotated_two,
        refresh_one_access=first_check,
        simulated_expiry_triggered_refresh=refreshed_for_expiry,
        refresh_two_access=second_check,
        passed_immediate_phase=passed,
        real_time_expiry_soak_pending=True,
    )
    write_private_json(
        output_path,
        {
            "classification": "PRIVATE GATE 5B REFRESH EVIDENCE",
            "recorded_at": int(time.time()),
            "summary": asdict(summary),
            "claim": "OBSERVED-LIVE",
            "tokens_retained_in_artifact": False,
        },
    )
    return summary


def main() -> int:
    prepared_path = Path(os.environ.get("TALLY_GATE3_PREPARED_INPUTS", ""))
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "gate5-deployer"))
        store = SSMTokenStore(
            session.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")),
            parameter_name=PARAMETER_NAME,
        )
        summary = run_phase_c(
            store=store,
            cluster_id=os.environ["TALLY_MCP_CLUSTER_ID"],
            database=str(prepared["database"]),
            tenant_id=str(prepared["tenant_id"]),
            case_id=str(prepared["hero_case_id"]),
            contest_id=str(prepared["hero_contest_id"]),
        )
        safe = asdict(summary)
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return 0 if summary.passed_immediate_phase else 1
    except Exception:
        print('{"error_code":"phase_c_failed","passed_immediate_phase":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

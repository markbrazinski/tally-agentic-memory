"""Wait through the provider TTL, refresh near expiry, and repeat live proofs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import boto3

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME
from scripts.gate5b_oauth_phase_c import AccessCheck, _check_access
from src.external.oauth_tokens import OAuthTokenManager, SSMTokenStore
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-5b/phase-c-real-time-soak.private.json")
REFRESH_MARGIN_SECONDS = 300
HEARTBEAT_SECONDS = 45


@dataclass(frozen=True)
class SoakSummary:
    actual_provider_ttl_seconds: int
    real_time_elapsed_seconds: int
    near_expiry_refresh_triggered: bool
    rotation_observed: bool
    refreshed_access: AccessCheck
    passed: bool


def run_soak(
    *,
    store: SSMTokenStore,
    cluster_id: str,
    database: str,
    tenant_id: str,
    case_id: str,
    contest_id: str,
    output_path: Path = DEFAULT_OUTPUT,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    heartbeat: Callable[[int], None] | None = None,
    manager_factory=OAuthTokenManager,
    check_access=_check_access,
) -> SoakSummary:
    initial = store.load()
    started = int(clock())
    initial_ttl = initial.expires_at - started
    if initial_ttl <= REFRESH_MARGIN_SECONDS:
        raise RuntimeError("oauth_soak_started_too_late")
    while True:
        remaining = initial.expires_at - int(clock())
        if remaining <= REFRESH_MARGIN_SECONDS:
            break
        wait_for = min(HEARTBEAT_SECONDS, remaining - REFRESH_MARGIN_SECONDS)
        sleeper(wait_for)
        if heartbeat:
            heartbeat(max(0, initial.expires_at - int(clock())))
    with manager_factory(
        store,
        refresh_margin_seconds=REFRESH_MARGIN_SECONDS,
        clock=clock,
    ) as manager:
        _access_token, triggered = manager.access_token()
    refreshed = store.load()
    rotated = refreshed.refresh_token != initial.refresh_token
    access = check_access(
        refreshed,
        cluster_id=cluster_id,
        database=database,
        tenant_id=tenant_id,
        case_id=case_id,
        contest_id=contest_id,
    )
    passed = bool(
        triggered
        and rotated
        and access.hero_query_succeeded
        and access.sealed_receipt_verified
        and access.write_scope_explicitly_denied
    )
    summary = SoakSummary(
        actual_provider_ttl_seconds=initial_ttl,
        real_time_elapsed_seconds=int(clock()) - started,
        near_expiry_refresh_triggered=triggered,
        rotation_observed=rotated,
        refreshed_access=access,
        passed=passed,
    )
    write_private_json(
        output_path,
        {
            "classification": "PRIVATE GATE 5B REAL-TIME EXPIRY EVIDENCE",
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
        store = SSMTokenStore(
            boto3.Session(
                profile_name=os.environ.get("AWS_PROFILE", "gate5-deployer")
            ).client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")),
            parameter_name=PARAMETER_NAME,
        )

        def heartbeat(remaining: int) -> None:
            print(f"gate5b_soak_remaining_minutes={max(0, remaining // 60)}", flush=True)

        summary = run_soak(
            store=store,
            cluster_id=os.environ["TALLY_MCP_CLUSTER_ID"],
            database=str(prepared["database"]),
            tenant_id=str(prepared["tenant_id"]),
            case_id=str(prepared["hero_case_id"]),
            contest_id=str(prepared["hero_contest_id"]),
            heartbeat=heartbeat,
        )
        print(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))
        return 0 if summary.passed else 1
    except Exception:
        print('{"error_code":"real_time_soak_failed","passed":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Fail-closed deployment preflight for completed private Gate 5B evidence."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import boto3

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME
from src.external.oauth_tokens import SSMTokenStore

DEFAULT_IMMEDIATE = Path("runtime-artifacts/gate-5b/phase-c-immediate.private.json")
DEFAULT_SOAK = Path("runtime-artifacts/gate-5b/phase-c-real-time-soak.private.json")


@dataclass(frozen=True)
class DeploymentPreflight:
    immediate_phase_passed: bool
    real_time_soak_passed: bool
    renewable_bundle_available: bool
    passed: bool
    error_code: str | None = None


def _mapping_file(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def run_preflight(
    *,
    store: SSMTokenStore,
    immediate_path: Path = DEFAULT_IMMEDIATE,
    soak_path: Path = DEFAULT_SOAK,
    now: int | None = None,
) -> DeploymentPreflight:
    """Require executed refresh evidence and one still-renewable SSM bundle."""
    try:
        immediate = _mapping_file(immediate_path)
        soak = _mapping_file(soak_path)
        immediate_summary = immediate["summary"]
        soak_summary = soak["summary"]
        if not isinstance(immediate_summary, Mapping) or not isinstance(
            soak_summary, Mapping
        ):
            raise ValueError
        immediate_ok = bool(
            immediate.get("claim") == "OBSERVED-LIVE"
            and immediate.get("tokens_retained_in_artifact") is False
            and immediate_summary.get("passed_immediate_phase") is True
            and immediate_summary.get("refresh_grants_succeeded", 0) >= 2
            and immediate_summary.get("rotation_observed") is True
        )
        soak_ok = bool(
            soak.get("claim") == "OBSERVED-LIVE"
            and soak.get("tokens_retained_in_artifact") is False
            and soak_summary.get("passed") is True
            and soak_summary.get("near_expiry_refresh_triggered") is True
            and soak_summary.get("rotation_observed") is True
        )
        bundle = store.load()
        current = int(time.time()) if now is None else now
        bundle_ok = bool(
            bundle.refresh_token
            and bundle.scopes == frozenset({"mcp:read"})
            and bundle.expires_at > current
        )
        passed = immediate_ok and soak_ok and bundle_ok
        return DeploymentPreflight(
            immediate_phase_passed=immediate_ok,
            real_time_soak_passed=soak_ok,
            renewable_bundle_available=bundle_ok,
            passed=passed,
            error_code=None if passed else "gate5b_evidence_incomplete",
        )
    except Exception:
        return DeploymentPreflight(False, False, False, False, "gate5b_preflight_failed")


def main() -> int:
    try:
        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "gate5-deployer")
        )
        store = SSMTokenStore(
            session.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")),
            parameter_name=os.environ.get("TALLY_OAUTH_TOKEN_PARAMETER", PARAMETER_NAME),
        )
        result = run_preflight(
            store=store,
            immediate_path=Path(
                os.environ.get("TALLY_GATE5B_IMMEDIATE_EVIDENCE", str(DEFAULT_IMMEDIATE))
            ),
            soak_path=Path(os.environ.get("TALLY_GATE5B_SOAK_EVIDENCE", str(DEFAULT_SOAK))),
        )
        print(json.dumps(result.__dict__, sort_keys=True, separators=(",", ":")))
        return 0 if result.passed else 1
    except Exception:
        print('{"error_code":"gate5b_preflight_failed","passed":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

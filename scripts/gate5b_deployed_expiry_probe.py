"""Exercise the deployed on-demand refresh path without exposing token values."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import boto3
import httpx

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME
from src.external.oauth_tokens import SSMTokenStore
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-5b/deployed-expiry-probe.private.json")


@dataclass(frozen=True)
class DeployedExpiryProbe:
    forced_safety_window: bool
    hero_executed: bool
    sealed_receipt_verified: bool
    bundle_rotated: bool
    refreshed_ttl_safe: bool
    passed: bool


def run_probe(
    *,
    store: SSMTokenStore,
    hero_url: str,
    http_client: Any,
    clock=time.time,
) -> DeployedExpiryProbe:
    before = store.load()
    now = int(clock())
    store.save(replace(before, expires_at=now + 1))
    response = http_client.get(hero_url)
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    after = store.load()
    hero_ok = bool(
        response.status_code == 200
        and isinstance(body, dict)
        and body.get("status") == "executed"
        and body.get("mock_fallback") is False
        and isinstance(body.get("managed_mcp"), dict)
        and body["managed_mcp"].get("status") == "verified_read"
    )
    receipt_ok = bool(
        isinstance(body, dict)
        and isinstance(body.get("replay"), dict)
        and isinstance(body["replay"].get("receipt"), dict)
        and body["replay"]["receipt"].get("exact_versioned_s3_verified") is True
    )
    rotated = after.refresh_token != before.refresh_token
    ttl_safe = after.expires_at - int(clock()) > 300
    passed = hero_ok and receipt_ok and rotated and ttl_safe
    return DeployedExpiryProbe(True, hero_ok, receipt_ok, rotated, ttl_safe, passed)


def main() -> int:
    try:
        profile = os.environ.get("AWS_PROFILE", "gate5-deployer")
        region = os.environ.get("AWS_REGION", "us-east-1")
        session = boto3.Session(profile_name=profile)
        apprunner = session.client("apprunner", region_name=region)
        services = apprunner.list_services()["ServiceSummaryList"]
        matches = [item for item in services if item.get("ServiceName") == "tally-gate5-demo"]
        if len(matches) != 1:
            raise ValueError("gate5_service_not_unique")
        service = apprunner.describe_service(
            ServiceArn=matches[0]["ServiceArn"]
        )["Service"]
        hero_url = "https://" + service["ServiceUrl"] + "/public/demo/hero"
        store = SSMTokenStore(
            session.client("ssm", region_name=region),
            parameter_name=os.environ.get("TALLY_OAUTH_TOKEN_PARAMETER", PARAMETER_NAME),
        )
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            result = run_probe(store=store, hero_url=hero_url, http_client=client)
        write_private_json(
            DEFAULT_OUTPUT,
            {
                "classification": "PRIVATE GATE 5B DEPLOYED EXPIRY EVIDENCE",
                "claim": "OBSERVED-LIVE",
                "summary": asdict(result),
                "tokens_retained_in_artifact": False,
            },
        )
        print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
        return 0 if result.passed else 1
    except Exception:
        print('{"error_code":"deployed_expiry_probe_failed","passed":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Scan public candidate history/worktree against the live SSM token in memory."""

from __future__ import annotations

import os
from pathlib import Path

import boto3

from scripts.gate5b_oauth_bootstrap import PARAMETER_NAME
from scripts.public_safety_scan import ScanError, scan_repository
from src.external.oauth_tokens import SSMTokenStore


def run_scan(store: SSMTokenStore, *, repo: Path) -> tuple[int, bool]:
    bundle = store.load()
    report = scan_repository(
        repo,
        exact_values=(bundle.access_token, bundle.refresh_token, bundle.client_id),
    )
    token_findings = sum(
        finding.code == "exact_prohibited_value" for finding in report.findings
    )
    return token_findings, token_findings == 0


def main() -> int:
    try:
        store = SSMTokenStore(
            boto3.Session(
                profile_name=os.environ.get("AWS_PROFILE", "gate5-deployer")
            ).client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")),
            parameter_name=PARAMETER_NAME,
        )
        findings, passed = run_scan(store, repo=Path("."))
    except (ScanError, OSError, ValueError, RuntimeError):
        print("token_leak_scan_error=true")
        print("passed=false")
        return 2
    print(f"token_value_findings={findings}")
    print(f"passed={str(passed).lower()}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

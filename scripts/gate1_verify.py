#!/usr/bin/env python3
"""Independently reopen one sealed Gate 1 receipt.

The full report, including private identifiers and hashes, is written only to
an ignored mode-0600 file. Standard output is aggregate pass/fail evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import boto3

from src.external.dal import DAL, Tenant
from src.external.db import connect
from src.platform.receipt_verifier import CaseReceiptNotFoundError, verify_case_receipt


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env-var", default="TALLY_GATE1_CRDB_DSN")
    parser.add_argument("--aws-profile")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--private-output",
        default="runtime-artifacts/gate-1/private-verifier-report.json",
    )
    return parser.parse_args(argv)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as private_file:
            fd = -1
            json.dump(value, private_file, indent=2, sort_keys=True, default=str)
            private_file.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dsn = os.environ.get(args.dsn_env_var)
    if not dsn:
        raise RuntimeError(f"required DSN environment variable {args.dsn_env_var!r} is not set")

    session = boto3.Session(profile_name=args.aws_profile)
    s3_client = session.client("s3")
    try:
        with connect(dsn) as conn:
            dal = DAL(conn, Tenant(tenant_id=args.tenant_id, actor="gate1-receipt-verifier"))
            report = verify_case_receipt(dal, s3_client, case_id=args.case_id)
    except CaseReceiptNotFoundError:
        report = {"passed": False, "reasons": ["case_not_found"], "checks": []}

    private_report = {
        "tenant_id": args.tenant_id,
        "case_id": args.case_id,
        **report,
    }
    _write_private_json(Path(args.private_output), private_report)
    checks = report.get("checks", [])
    public_report = {
        "passed": bool(report.get("passed")),
        "checks_total": len(checks),
        "checks_passed": sum(bool(check.get("passed")) for check in checks),
        "reason_count": len(report.get("reasons", [])),
        "private_details_written": True,
    }
    print(json.dumps(public_report, indent=2, sort_keys=True))
    return 0 if public_report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prepare one Gate 1 receipt against exact S3 versions and CockroachDB.

Sensitive identifiers and hashes are written only to an ignored private output
path. Standard output contains aggregate/pass-fail evidence suitable for a
redacted Gate Report. This runner deliberately stops before human approval and
sealing; approval must arrive through the authenticated product workflow.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import boto3

from src.core.receipt import (
    InvoiceClaim,
    TariffExtraction,
    TariffVerification,
    parse_invoice_claim,
    verify_tariff_extraction,
)
from src.external.dal import DAL, Tenant
from src.external.db import connect
from src.external.seed_demo_tenant import run_seed
from src.external.tariff_extract import BedrockTariffExtractor
from src.external.versioned_source import RetainedObject, S3VersionedSource
from src.platform.receipt_pipeline import (
    StoredReceiptInputs,
    file_gate1_case,
    persist_verified_inputs,
)


@dataclass(frozen=True)
class PreparationAttempt:
    """Private in-memory result of one complete retained-input attempt."""

    tariff: RetainedObject
    invoice: RetainedObject
    extraction: TariffExtraction
    verification: TariffVerification
    invoice_claim: InvoiceClaim
    stored: StoredReceiptInputs
    filed: dict[str, Any]


class Gate1AttemptError(RuntimeError):
    """Expected fail-closed result that is safe to summarize publicly."""

    def __init__(
        self,
        stage: str,
        reasons: tuple[str, ...] = (),
        private_details: dict[str, Any] | None = None,
    ):
        super().__init__(stage)
        self.stage = stage
        self.reasons = reasons
        self.private_details = private_details or {}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env-var", default="TALLY_GATE1_CRDB_DSN")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--tariff-key", required=True)
    parser.add_argument("--tariff-version-id", required=True)
    parser.add_argument("--invoice-key", required=True)
    parser.add_argument("--invoice-version-id", required=True)
    parser.add_argument("--carrier-scac", default="NOLU")
    parser.add_argument("--lane", default="NP-SP")
    parser.add_argument("--dispute-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--private-output",
        default="runtime-artifacts/gate-1/private-receipt.json",
    )
    return parser.parse_args(argv)


def _carrier_id(conn: Any, tenant_id: str, carrier_scac: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM carriers WHERE tenant_id=%s AND scac=%s;",
            (tenant_id, carrier_scac),
        )
        carrier = cur.fetchone()
    if carrier is None:
        raise RuntimeError("synthetic tenant/carrier seed is incomplete")
    return str(carrier[0])


def _counts(conn: Any, tenant_id: str, attempt: PreparationAttempt) -> dict[str, int]:
    invoice_id = attempt.stored.invoice_id
    queries = {
        "tariff_snapshots": (
            "SELECT count(*) FROM tariff_snapshots "
            "WHERE tenant_id=%s AND s3_key=%s AND source_version_id=%s;",
            (tenant_id, attempt.tariff.key, attempt.tariff.version_id),
        ),
        "tariff_clauses": (
            "SELECT count(*) FROM tariff_clauses tc JOIN tariff_snapshots ts "
            "ON ts.tenant_id=tc.tenant_id AND ts.id=tc.snapshot_id "
            "WHERE ts.tenant_id=%s AND ts.s3_key=%s AND ts.source_version_id=%s;",
            (tenant_id, attempt.tariff.key, attempt.tariff.version_id),
        ),
        "invoices": (
            "SELECT count(*) FROM invoices "
            "WHERE tenant_id=%s AND s3_key=%s AND source_version_id=%s;",
            (tenant_id, attempt.invoice.key, attempt.invoice.version_id),
        ),
        "clerk_runs": (
            "SELECT count(*) FROM clerk_runs WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        ),
        "findings": (
            "SELECT count(*) FROM findings WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        ),
        "cases": (
            "SELECT count(*) FROM cases WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        ),
        "case_evidence": (
            "SELECT count(*) FROM case_evidence ce JOIN cases c "
            "ON c.tenant_id=ce.tenant_id AND c.id=ce.case_id "
            "WHERE c.tenant_id=%s AND c.invoice_id=%s;",
            (tenant_id, invoice_id),
        ),
        "ledger_events": (
            "SELECT count(*) FROM ledger_events le JOIN cases c "
            "ON c.tenant_id=le.tenant_id AND c.id=le.case_id "
            "WHERE c.tenant_id=%s AND c.invoice_id=%s;",
            (tenant_id, invoice_id),
        ),
    }
    result = {}
    with conn.cursor() as cur:
        for name, (sql, params) in queries.items():
            cur.execute(sql, params)
            result[name] = int(cur.fetchone()[0])
    return result


def _database_evidence(conn: Any, tenant_id: str, case_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.verification_status, tc.sha256,
                   ts.source_version_id, ts.doc_sha256,
                   f.human_approval_state, c.state,
                   c.evidence_hash, c.manifest_version,
                   c.evidence_manifest IS NOT NULL,
                   c.sealed_txn_ts
            FROM cases c
            JOIN findings f ON f.tenant_id=c.tenant_id AND f.id=c.finding_id
            JOIN tariff_clauses tc
              ON tc.tenant_id=f.tenant_id AND tc.id=f.tariff_clause_id
            JOIN tariff_snapshots ts
              ON ts.tenant_id=tc.tenant_id AND ts.id=tc.snapshot_id
            WHERE c.tenant_id=%s AND c.id=%s;
            """,
            (tenant_id, case_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("prepared receipt evidence row is missing")
    return {
        "verification_status": row[0],
        "clause_hash_present": bool(row[1]),
        "source_version_present": bool(row[2]),
        "source_hash_present": bool(row[3]),
        "human_approval_state": row[4],
        "case_state": row[5],
        "evidence_hash_present": bool(row[6]),
        "manifest_version": row[7],
        "evidence_manifest_present": bool(row[8]),
        "sealed_txn_ts_present": row[9] is not None,
    }


def _prepare_attempt(
    *,
    args: argparse.Namespace,
    conn: Any,
    tenant_id: str,
    carrier_id: str,
    s3_client: Any,
) -> PreparationAttempt:
    """Run retrieval through filing once, with no approval or sealing side effect."""
    source_store = S3VersionedSource(s3_client)
    tariff = source_store.get_exact(
        bucket=args.bucket,
        key=args.tariff_key,
        version_id=args.tariff_version_id,
    )
    invoice = source_store.get_exact(
        bucket=args.bucket,
        key=args.invoice_key,
        version_id=args.invoice_version_id,
    )
    extraction = BedrockTariffExtractor().extract(tariff.body.decode("utf-8"))
    verification = verify_tariff_extraction(tariff.body, extraction)
    if not verification.eligible:
        raise Gate1AttemptError("tariff_verification", tuple(verification.reasons))
    invoice_claim = parse_invoice_claim(invoice.body)
    dal = DAL(conn, Tenant(tenant_id=tenant_id, actor="gate1-receipt-runner"))
    stored = persist_verified_inputs(
        dal,
        carrier_id=carrier_id,
        lane=args.lane,
        tariff=tariff,
        tariff_source_url="retained-object://exact-version",
        extraction=extraction,
        verification=verification,
        invoice=invoice,
        invoice_claim=invoice_claim,
    )
    filed = file_gate1_case(
        dal,
        carrier_id=carrier_id,
        stored=stored,
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=invoice_claim,
        pin_date=args.dispute_date,
    )
    if not filed["filed"]:
        raise Gate1AttemptError(
            "no_overcharge",
            private_details={"calculation": filed["calculation"]},
        )
    return PreparationAttempt(
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=invoice_claim,
        stored=stored,
        filed=filed,
    )


def _same_receipt(first: PreparationAttempt, second: PreparationAttempt) -> bool:
    return (
        first.stored.snapshot_id == second.stored.snapshot_id
        and first.stored.clause_id == second.stored.clause_id
        and first.stored.invoice_id == second.stored.invoice_id
        and first.filed["case_id"] == second.filed["case_id"]
        and first.filed["finding_id"] == second.filed["finding_id"]
        and bool(second.filed.get("already_filed"))
    )


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Write private evidence without a world-readable creation window."""
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

    tenant_id = run_seed(dsn=dsn)
    s3_client = boto3.client("s3")
    try:
        with connect(dsn) as conn:
            carrier_id = _carrier_id(conn, tenant_id, args.carrier_scac)
            first = _prepare_attempt(
                args=args,
                conn=conn,
                tenant_id=tenant_id,
                carrier_id=carrier_id,
                s3_client=s3_client,
            )
            counts_after_first = _counts(conn, tenant_id, first)
            second = _prepare_attempt(
                args=args,
                conn=conn,
                tenant_id=tenant_id,
                carrier_id=carrier_id,
                s3_client=s3_client,
            )
            counts_after_second = _counts(conn, tenant_id, second)
            db_evidence = _database_evidence(
                conn,
                tenant_id,
                first.filed["case_id"],
            )
    except Gate1AttemptError as exc:
        _write_private_json(
            Path(args.private_output),
            {
                "workflow_replay_passed": False,
                "stage": exc.stage,
                "reasons": exc.reasons,
                "tariff": {
                    "bucket": args.bucket,
                    "key": args.tariff_key,
                    "version_id": args.tariff_version_id,
                },
                "invoice": {
                    "bucket": args.bucket,
                    "key": args.invoice_key,
                    "version_id": args.invoice_version_id,
                },
                **exc.private_details,
            },
        )
        print(
            json.dumps(
                {
                    "workflow_replay_passed": False,
                    "stage": exc.stage,
                    "reason_count": len(exc.reasons),
                },
                sort_keys=True,
            )
        )
        return 1

    idempotent_counts = counts_after_first == counts_after_second
    stable_receipt = _same_receipt(first, second)
    expected_prepared_counts = {
        "tariff_snapshots": 1,
        "tariff_clauses": 1,
        "invoices": 1,
        "clerk_runs": 1,
        "findings": 1,
        "cases": 1,
        "case_evidence": 1,
        "ledger_events": 0,
    }
    expected_sealed_counts = {**expected_prepared_counts, "ledger_events": 1}
    receipt_is_prepared = (
        db_evidence["human_approval_state"] == "NOT_PRESSED"
        and db_evidence["case_state"] == "ANALYZED"
        and not db_evidence["evidence_hash_present"]
        and db_evidence["manifest_version"] is None
        and not db_evidence["evidence_manifest_present"]
        and not db_evidence["sealed_txn_ts_present"]
    )
    receipt_is_sealed = (
        db_evidence["human_approval_state"] == "APPROVED"
        and db_evidence["case_state"] == "FILED"
        and db_evidence["evidence_hash_present"]
        and db_evidence["manifest_version"] is not None
        and db_evidence["evidence_manifest_present"]
        and db_evidence["sealed_txn_ts_present"]
    )
    receipt_state = (
        "PREPARED"
        if receipt_is_prepared
        else "SEALED"
        if receipt_is_sealed
        else "INCOHERENT"
    )
    expected_counts = (
        expected_prepared_counts
        if receipt_state == "PREPARED"
        else expected_sealed_counts
    )
    complete_counts = counts_after_second == expected_counts
    workflow_replay_passed = bool(
        first.verification.eligible
        and idempotent_counts
        and stable_receipt
        and complete_counts
        and db_evidence["verification_status"] == "VERIFIED"
        and db_evidence["clause_hash_present"]
        and db_evidence["source_version_present"]
        and db_evidence["source_hash_present"]
        and receipt_state != "INCOHERENT"
    )
    private_record = {
        "tenant_id": tenant_id,
        "carrier_id": carrier_id,
        "snapshot_id": first.stored.snapshot_id,
        "clause_id": first.stored.clause_id,
        "invoice_id": first.stored.invoice_id,
        "case_id": first.filed["case_id"],
        "finding_id": first.filed["finding_id"],
        "tariff": {
            "bucket": first.tariff.bucket,
            "key": first.tariff.key,
            "version_id": first.tariff.version_id,
            "sha256": first.verification.source_sha256,
        },
        "invoice": {
            "bucket": first.invoice.bucket,
            "key": first.invoice.key,
            "version_id": first.invoice.version_id,
        },
        "calculation": first.filed["calculation"],
        "first_already_filed": bool(first.filed.get("already_filed")),
        "second_already_filed": bool(second.filed.get("already_filed")),
        "stable_receipt_identity": stable_receipt,
        "receipt_state": receipt_state,
        "approval_or_seal_performed_by_runner": False,
        "database_evidence": db_evidence,
        "counts_after_first": counts_after_first,
        "counts_after_second": counts_after_second,
        "workflow_replay_passed": workflow_replay_passed,
    }
    private_path = Path(args.private_output)
    _write_private_json(private_path, private_record)

    public_result = {
        "workflow_replay_passed": workflow_replay_passed,
        "stage": (
            "prepared_for_human_approval"
            if receipt_state == "PREPARED"
            else "human_sealed_receipt_revalidated"
            if receipt_state == "SEALED"
            else "incoherent_receipt_state"
        ),
        "receipt_state": receipt_state,
        "tariff_extraction_eligible": first.verification.eligible,
        "full_attempts_executed": 2,
        "idempotent_rerun": idempotent_counts and stable_receipt,
        "expected_counts_met": complete_counts,
        "row_counts": counts_after_second,
        "human_approval_required": receipt_state == "PREPARED",
        "receipt_is_unsealed": receipt_state == "PREPARED",
        "approval_or_seal_performed_by_runner": False,
        "private_details_written": True,
    }
    print(json.dumps(public_result, indent=2, sort_keys=True))
    return 0 if workflow_replay_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

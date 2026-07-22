"""CockroachDB/S3 adapter for the pure sealed-receipt verifier."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.core.receipt_verifier import LoadedSource, verify_sealed_receipt
from src.external.dal import DAL
from src.external.versioned_source import S3VersionedSource


class CaseReceiptNotFoundError(Exception):
    pass


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def verify_case_receipt(dal: DAL, s3_client: Any, *, case_id: str) -> dict[str, Any]:
    """Load one tenant-scoped receipt and delegate all verdict logic to core."""
    rows = dal.execute(
        """
        SELECT tenant_id, id, state, manifest_version, evidence_manifest,
               evidence_hash, sealed_by, sealed_txn_ts, sealed_at_display,
               invoice_id, finding_id
        FROM cases WHERE tenant_id=%s AND id=%s;
        """,
        (case_id,),
        tag="receipt.verify.case",
    )
    if not rows:
        raise CaseReceiptNotFoundError(case_id)
    row = rows[0]
    stored_case = {
        "tenant_id": str(row[0]),
        "id": str(row[1]),
        "state": row[2],
        "manifest_version": row[3],
        "sealed_by": str(row[6]) if row[6] is not None else None,
        "sealed_txn_ts": row[7],
        "sealed_at_display": row[8],
        "invoice_id": str(row[9]),
        "finding_id": str(row[10]),
    }
    manifest = _mapping(row[4])
    evidence_hash = row[5] or ""
    source_store = S3VersionedSource(s3_client)

    def load_source(bucket: str, key: str, version_id: str) -> LoadedSource:
        retained = source_store.get_exact(bucket=bucket, key=key, version_id=version_id)
        return LoadedSource(
            bucket=retained.bucket,
            key=retained.key,
            version_id=retained.version_id,
            body=retained.body,
            observed_at=retained.observed_at,
        )

    def load_capture(tenant_id: str, capture_id: str):
        if tenant_id != dal.tenant.tenant_id:
            return None
        capture_rows = dal.execute(
            """
            SELECT id, tenant_id, carrier_id, s3_key, source_version_id,
                   source_byte_size, doc_sha256, captured_at, committed_at
            FROM tariff_snapshots WHERE tenant_id=%s AND id=%s;
            """,
            (capture_id,),
            tag="receipt.verify.capture",
        )
        if not capture_rows:
            return None
        value = capture_rows[0]
        return {
            "id": str(value[0]),
            "tenant_id": str(value[1]),
            "carrier_id": str(value[2]),
            "s3_key": value[3],
            "source_version_id": value[4],
            "source_byte_size": value[5],
            "doc_sha256": value[6],
            "captured_at": value[7],
            "committed_at": value[8],
        }

    def load_clause(tenant_id: str, clause_id: str):
        if tenant_id != dal.tenant.tenant_id:
            return None
        clause_rows = dal.execute(
            """
            SELECT id, tenant_id, snapshot_id, clause_text, sha256,
                   rate_amount, rate_currency, rate_unit, effective_from,
                   effective_to, committed_at, verification_status,
                   verification_reason
            FROM tariff_clauses WHERE tenant_id=%s AND id=%s;
            """,
            (clause_id,),
            tag="receipt.verify.clause",
        )
        if not clause_rows:
            return None
        value = clause_rows[0]
        return {
            "id": str(value[0]),
            "tenant_id": str(value[1]),
            "snapshot_id": str(value[2]),
            "clause_text": value[3],
            "sha256": value[4],
            "rate_amount": str(value[5]),
            "rate_currency": value[6],
            "rate_unit": value[7],
            "effective_from": value[8],
            "effective_to": value[9],
            "committed_at": value[10],
            "verification_status": value[11],
            "verification_reason": value[12],
        }

    def load_evidence(tenant_id: str, evidence_id: str):
        if tenant_id != dal.tenant.tenant_id:
            return None
        evidence_rows = dal.execute(
            """
            SELECT id, tenant_id, case_id, kind, source_table, source_id,
                   content, content_sha256, sealed
            FROM case_evidence WHERE tenant_id=%s AND id=%s;
            """,
            (evidence_id,),
            tag="receipt.verify.evidence",
        )
        if not evidence_rows:
            return None
        value = evidence_rows[0]
        return {
            "id": str(value[0]),
            "tenant_id": str(value[1]),
            "case_id": str(value[2]),
            "kind": value[3],
            "source_table": value[4],
            "source_id": str(value[5]),
            "content": _mapping(value[6]),
            "content_sha256": value[7],
            "sealed": value[8],
        }

    def load_invoice(tenant_id: str, invoice_id: str):
        if tenant_id != dal.tenant.tenant_id:
            return None
        invoice_rows = dal.execute(
            """
            SELECT id, tenant_id, invoice_no, s3_key, source_version_id, sha256,
                   claimed_rate, currency, rate_unit, charge_days,
                   received_at, invoice_date
            FROM invoices WHERE tenant_id=%s AND id=%s;
            """,
            (invoice_id,),
            tag="receipt.verify.invoice",
        )
        if not invoice_rows:
            return None
        value = invoice_rows[0]
        return {
            "id": str(value[0]),
            "tenant_id": str(value[1]),
            "invoice_no": value[2],
            "s3_key": value[3],
            "source_version_id": value[4],
            "sha256": value[5],
            "claimed_rate": str(value[6]),
            "rate_currency": value[7],
            "rate_unit": value[8],
            "charge_days": value[9],
            "received_at": value[10],
            "invoice_date": value[11],
        }

    def load_finding(tenant_id: str, finding_id: str):
        if tenant_id != dal.tenant.tenant_id:
            return None
        finding_rows = dal.execute(
            """
            SELECT id, tenant_id, invoice_id, recommendation, calculation,
                   human_approval_state
            FROM findings WHERE tenant_id=%s AND id=%s;
            """,
            (finding_id,),
            tag="receipt.verify.finding",
        )
        if not finding_rows:
            return None
        value = finding_rows[0]
        return {
            "id": str(value[0]),
            "tenant_id": str(value[1]),
            "invoice_id": str(value[2]),
            "recommendation": value[3],
            "calculation": _mapping(value[4]),
            "human_approval_state": value[5],
        }

    report = verify_sealed_receipt(
        manifest=manifest,
        stored_evidence_hash=evidence_hash,
        stored_case=stored_case,
        expected_tenant_id=dal.tenant.tenant_id,
        expected_case_id=case_id,
        source_loader=load_source,
        capture_loader=load_capture,
        clause_loader=load_clause,
        evidence_loader=load_evidence,
        invoice_loader=load_invoice,
        finding_loader=load_finding,
    )
    return report.as_dict()

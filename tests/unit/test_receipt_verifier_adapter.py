from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.receipt import canonical_json_bytes, prefixed_sha256, sha256_hex
from src.external.dal import Tenant
from src.platform.receipt_verifier import CaseReceiptNotFoundError, verify_case_receipt

TENANT = "tenant-fixture"
CASE = "case-fixture"
FINDING = "finding-fixture"
EVIDENCE = "evidence-fixture"
CAPTURE = "capture-fixture"
INVOICE = "invoice-fixture"
CLAUSE = "clause-fixture"
CARRIER = "carrier-fixture"
TARIFF_BODY = b"The rate is USD $250.00 per day."
INVOICE_BODY = (
    b'{"invoice_no":"INV-NOL-084213","claimed_rate":{"amount":"350.00",'
    b'"currency":"USD","unit":"per_day"},"charge_days":7,'
    b'"invoice_date":"2026-07-10","received_at":"2026-07-10T16:05:00+00:00"}'
)
TARIFF_OBSERVED = datetime(2026, 7, 2, 8, tzinfo=timezone.utc)
TARIFF_COMMITTED = datetime(2026, 7, 2, 8, 0, 1, tzinfo=timezone.utc)
CLAUSE_COMMITTED = datetime(2026, 7, 2, 8, 0, 2, tzinfo=timezone.utc)
INVOICE_OBSERVED = datetime(2026, 7, 10, 16, tzinfo=timezone.utc)
INVOICE_RECEIVED = datetime(2026, 7, 10, 16, 5, tzinfo=timezone.utc)
APPROVED_AT = datetime(2026, 7, 11, 9, tzinfo=timezone.utc)
CALCULATION = {
    "recorded_rate": "250.00",
    "claimed_rate": "350.00",
    "rate_currency": "USD",
    "rate_unit": "per_day",
    "charge_days": 7,
    "overcharge": "700.00",
    "recommendation": "dispute_overcharge",
}


def _content():
    return {
        "tenant_id": TENANT,
        "carrier_id": CARRIER,
        "capture_id": CAPTURE,
        "s3_bucket": "example-tariff-bucket",
        "s3_key": "fixture/tariff.txt",
        "s3_version_id": "fixture-tariff-v1",
        "source_sha256": sha256_hex(TARIFF_BODY),
        "source_size": len(TARIFF_BODY),
        "clause_id": CLAUSE,
        "clause_sha256": sha256_hex(TARIFF_BODY),
        "clause_text": TARIFF_BODY.decode(),
        "verification_status": "VERIFIED",
        "verification_reason": "verified",
        "rate_amount": "250.00",
        "rate_currency": "USD",
        "rate_unit": "per_day",
        "effective_from": "2026-07-01",
        "effective_to": "2026-12-31",
        "observed_at": TARIFF_OBSERVED.isoformat(),
        "committed_at": TARIFF_COMMITTED.isoformat(),
        "clause_committed_at": CLAUSE_COMMITTED.isoformat(),
        "invoice_id": INVOICE,
        "invoice_s3_bucket": "example-invoice-bucket",
        "invoice_s3_key": "fixture/invoice.json",
        "invoice_s3_version_id": "fixture-invoice-v1",
        "invoice_sha256": sha256_hex(INVOICE_BODY),
        "invoice_source_size": len(INVOICE_BODY),
        "invoice_no": "INV-NOL-084213",
        "invoice_observed_at": INVOICE_OBSERVED.isoformat(),
        "invoice_received_at": INVOICE_RECEIVED.isoformat(),
        "invoice_date": "2026-07-10",
        "invoice_claimed_rate": "350.00",
        "charge_days": 7,
        "calculation": CALCULATION,
        "recommendation": "dispute_overcharge",
    }


def _manifest():
    content = _content()
    content_hash = sha256_hex(canonical_json_bytes(content))
    return {
        "manifest_version": 1,
        "tenant_id": TENANT,
        "case_id": CASE,
        "invoice_id": INVOICE,
        "finding_id": FINDING,
        "evidence": [
            {
                **content,
                "content": content,
                "evidence_id": EVIDENCE,
                "kind": "tariff_invoice_receipt",
                "source_table": "tariff_clauses",
                "source_id": CLAUSE,
                "content_sha256": content_hash,
            }
        ],
        "calculation": CALCULATION,
        "recommendation": "dispute_overcharge",
        "approved_by": "reviewer-fixture",
        "approved_at": APPROVED_AT.isoformat(),
    }


class FakeDAL:
    tenant = Tenant(tenant_id=TENANT, actor="verifier")

    def __init__(self, *, case_exists=True):
        self.case_exists = case_exists
        self.tags = []

    def execute(self, sql, params, *, tag):
        self.tags.append(tag)
        manifest = _manifest()
        if tag == "receipt.verify.case":
            if not self.case_exists:
                return []
            return [
                (
                    TENANT,
                    CASE,
                    "FILED",
                    1,
                    manifest,
                    prefixed_sha256(canonical_json_bytes(manifest)),
                    "reviewer-fixture",
                    "1783534098823702432.0000000000",
                    APPROVED_AT,
                    INVOICE,
                    FINDING,
                )
            ]
        if tag == "receipt.verify.capture":
            return [
                (
                    CAPTURE,
                    TENANT,
                    CARRIER,
                    "fixture/tariff.txt",
                    "fixture-tariff-v1",
                    len(TARIFF_BODY),
                    sha256_hex(TARIFF_BODY),
                    TARIFF_OBSERVED,
                    TARIFF_COMMITTED,
                )
            ]
        if tag == "receipt.verify.clause":
            return [
                (
                    CLAUSE,
                    TENANT,
                    CAPTURE,
                    TARIFF_BODY.decode(),
                    sha256_hex(TARIFF_BODY),
                    "250.00",
                    "USD",
                    "per_day",
                    "2026-07-01",
                    "2026-12-31",
                    CLAUSE_COMMITTED,
                    "VERIFIED",
                    "verified",
                )
            ]
        if tag == "receipt.verify.evidence":
            content = _content()
            return [
                (
                    EVIDENCE,
                    TENANT,
                    CASE,
                    "tariff_invoice_receipt",
                    "tariff_clauses",
                    CLAUSE,
                    content,
                    sha256_hex(canonical_json_bytes(content)),
                    True,
                )
            ]
        if tag == "receipt.verify.invoice":
            return [
                (
                    INVOICE,
                    TENANT,
                    "INV-NOL-084213",
                    "fixture/invoice.json",
                    "fixture-invoice-v1",
                    sha256_hex(INVOICE_BODY),
                    "350.00",
                    "USD",
                    "per_day",
                    7,
                    INVOICE_RECEIVED,
                    "2026-07-10",
                )
            ]
        if tag == "receipt.verify.finding":
            return [
                (
                    FINDING,
                    TENANT,
                    INVOICE,
                    "dispute_overcharge",
                    CALCULATION,
                    "APPROVED",
                )
            ]
        raise AssertionError(tag)


class Body:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


class FakeS3:
    def __init__(self):
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["Key"] == "fixture/tariff.txt":
            body, observed = TARIFF_BODY, TARIFF_OBSERVED
        elif kwargs["Key"] == "fixture/invoice.json":
            body, observed = INVOICE_BODY, INVOICE_OBSERVED
        else:
            raise KeyError(kwargs["Key"])
        return {
            "VersionId": kwargs["VersionId"],
            "Body": Body(body),
            "LastModified": observed,
        }


def test_adapter_loads_all_rows_and_both_exact_object_versions():
    dal = FakeDAL()
    s3 = FakeS3()

    report = verify_case_receipt(dal, s3, case_id=CASE)

    assert report["passed"] is True
    assert report["reasons"] == []
    assert dal.tags == [
        "receipt.verify.case",
        "receipt.verify.evidence",
        "receipt.verify.capture",
        "receipt.verify.clause",
        "receipt.verify.invoice",
        "receipt.verify.finding",
    ]
    assert [(call["Key"], call["VersionId"]) for call in s3.calls] == [
        ("fixture/tariff.txt", "fixture-tariff-v1"),
        ("fixture/invoice.json", "fixture-invoice-v1"),
    ]


def test_adapter_raises_for_missing_tenant_scoped_case():
    with pytest.raises(CaseReceiptNotFoundError):
        verify_case_receipt(FakeDAL(case_exists=False), FakeS3(), case_id=CASE)

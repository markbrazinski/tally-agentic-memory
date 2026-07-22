from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from src.core.receipt import (
    InvoiceClaim,
    TariffExtraction,
    calculate_overcharge,
    verify_tariff_extraction,
)
from src.external.versioned_source import RetainedObject
from src.platform.receipt_pipeline import StoredReceiptInputs, build_receipt_evidence

TARIFF_BODY = (
    b"Section 4 - Import demurrage: The rate is USD $250.00 per day for each "
    b"chargeable day after free time."
)


def _extraction():
    return TariffExtraction.from_mapping(
        {
            "rate_amount": "250.00",
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "rate_text": "USD $250.00 per day",
            "effective_from": "2026-07-01",
            "effective_to": "2026-12-31",
            "clause_text": TARIFF_BODY.decode(),
            "source_locator": "Section 4",
            "confidence": "0.99",
        }
    )


def test_build_receipt_evidence_binds_exact_sources_times_clause_invoice_and_calculation():
    extraction = _extraction()
    verification = verify_tariff_extraction(TARIFF_BODY, extraction)
    claim = InvoiceClaim(
        invoice_no="INV-NOL-0001",
        claimed_rate=Decimal("350.00"),
        rate_currency="USD",
        rate_unit="per_day",
        charge_days=7,
        invoice_date=date(2026, 7, 10),
        received_at="2026-07-10T16:00:00Z",
    )
    calculation = calculate_overcharge(
        recorded_rate=extraction.rate_amount,
        claimed_rate=claim.claimed_rate,
        charge_days=claim.charge_days,
    )
    tariff = RetainedObject(
        bucket="example-bucket", key="fixtures/tariff.txt", version_id="fixture-tariff-v1",
        body=TARIFF_BODY, observed_at=datetime(2026, 7, 2, 8, tzinfo=UTC),
    )
    invoice = RetainedObject(
        bucket="example-bucket", key="fixtures/invoice.json", version_id="fixture-invoice-v1",
        body=b"{}", observed_at=datetime(2026, 7, 10, 16, tzinfo=UTC),
    )
    stored = StoredReceiptInputs(
        snapshot_id="capture-1", snapshot_committed_at="2026-07-02T08:00:01Z",
        clause_id="clause-1", clause_committed_at="2026-07-02T08:00:01Z",
        invoice_id="invoice-1", clerk_run_id="run-1",
    )

    evidence = build_receipt_evidence(
        tenant_id="tenant-1", carrier_id="carrier-1", stored=stored,
        tariff=tariff, invoice=invoice, extraction=extraction,
        verification=verification, invoice_claim=claim, calculation=calculation,
    )

    assert evidence["capture_id"] == "capture-1"
    assert evidence["s3_version_id"] == "fixture-tariff-v1"
    assert evidence["source_sha256"] == verification.source_sha256
    assert evidence["clause_sha256"] == verification.clause_sha256
    assert evidence["rate_amount"] == "250.00"
    assert evidence["invoice_claimed_rate"] == "350.00"
    assert evidence["calculation"]["overcharge"] == "700.00"
    assert evidence["human_approval_state"] == "NOT_PRESSED"

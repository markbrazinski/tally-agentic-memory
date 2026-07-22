"""Gate 1 orchestration from verified retained bytes to an evidence-bound case."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

import psycopg

from src.core.clerk_steps import WindowResult
from src.core.receipt import (
    InvoiceClaim,
    OverchargeCalculation,
    TariffExtraction,
    TariffVerification,
    calculate_overcharge,
    canonical_json_bytes,
    sha256_hex,
)
from src.core.verdict import VERDICT_DEFECTIVE
from src.external.dal import DAL
from src.external.versioned_source import RetainedObject
from src.platform.clerk_pipeline import ClerkResult, file_case


class EvidenceConflictError(RuntimeError):
    """An idempotency key resolved to different retained evidence."""


class TemporalEvidenceError(ValueError):
    """The effective, observed, or dispute chronology is impossible."""


class ExistingReceiptConflictError(RuntimeError):
    """An invoice already has a case bound to different receipt evidence."""


@dataclass(frozen=True)
class StoredReceiptInputs:
    snapshot_id: str
    snapshot_committed_at: str
    clause_id: str
    clause_committed_at: str
    invoice_id: str
    clerk_run_id: str


def _as_utc(value: datetime | str) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise TemporalEvidenceError("evidence timestamps must include a timezone")
    return parsed.astimezone(UTC)


def validate_receipt_chronology(
    *,
    tariff: RetainedObject,
    invoice: RetainedObject,
    extraction: TariffExtraction,
    invoice_claim: InvoiceClaim,
    dispute_date: date | None = None,
) -> None:
    """Keep effective, observed, and dispute time domains explicit and ordered."""
    if invoice_claim.invoice_date < extraction.effective_from or (
        extraction.effective_to is not None
        and invoice_claim.invoice_date > extraction.effective_to
    ):
        raise TemporalEvidenceError("tariff is not effective on the invoice date")

    tariff_observed = _as_utc(tariff.observed_at)
    invoice_observed = _as_utc(invoice.observed_at)
    invoice_received = _as_utc(invoice_claim.received_at)
    if tariff_observed > invoice_observed or tariff_observed > invoice_received:
        raise TemporalEvidenceError("tariff must be observed before the invoice")

    if dispute_date is not None:
        dispute_cutoff = datetime.combine(dispute_date, time.max, tzinfo=UTC)
        if invoice_observed > dispute_cutoff or invoice_received > dispute_cutoff:
            raise TemporalEvidenceError("invoice must be observed before the dispute date")


def _assert_reused_row(label: str, actual: tuple, expected: tuple) -> None:
    if actual != expected:
        raise EvidenceConflictError(f"{label} idempotency key maps to different evidence")


def _assert_reused_clause(
    row: tuple,
    *,
    carrier_id: str,
    extraction: TariffExtraction,
    verification: TariffVerification,
) -> None:
    actual = (
        str(row[2]),
        row[4],
        Decimal(str(row[5])),
        row[6],
        row[7],
        row[8],
        str(row[9]),
        str(row[10]) if row[10] is not None else None,
        row[13],
        row[14],
    )
    expected = (
        carrier_id,
        extraction.clause_text,
        extraction.rate_amount,
        extraction.rate_currency,
        extraction.rate_unit,
        None,
        str(extraction.effective_from),
        str(extraction.effective_to) if extraction.effective_to is not None else None,
        "VERIFIED",
        verification.reason,
    )
    _assert_reused_row("tariff clause", actual, expected)


def _json_mapping(value: object) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def persist_verified_inputs(
    dal: DAL,
    *,
    carrier_id: str,
    lane: str,
    tariff: RetainedObject,
    tariff_source_url: str,
    extraction: TariffExtraction,
    verification: TariffVerification,
    invoice: RetainedObject,
    invoice_claim: InvoiceClaim,
) -> StoredReceiptInputs:
    """Idempotently store source-derived inputs before the filing transaction."""
    if not verification.eligible:
        raise ValueError(f"tariff extraction is not eligible: {verification.reason}")
    validate_receipt_chronology(
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        invoice_claim=invoice_claim,
    )
    tenant_id = dal.tenant.tenant_id
    tariff_text = tariff.body.decode("utf-8")
    invoice_text = invoice.body.decode("utf-8")
    invoice_sha = sha256_hex(invoice.body)

    def _persist(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tariff_snapshots
                    (tenant_id, carrier_id, lane, version_label, effective_date,
                     captured_at, source_url, s3_key, source_version_id,
                     source_byte_size, doc_sha256, doc_text, headline_rate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, carrier_id, lane, captured_at) DO NOTHING
                RETURNING id, committed_at;
                """,
                (
                    tenant_id,
                    carrier_id,
                    lane,
                    extraction.effective_from.isoformat(),
                    extraction.effective_from,
                    tariff.observed_at,
                    tariff_source_url,
                    tariff.key,
                    tariff.version_id,
                    tariff.size,
                    verification.source_sha256,
                    tariff_text,
                    extraction.rate_amount,
                ),
            )
            snapshot_row = cur.fetchone()
            if snapshot_row is None:
                cur.execute(
                    """
                    SELECT id, committed_at, s3_key, source_version_id,
                           source_byte_size, doc_sha256, doc_text, source_url
                    FROM tariff_snapshots
                    WHERE tenant_id=%s AND carrier_id=%s AND lane=%s AND captured_at=%s;
                    """,
                    (tenant_id, carrier_id, lane, tariff.observed_at),
                )
                snapshot_row = cur.fetchone()
                _assert_reused_row(
                    "tariff snapshot",
                    tuple(snapshot_row[2:]),
                    (
                        tariff.key,
                        tariff.version_id,
                        tariff.size,
                        verification.source_sha256,
                        tariff_text,
                        tariff_source_url,
                    ),
                )
            snapshot_id, snapshot_committed_at = snapshot_row[:2]

            cur.execute(
                """
                INSERT INTO tariff_clauses
                    (tenant_id, carrier_id, snapshot_id, clause_ref, clause_kind,
                     clause_text, rate_amount, rate_currency, rate_unit,
                     free_time_basis, sha256, effective_from, effective_to,
                     source_locator, confidence, verification_status,
                     verification_reason)
                VALUES (%s, %s, %s, %s, 'rate', %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, 'VERIFIED', %s)
                ON CONFLICT (tenant_id, snapshot_id, sha256) DO NOTHING
                RETURNING id, committed_at;
                """,
                (
                    tenant_id,
                    carrier_id,
                    snapshot_id,
                    extraction.source_locator,
                    extraction.clause_text,
                    extraction.rate_amount,
                    extraction.rate_currency,
                    extraction.rate_unit,
                    None,
                    verification.clause_sha256,
                    extraction.effective_from,
                    extraction.effective_to,
                    extraction.source_locator,
                    extraction.confidence,
                    verification.reason,
                ),
            )
            clause_row = cur.fetchone()
            if clause_row is None:
                cur.execute(
                    """
                    SELECT id, committed_at, carrier_id, clause_ref, clause_text,
                           rate_amount, rate_currency, rate_unit, free_time_basis,
                           effective_from, effective_to, source_locator, confidence,
                           verification_status, verification_reason
                    FROM tariff_clauses
                    WHERE tenant_id=%s AND snapshot_id=%s AND sha256=%s;
                    """,
                    (tenant_id, snapshot_id, verification.clause_sha256),
                )
                clause_row = cur.fetchone()
                _assert_reused_clause(
                    clause_row,
                    carrier_id=carrier_id,
                    extraction=extraction,
                    verification=verification,
                )
            clause_id, clause_committed_at = clause_row[:2]

            extracted_invoice = {
                "invoice_no": invoice_claim.invoice_no,
                "claimed_rate": format(invoice_claim.claimed_rate, ".2f"),
                "rate_currency": invoice_claim.rate_currency,
                "rate_unit": invoice_claim.rate_unit,
                "charge_days": invoice_claim.charge_days,
                "invoice_date": invoice_claim.invoice_date.isoformat(),
            }
            cur.execute(
                """
                INSERT INTO invoices
                    (tenant_id, carrier_id, invoice_no, received_at, s3_key,
                     source_version_id, sha256, page_count, is_image_only,
                     raw_text, extracted, amount, currency, invoice_date,
                     claimed_rate, rate_unit, charge_days, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, false, %s, %s,
                        %s, %s, %s, %s, %s, %s, 'EXTRACTED')
                ON CONFLICT (tenant_id, sha256) DO NOTHING
                RETURNING id;
                """,
                (
                    tenant_id,
                    carrier_id,
                    invoice_claim.invoice_no,
                    invoice_claim.received_at,
                    invoice.key,
                    invoice.version_id,
                    invoice_sha,
                    invoice_text,
                    json.dumps(extracted_invoice, sort_keys=True),
                    invoice_claim.claimed_rate * invoice_claim.charge_days,
                    invoice_claim.rate_currency,
                    invoice_claim.invoice_date,
                    invoice_claim.claimed_rate,
                    invoice_claim.rate_unit,
                    invoice_claim.charge_days,
                ),
            )
            invoice_row = cur.fetchone()
            if invoice_row is None:
                cur.execute(
                    """
                    SELECT id, s3_key, source_version_id, sha256, raw_text,
                           claimed_rate, currency, invoice_date, rate_unit,
                           charge_days, received_at
                    FROM invoices WHERE tenant_id=%s AND sha256=%s;
                    """,
                    (tenant_id, invoice_sha),
                )
                invoice_row = cur.fetchone()
                _assert_reused_row(
                    "invoice",
                    (
                        invoice_row[1],
                        invoice_row[2],
                        invoice_row[3],
                        invoice_row[4],
                        str(invoice_row[5]),
                        invoice_row[6],
                        str(invoice_row[7]),
                        invoice_row[8],
                        int(invoice_row[9]),
                        _as_utc(invoice_row[10]),
                    ),
                    (
                        invoice.key,
                        invoice.version_id,
                        invoice_sha,
                        invoice_text,
                        str(invoice_claim.claimed_rate),
                        invoice_claim.rate_currency,
                        str(invoice_claim.invoice_date),
                        invoice_claim.rate_unit,
                        invoice_claim.charge_days,
                        _as_utc(invoice_claim.received_at),
                    ),
                )
            invoice_id = invoice_row[0]

            cur.execute(
                """
                SELECT id FROM clerk_runs
                WHERE tenant_id=%s AND invoice_id=%s
                ORDER BY created_at ASC LIMIT 1;
                """,
                (tenant_id, invoice_id),
            )
            clerk_row = cur.fetchone()
            if clerk_row is None:
                cur.execute(
                    """
                    INSERT INTO clerk_runs
                        (tenant_id, invoice_id, status, current_step, steps,
                         started_at, finished_at)
                    VALUES (%s, %s, 'COMPLETE', 7, %s, now(), now())
                    RETURNING id;
                    """,
                    (
                        tenant_id,
                        invoice_id,
                        json.dumps(
                            [
                                {"step": "extract", "status": "complete"},
                                {"step": "verify", "status": "complete"},
                                {"step": "calculate", "status": "complete"},
                            ]
                        ),
                    ),
                )
                clerk_row = cur.fetchone()

        return StoredReceiptInputs(
            snapshot_id=str(snapshot_id),
            snapshot_committed_at=str(snapshot_committed_at),
            clause_id=str(clause_id),
            clause_committed_at=str(clause_committed_at),
            invoice_id=str(invoice_id),
            clerk_run_id=str(clerk_row[0]),
        )

    return dal.run_with_retry(_persist)


def build_receipt_evidence(
    *,
    tenant_id: str,
    carrier_id: str,
    stored: StoredReceiptInputs,
    tariff: RetainedObject,
    invoice: RetainedObject,
    extraction: TariffExtraction,
    verification: TariffVerification,
    invoice_claim: InvoiceClaim,
    calculation: OverchargeCalculation,
) -> dict:
    """Build the exact, non-secret metadata copied into one evidence row."""
    return {
        "tenant_id": tenant_id,
        "carrier_id": carrier_id,
        "capture_id": stored.snapshot_id,
        "s3_bucket": tariff.bucket,
        "s3_key": tariff.key,
        "s3_version_id": tariff.version_id,
        "source_sha256": verification.source_sha256,
        "source_size": tariff.size,
        "clause_id": stored.clause_id,
        "clause_sha256": verification.clause_sha256,
        "clause_text": extraction.clause_text,
        "verification_status": "VERIFIED",
        "verification_reason": verification.reason,
        "rate_amount": format(extraction.rate_amount, ".2f"),
        "rate_currency": extraction.rate_currency,
        "rate_unit": extraction.rate_unit,
        "effective_from": extraction.effective_from.isoformat(),
        "effective_to": extraction.effective_to.isoformat() if extraction.effective_to else None,
        "observed_at": tariff.observed_at.isoformat(),
        "committed_at": stored.snapshot_committed_at,
        "clause_committed_at": stored.clause_committed_at,
        "invoice_id": stored.invoice_id,
        "invoice_s3_bucket": invoice.bucket,
        "invoice_s3_key": invoice.key,
        "invoice_s3_version_id": invoice.version_id,
        "invoice_sha256": sha256_hex(invoice.body),
        "invoice_source_size": invoice.size,
        "invoice_no": invoice_claim.invoice_no,
        "invoice_observed_at": invoice.observed_at.isoformat(),
        "invoice_received_at": invoice_claim.received_at,
        "invoice_date": invoice_claim.invoice_date.isoformat(),
        "invoice_claimed_rate": format(invoice_claim.claimed_rate, ".2f"),
        "charge_days": invoice_claim.charge_days,
        "calculation": {
            **calculation.as_dict(),
            "rate_currency": extraction.rate_currency,
            "rate_unit": extraction.rate_unit,
        },
        "recommendation": calculation.recommendation,
        "human_approval_state": "NOT_PRESSED",
    }


def file_gate1_case(
    dal: DAL,
    *,
    carrier_id: str,
    stored: StoredReceiptInputs,
    tariff: RetainedObject,
    invoice: RetainedObject,
    extraction: TariffExtraction,
    verification: TariffVerification,
    invoice_claim: InvoiceClaim,
    pin_date: date,
) -> dict:
    validate_receipt_chronology(
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        invoice_claim=invoice_claim,
        dispute_date=pin_date,
    )
    calculation = calculate_overcharge(
        recorded_rate=extraction.rate_amount,
        claimed_rate=invoice_claim.claimed_rate,
        charge_days=invoice_claim.charge_days,
    )
    if not calculation.should_file:
        return {"filed": False, "calculation": calculation.as_dict()}

    expected_calculation = {
        **calculation.as_dict(),
        "rate_currency": extraction.rate_currency,
        "rate_unit": extraction.rate_unit,
    }
    evidence_content = build_receipt_evidence(
        tenant_id=dal.tenant.tenant_id,
        carrier_id=carrier_id,
        stored=stored,
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=invoice_claim,
        calculation=calculation,
    )
    existing_sql = """
        SELECT c.id, c.finding_id, f.tariff_clause_id, f.calculation,
               f.recommendation, ce.kind, ce.source_table, ce.source_id, ce.content
        FROM cases c
        JOIN findings f ON f.tenant_id=c.tenant_id AND f.id=c.finding_id
        LEFT JOIN case_evidence ce ON ce.tenant_id=c.tenant_id AND ce.case_id=c.id
        WHERE c.tenant_id=%s AND c.invoice_id=%s
        ORDER BY ce.id;
        """

    def _existing_case_result(rows: list[tuple]) -> dict:
        row = rows[0]
        binding_matches = (
            len(rows) == 1
            and str(row[2]) == stored.clause_id
            and canonical_json_bytes(_json_mapping(row[3]))
            == canonical_json_bytes(expected_calculation)
            and row[4] == calculation.recommendation
            and row[5] == "tariff_invoice_receipt"
            and row[6] == "tariff_clauses"
            and str(row[7]) == stored.clause_id
            and canonical_json_bytes(_json_mapping(row[8]))
            == canonical_json_bytes(evidence_content)
        )
        if not binding_matches:
            raise ExistingReceiptConflictError(
                "existing case is bound to different receipt evidence"
            )
        return {
            "filed": True,
            "already_filed": True,
            "case_id": str(row[0]),
            "finding_id": str(row[1]),
            "calculation": calculation.as_dict(),
        }

    existing = dal.execute(
        existing_sql,
        (stored.invoice_id,),
        tag="receipt.idempotency",
    )
    if existing:
        return _existing_case_result(existing)

    summary = (
        f"Invoice rate {invoice_claim.claimed_rate:.2f} exceeds verified tariff "
        f"rate {extraction.rate_amount:.2f} for {invoice_claim.charge_days} days; "
        f"dispute {calculation.overcharge:.2f} {extraction.rate_currency}."
    )
    clerk_result = ClerkResult(
        verdict=VERDICT_DEFECTIVE,
        cited_rule="verified tariff rate mismatch",
        field_results=(),
        window_result=WindowResult(
            invoice_date=invoice_claim.invoice_date,
            last_charge_date=invoice_claim.invoice_date,
            days=0,
            within_30=True,
            ambiguous=False,
        ),
        summary=summary,
    )
    try:
        result = file_case(
            dal,
            invoice_id=stored.invoice_id,
            clerk_run_id=stored.clerk_run_id,
            carrier_id=carrier_id,
            pin_date=pin_date.isoformat(),
            amount=float(calculation.overcharge),
            clerk_result=clerk_result,
            evidence_items=(
                {
                    "kind": "tariff_invoice_receipt",
                    "source_table": "tariff_clauses",
                    "source_id": stored.clause_id,
                    "content": evidence_content,
                },
            ),
            receipt_binding={
                "tariff_clause_id": stored.clause_id,
                "rate_unit": extraction.rate_unit,
                "tariff_result": {
                    "verification_status": "VERIFIED",
                    "verification_reason": verification.reason,
                    "capture_id": stored.snapshot_id,
                    "source_sha256": verification.source_sha256,
                    "source_version_id": tariff.version_id,
                    "clause_id": stored.clause_id,
                },
                "calculation": expected_calculation,
            },
        )
    except psycopg.errors.UniqueViolation:
        # Another filer may win after the preflight read. The failed transaction
        # has rolled back; only treat the race as idempotent if the expected case
        # can now be read back under this tenant and invoice.
        raced = dal.execute(
            existing_sql,
            (stored.invoice_id,),
            tag="receipt.idempotency.race",
        )
        if not raced:
            raise
        return _existing_case_result(raced)
    return {"filed": True, "already_filed": False, **result, "calculation": calculation.as_dict()}

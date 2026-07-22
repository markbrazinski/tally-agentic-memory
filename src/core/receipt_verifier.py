"""Pure, fail-closed verification for a sealed evidence receipt.

The caller supplies narrow row and exact-source loaders. This module performs
no network or database I/O, and loader exception details never enter reports.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.core.receipt import (
    calculate_overcharge,
    canonical_json_bytes,
    normalize_source_text,
    parse_invoice_claim,
    prefixed_sha256,
    sha256_hex,
)

SEALED_CASE_STATES = frozenset({"FILED", "CONTESTED", "RESOLVED"})


@dataclass(frozen=True)
class LoadedSource:
    """Exact object-store response supplied to the pure verifier."""

    bucket: str
    key: str
    version_id: str
    body: bytes
    observed_at: Any


ExactSourceLoader = Callable[[str, str, str], LoadedSource]
StoredRowLoader = Callable[[str, str], Optional[Mapping[str, Any]]]


@dataclass(frozen=True)
class ReceiptCheck:
    name: str
    passed: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"name": self.name, "passed": self.passed}
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True)
class ReceiptVerificationReport:
    passed: bool
    computed_manifest_hash: str
    checks: tuple[ReceiptCheck, ...]

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(check.reason for check in self.checks if check.reason is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "computed_manifest_hash": self.computed_manifest_hash,
            "reasons": list(self.reasons),
            "checks": [check.as_dict() for check in self.checks],
        }


def _stored_hash_matches(stored: Any, computed_hex: str) -> bool:
    return isinstance(stored, str) and stored in {computed_hex, f"sha256:{computed_hex}"}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if str(value).strip() == str(parsed) else None


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _same_timestamp(left: Any, right: Any) -> bool:
    if not _present(left) or not _present(right):
        return False

    def normalized(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return text
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
        return parsed.isoformat()

    return normalized(left) == normalized(right)


def _same_json(left: Any, right: Any) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _evidence_content_hash(content: Mapping[str, Any]) -> str:
    """Reproduce the filing transaction's persisted content hash format."""
    return sha256_hex(canonical_json_bytes(dict(content)))


def _clause_contains_rate(clause_text: str, rate: Decimal | None) -> bool:
    if rate is None:
        return False
    for token in re.findall(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)(?!\d)", clause_text):
        try:
            if Decimal(token.replace(",", "")) == rate:
                return True
        except InvalidOperation:
            continue
    return False


def verify_sealed_receipt(
    *,
    manifest: Mapping[str, Any],
    stored_evidence_hash: str,
    stored_case: Mapping[str, Any],
    expected_tenant_id: str,
    expected_case_id: str,
    source_loader: ExactSourceLoader,
    capture_loader: StoredRowLoader,
    clause_loader: StoredRowLoader,
    evidence_loader: StoredRowLoader,
    invoice_loader: StoredRowLoader,
    finding_loader: StoredRowLoader,
) -> ReceiptVerificationReport:
    """Reopen and recompute every durable binding in a sealed receipt."""
    manifest_value = dict(manifest)
    computed_manifest_hash = prefixed_sha256(canonical_json_bytes(manifest_value))
    checks: list[ReceiptCheck] = []

    def record(name: str, passed: bool, reason: str) -> None:
        checks.append(ReceiptCheck(name=name, passed=passed, reason=None if passed else reason))

    def load(loader: StoredRowLoader, tenant_id: Any, row_id: Any):
        if not all(isinstance(value, str) and value.strip() for value in (tenant_id, row_id)):
            return None
        try:
            return loader(tenant_id, row_id)
        except Exception:
            return None

    record(
        "manifest_hash",
        stored_evidence_hash == computed_manifest_hash,
        "manifest_hash_mismatch",
    )
    record(
        "manifest_version",
        manifest_value.get("manifest_version") == 1,
        "unsupported_manifest_version",
    )

    manifest_tenant = manifest_value.get("tenant_id")
    manifest_case = manifest_value.get("case_id")
    manifest_invoice = manifest_value.get("invoice_id")
    manifest_finding = manifest_value.get("finding_id")
    record("tenant", manifest_tenant == expected_tenant_id, "tenant_mismatch")
    record("case", manifest_case == expected_case_id, "case_mismatch")

    record(
        "case_identity",
        str(stored_case.get("id")) == str(manifest_case)
        and str(stored_case.get("tenant_id")) == str(manifest_tenant),
        "stored_case_identity_mismatch",
    )
    record(
        "case_sealed",
        stored_case.get("state") in SEALED_CASE_STATES,
        "case_not_sealed",
    )
    record(
        "stored_manifest_version",
        stored_case.get("manifest_version") == manifest_value.get("manifest_version") == 1,
        "stored_manifest_version_mismatch",
    )
    record(
        "case_invoice",
        str(stored_case.get("invoice_id")) == str(manifest_invoice),
        "case_invoice_mismatch",
    )
    record(
        "case_finding",
        str(stored_case.get("finding_id")) == str(manifest_finding),
        "case_finding_mismatch",
    )
    record(
        "sealed_by",
        _present(stored_case.get("sealed_by"))
        and str(stored_case.get("sealed_by")) == str(manifest_value.get("approved_by")),
        "sealed_by_mismatch",
    )
    record(
        "sealed_txn_ts",
        _present(stored_case.get("sealed_txn_ts")),
        "sealed_txn_ts_missing",
    )
    record(
        "sealed_at",
        _same_timestamp(stored_case.get("sealed_at_display"), manifest_value.get("approved_at")),
        "sealed_at_mismatch",
    )

    evidence = manifest_value.get("evidence")
    evidence_rows = evidence if isinstance(evidence, list) else []
    record("evidence", bool(evidence_rows), "empty_evidence")

    evidence_rates: list[Decimal] = []
    for index, item in enumerate(evidence_rows):
        prefix = f"evidence[{index}]"
        if not isinstance(item, Mapping):
            record(f"{prefix}.shape", False, "invalid_evidence")
            continue

        evidence_id = item.get("evidence_id")
        capture_id = item.get("capture_id")
        clause_id = item.get("clause_id")
        bucket = item.get("s3_bucket")
        key = item.get("s3_key")
        version_id = item.get("s3_version_id")
        complete_reference = all(
            isinstance(value, str) and bool(value.strip())
            for value in (evidence_id, capture_id, clause_id, bucket, key, version_id)
        )
        record(f"{prefix}.reference", complete_reference, "incomplete_evidence_reference")
        record(
            f"{prefix}.source_identity",
            item.get("source_table") == "tariff_clauses"
            and str(item.get("source_id")) == str(clause_id),
            "evidence_source_identity_mismatch",
        )

        stored_evidence = load(evidence_loader, manifest_tenant, evidence_id)
        record(f"{prefix}.row", stored_evidence is not None, "evidence_row_missing")
        if stored_evidence is not None:
            evidence_identity_ok = (
                str(stored_evidence.get("id")) == str(evidence_id)
                and str(stored_evidence.get("tenant_id")) == str(manifest_tenant)
                and str(stored_evidence.get("case_id")) == str(manifest_case)
                and stored_evidence.get("kind") == item.get("kind")
                and stored_evidence.get("source_table") == item.get("source_table")
                and str(stored_evidence.get("source_id")) == str(item.get("source_id"))
            )
            record(
                f"{prefix}.row_identity",
                evidence_identity_ok,
                "evidence_row_identity_mismatch",
            )
            record(
                f"{prefix}.row_sealed",
                stored_evidence.get("sealed") is True,
                "evidence_row_not_sealed",
            )
            stored_content = stored_evidence.get("content")
            manifest_content = item.get("content")
            content_ok = (
                isinstance(stored_content, Mapping)
                and isinstance(manifest_content, Mapping)
                and _same_json(stored_content, manifest_content)
            )
            record(f"{prefix}.content", content_ok, "evidence_content_mismatch")
            flattened_binding_ok = isinstance(manifest_content, Mapping) and all(
                key in item and _same_json(item.get(key), value)
                for key, value in manifest_content.items()
            )
            record(
                f"{prefix}.flattened_binding",
                flattened_binding_ok,
                "evidence_flattened_binding_mismatch",
            )
            if isinstance(stored_content, Mapping):
                content_hex = _evidence_content_hash(stored_content)
                content_hash_ok = _stored_hash_matches(
                    stored_evidence.get("content_sha256"), content_hex
                ) and _stored_hash_matches(item.get("content_sha256"), content_hex)
            else:
                content_hash_ok = False
            record(
                f"{prefix}.content_hash",
                content_hash_ok,
                "evidence_content_hash_mismatch",
            )

        capture = load(capture_loader, manifest_tenant, capture_id)
        record(f"{prefix}.capture", capture is not None, "capture_missing")
        if capture is not None:
            record(
                f"{prefix}.capture_identity",
                str(capture.get("id")) == str(capture_id)
                and str(capture.get("tenant_id")) == str(manifest_tenant)
                and str(capture.get("carrier_id")) == str(item.get("carrier_id")),
                "capture_identity_mismatch",
            )
            record(
                f"{prefix}.capture_key",
                capture.get("s3_key") == key,
                "capture_source_key_mismatch",
            )
            record(
                f"{prefix}.capture_version",
                capture.get("source_version_id") == version_id,
                "capture_source_version_mismatch",
            )
            record(
                f"{prefix}.capture_hash",
                capture.get("doc_sha256") == item.get("source_sha256"),
                "capture_source_hash_mismatch",
            )
            record(
                f"{prefix}.capture_size",
                capture.get("source_byte_size") == item.get("source_size"),
                "capture_source_size_mismatch",
            )
            record(
                f"{prefix}.capture_observed",
                _same_timestamp(capture.get("captured_at"), item.get("observed_at")),
                "capture_observed_at_mismatch",
            )
            record(
                f"{prefix}.capture_committed",
                _same_timestamp(capture.get("committed_at"), item.get("committed_at")),
                "capture_committed_at_mismatch",
            )

        source: LoadedSource | None = None
        if complete_reference:
            try:
                loaded = source_loader(str(bucket), str(key), str(version_id))
                if isinstance(loaded, LoadedSource):
                    source = loaded
            except Exception:
                source = None
        record(f"{prefix}.source_fetch", source is not None, "source_version_fetch_failed")
        if source is not None:
            record(
                f"{prefix}.source_response_identity",
                (source.bucket, source.key, source.version_id) == (bucket, key, version_id),
                "source_response_identity_mismatch",
            )
            record(
                f"{prefix}.source_observed",
                _same_timestamp(source.observed_at, item.get("observed_at")),
                "source_observed_at_mismatch",
            )
            record(
                f"{prefix}.source_hash",
                _stored_hash_matches(item.get("source_sha256"), sha256_hex(source.body)),
                "source_hash_mismatch",
            )
            record(
                f"{prefix}.source_size",
                len(source.body) == item.get("source_size"),
                "source_size_mismatch",
            )

        clause = load(clause_loader, manifest_tenant, clause_id)
        record(f"{prefix}.clause", clause is not None, "clause_missing")
        if clause is None:
            continue
        record(
            f"{prefix}.clause_identity",
            str(clause.get("id")) == str(clause_id)
            and str(clause.get("tenant_id")) == str(manifest_tenant)
            and str(clause.get("snapshot_id")) == str(capture_id),
            "clause_identity_mismatch",
        )
        record(
            f"{prefix}.clause_committed",
            _same_timestamp(clause.get("committed_at"), item.get("clause_committed_at")),
            "clause_committed_at_mismatch",
        )
        record(
            f"{prefix}.clause_verification",
            clause.get("verification_status") == item.get("verification_status") == "VERIFIED"
            and _present(clause.get("verification_reason"))
            and clause.get("verification_reason") == item.get("verification_reason"),
            "clause_verification_mismatch",
        )
        clause_text = clause.get("clause_text")
        valid_clause_text = isinstance(clause_text, str) and bool(clause_text.strip())
        if valid_clause_text:
            clause_hex = sha256_hex(clause_text.encode("utf-8"))
            clause_hash_ok = _stored_hash_matches(item.get("clause_sha256"), clause_hex)
            clause_hash_ok = clause_hash_ok and _stored_hash_matches(
                clause.get("sha256"), clause_hex
            )
        else:
            clause_hash_ok = False
        record(f"{prefix}.clause_hash", clause_hash_ok, "clause_hash_mismatch")

        if valid_clause_text and source is not None:
            try:
                source_contains_clause = normalize_source_text(
                    clause_text
                ) in normalize_source_text(source.body.decode("utf-8"))
            except UnicodeDecodeError:
                source_contains_clause = False
        else:
            source_contains_clause = False
        record(
            f"{prefix}.clause_source",
            source_contains_clause,
            "clause_absent_from_source",
        )

        evidence_rate = _decimal(item.get("rate_amount"))
        clause_rate = _decimal(clause.get("rate_amount"))
        rate_ok = evidence_rate is not None and clause_rate == evidence_rate
        rate_ok = rate_ok and clause.get("rate_currency") == item.get("rate_currency")
        rate_ok = rate_ok and clause.get("rate_unit") == item.get("rate_unit")
        rate_ok = rate_ok and str(clause.get("effective_from")) == str(item.get("effective_from"))
        rate_ok = rate_ok and str(clause.get("effective_to")) == str(item.get("effective_to"))
        record(f"{prefix}.clause_rate", rate_ok, "clause_rate_mismatch")
        record(
            f"{prefix}.clause_rate_text",
            valid_clause_text and _clause_contains_rate(clause_text, evidence_rate),
            "rate_absent_from_clause",
        )
        if rate_ok and evidence_rate is not None:
            evidence_rates.append(evidence_rate)

    invoice = load(invoice_loader, manifest_tenant, manifest_invoice)
    record("invoice", invoice is not None, "invoice_missing")
    calculation = manifest_value.get("calculation")
    calculation_value = calculation if isinstance(calculation, Mapping) else {}
    if invoice is not None:
        record(
            "invoice_identity",
            str(invoice.get("id")) == str(manifest_invoice)
            and str(invoice.get("tenant_id")) == str(manifest_tenant),
            "invoice_identity_mismatch",
        )
        invoice_evidence = (
            evidence_rows[0] if evidence_rows and isinstance(evidence_rows[0], Mapping) else {}
        )
        invoice_reference_ok = (
            str(invoice_evidence.get("invoice_id")) == str(manifest_invoice)
            and invoice.get("s3_key") == invoice_evidence.get("invoice_s3_key")
            and invoice.get("source_version_id") == invoice_evidence.get("invoice_s3_version_id")
            and invoice.get("sha256") == invoice_evidence.get("invoice_sha256")
        )
        record("invoice_source_identity", invoice_reference_ok, "invoice_source_identity_mismatch")
        record(
            "invoice_received_at",
            _same_timestamp(
                invoice.get("received_at"), invoice_evidence.get("invoice_received_at")
            ),
            "invoice_received_at_mismatch",
        )
        record(
            "invoice_date",
            str(invoice.get("invoice_date")) == str(invoice_evidence.get("invoice_date")),
            "invoice_date_mismatch",
        )

        invoice_source: LoadedSource | None = None
        invoice_bucket = invoice_evidence.get("invoice_s3_bucket")
        invoice_key = invoice_evidence.get("invoice_s3_key")
        invoice_version = invoice_evidence.get("invoice_s3_version_id")
        if all(
            isinstance(value, str) and value.strip()
            for value in (invoice_bucket, invoice_key, invoice_version)
        ):
            try:
                loaded = source_loader(invoice_bucket, invoice_key, invoice_version)
                if isinstance(loaded, LoadedSource):
                    invoice_source = loaded
            except Exception:
                invoice_source = None
        record("invoice_source_fetch", invoice_source is not None, "invoice_source_fetch_failed")
        if invoice_source is not None:
            record(
                "invoice_source_response_identity",
                (invoice_source.bucket, invoice_source.key, invoice_source.version_id)
                == (invoice_bucket, invoice_key, invoice_version),
                "invoice_source_response_identity_mismatch",
            )
            record(
                "invoice_source_observed",
                _same_timestamp(
                    invoice_source.observed_at, invoice_evidence.get("invoice_observed_at")
                ),
                "invoice_source_observed_at_mismatch",
            )
            invoice_hex = sha256_hex(invoice_source.body)
            invoice_hash_ok = _stored_hash_matches(invoice.get("sha256"), invoice_hex)
            invoice_hash_ok = invoice_hash_ok and _stored_hash_matches(
                invoice_evidence.get("invoice_sha256"), invoice_hex
            )
            record("invoice_source_hash", invoice_hash_ok, "invoice_source_hash_mismatch")
            record(
                "invoice_source_size",
                len(invoice_source.body) == invoice_evidence.get("invoice_source_size"),
                "invoice_source_size_mismatch",
            )
            try:
                parsed_claim = parse_invoice_claim(invoice_source.body)
            except ValueError:
                parsed_claim = None
            source_claim_ok = parsed_claim is not None and (
                parsed_claim.invoice_no == invoice.get("invoice_no")
                and parsed_claim.invoice_no == invoice_evidence.get("invoice_no")
                and parsed_claim.claimed_rate == _decimal(invoice.get("claimed_rate"))
                and parsed_claim.claimed_rate
                == _decimal(invoice_evidence.get("invoice_claimed_rate"))
                and parsed_claim.rate_currency == invoice.get("rate_currency")
                and parsed_claim.rate_unit == invoice.get("rate_unit")
                and parsed_claim.charge_days == _integer(invoice.get("charge_days"))
                and parsed_claim.charge_days == _integer(invoice_evidence.get("charge_days"))
                and str(parsed_claim.invoice_date) == str(invoice.get("invoice_date"))
                and _same_timestamp(parsed_claim.received_at, invoice.get("received_at"))
            )
            record(
                "invoice_source_claim",
                source_claim_ok,
                "invoice_source_claim_mismatch",
            )

        claimed_rate = _decimal(invoice.get("claimed_rate"))
        charge_days = _integer(invoice.get("charge_days"))
        invoice_claim_ok = (
            claimed_rate is not None
            and charge_days is not None
            and charge_days > 0
            and invoice.get("rate_currency") == calculation_value.get("rate_currency")
            and invoice.get("rate_unit") == calculation_value.get("rate_unit")
            and claimed_rate == _decimal(calculation_value.get("claimed_rate"))
            and charge_days == _integer(calculation_value.get("charge_days"))
        )
        record("invoice_claim", invoice_claim_ok, "invoice_claim_mismatch")

        rates_consistent = bool(evidence_rates) and len(set(evidence_rates)) == 1
        if (
            rates_consistent
            and invoice_claim_ok
            and claimed_rate is not None
            and charge_days is not None
        ):
            recomputed = calculate_overcharge(
                recorded_rate=evidence_rates[0],
                claimed_rate=claimed_rate,
                charge_days=charge_days,
            ).as_dict()
            calculation_ok = (
                _decimal(calculation_value.get("recorded_rate"))
                == _decimal(recomputed["recorded_rate"])
                and _decimal(calculation_value.get("overcharge"))
                == _decimal(recomputed["overcharge"])
                and calculation_value.get("recommendation") == recomputed["recommendation"]
            )
        else:
            calculation_ok = False
        record("calculation", calculation_ok, "calculation_mismatch")

    finding = load(finding_loader, manifest_tenant, manifest_finding)
    record("finding", finding is not None, "finding_missing")
    if finding is not None:
        record(
            "finding_identity",
            str(finding.get("id")) == str(manifest_finding)
            and str(finding.get("tenant_id")) == str(manifest_tenant)
            and str(finding.get("invoice_id")) == str(manifest_invoice),
            "finding_identity_mismatch",
        )
        record(
            "human_approval",
            finding.get("human_approval_state") == "APPROVED",
            "human_approval_missing",
        )
        finding_binding_ok = finding.get("recommendation") == manifest_value.get("recommendation")
        finding_binding_ok = finding_binding_ok and _same_json(
            finding.get("calculation"), calculation_value
        )
        record("finding_binding", finding_binding_ok, "finding_binding_mismatch")

    return ReceiptVerificationReport(
        passed=all(check.passed for check in checks),
        computed_manifest_hash=computed_manifest_hash,
        checks=tuple(checks),
    )

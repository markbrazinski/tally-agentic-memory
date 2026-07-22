"""Pure validation and projection for a sealed case returned through MCP.

The Managed MCP response is untrusted boundary data.  This module binds it
back to the canonical Gate 1 receipt before any of it is exposed as contest
memory.  It deliberately returns only the fields needed to answer a later
challenge; storage locations and raw retained source bodies are excluded.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from src.core.receipt import canonical_json_bytes, prefixed_sha256


class SealedMemoryValidationError(ValueError):
    """The MCP result is not a complete, internally consistent sealed receipt."""


SEALED_CASE_STATES = frozenset({"FILED", "CONTESTED", "RESOLVED"})


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SealedMemoryValidationError(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise SealedMemoryValidationError(f"{field} must be a JSON object")
    return dict(value)


def _canonical_uuid(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SealedMemoryValidationError(f"{field} must be a UUID") from exc


def _required_text(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if value is None or not str(value).strip():
        raise SealedMemoryValidationError(f"{field} is required")
    return str(value)


def _timestamp(value: Any, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SealedMemoryValidationError(f"{field} must be a timestamp") from exc
    if parsed.tzinfo is None:
        raise SealedMemoryValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class SealedCaseMemory:
    contest_id: str
    contest_status: str
    case_id: str
    invoice_id: str
    invoice_no: str
    finding_id: str
    current_state: str
    recommendation: str
    recorded_rate: str
    claimed_rate: str
    rate_currency: str
    rate_unit: str
    charge_days: int
    clause_id: str
    clause_text: str
    capture_id: str
    source_version_id: str
    source_sha256: str
    invoice_source_version_id: str
    invoice_source_sha256: str
    approved_by: str
    sealed_at: str
    sealed_txn_ts: str
    manifest_version: int
    evidence_hash: str
    evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "contest_id": self.contest_id,
            "contest_status": self.contest_status,
            "case_id": self.case_id,
            "invoice_id": self.invoice_id,
            "invoice_no": self.invoice_no,
            "finding_id": self.finding_id,
            "current_state": self.current_state,
            "recommendation": self.recommendation,
            "recorded_rate": self.recorded_rate,
            "claimed_rate": self.claimed_rate,
            "rate_currency": self.rate_currency,
            "rate_unit": self.rate_unit,
            "charge_days": self.charge_days,
            "clause_id": self.clause_id,
            "clause_text": self.clause_text,
            "evidence_version": {
                "tariff": {
                    "capture_id": self.capture_id,
                    "source_version_id": self.source_version_id,
                    "source_sha256": self.source_sha256,
                },
                "invoice": {
                    "source_version_id": self.invoice_source_version_id,
                    "source_sha256": self.invoice_source_sha256,
                },
            },
            "approved_by": self.approved_by,
            "sealed_at": self.sealed_at,
            "sealed_txn_ts": self.sealed_txn_ts,
            "manifest_version": self.manifest_version,
            "evidence_hash": self.evidence_hash,
            "evidence_ids": list(self.evidence_ids),
        }


def validate_sealed_case_memory(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_tenant_id: str,
    expected_case_id: str,
    expected_contest_id: str,
) -> SealedCaseMemory | None:
    """Validate MCP rows and return a minimal contest-memory projection.

    Zero rows is a clean not-found result.  A filed case is accepted only when
    every returned evidence row is sealed and the returned evidence set exactly
    matches the canonical manifest whose stored hash is re-computed here.
    """
    if not rows:
        return None

    tenant_id = _canonical_uuid(expected_tenant_id, field="expected_tenant_id")
    case_id = _canonical_uuid(expected_case_id, field="expected_case_id")
    contest_id = _canonical_uuid(expected_contest_id, field="expected_contest_id")
    normalized = [dict(row) for row in rows]
    first = normalized[0]

    if _canonical_uuid(first.get("tenant_id"), field="tenant_id") != tenant_id:
        raise SealedMemoryValidationError("returned tenant does not match trusted context")
    if _canonical_uuid(first.get("case_id"), field="case_id") != case_id:
        raise SealedMemoryValidationError("returned case does not match requested case")
    if _canonical_uuid(first.get("contest_id"), field="contest_id") != contest_id:
        raise SealedMemoryValidationError("returned contest does not match trusted context")
    current_state = str(first.get("current_state"))
    if current_state not in SEALED_CASE_STATES:
        raise SealedMemoryValidationError("case has no filed seal")
    if first.get("manifest_version") != 1:
        raise SealedMemoryValidationError("unsupported manifest version")

    manifest = _mapping(first.get("evidence_manifest"), field="evidence_manifest")
    evidence_hash = _required_text(first, "evidence_hash")
    if prefixed_sha256(canonical_json_bytes(manifest)) != evidence_hash:
        raise SealedMemoryValidationError("manifest hash mismatch")
    if manifest.get("manifest_version") != 1:
        raise SealedMemoryValidationError("manifest version mismatch")
    if _canonical_uuid(manifest.get("tenant_id"), field="manifest tenant_id") != tenant_id:
        raise SealedMemoryValidationError("manifest tenant mismatch")
    if _canonical_uuid(manifest.get("case_id"), field="manifest case_id") != case_id:
        raise SealedMemoryValidationError("manifest case mismatch")

    invoice_id = _canonical_uuid(first.get("invoice_id"), field="invoice_id")
    finding_id = _canonical_uuid(first.get("finding_id"), field="finding_id")
    if _canonical_uuid(manifest.get("invoice_id"), field="manifest invoice_id") != invoice_id:
        raise SealedMemoryValidationError("manifest invoice mismatch")
    if _canonical_uuid(manifest.get("finding_id"), field="manifest finding_id") != finding_id:
        raise SealedMemoryValidationError("manifest finding mismatch")

    approved_by = _canonical_uuid(first.get("approved_by"), field="approved_by")
    if _canonical_uuid(manifest.get("approved_by"), field="manifest approved_by") != approved_by:
        raise SealedMemoryValidationError("manifest approver mismatch")
    sealed_at = _timestamp(first.get("sealed_at"), field="sealed_at")
    if _timestamp(manifest.get("approved_at"), field="manifest approved_at") != sealed_at:
        raise SealedMemoryValidationError("manifest approval time mismatch")

    manifest_items_value = manifest.get("evidence")
    if not isinstance(manifest_items_value, list) or not manifest_items_value:
        raise SealedMemoryValidationError("manifest has no evidence")
    manifest_items: dict[str, dict[str, Any]] = {}
    for value in manifest_items_value:
        item = _mapping(value, field="manifest evidence")
        evidence_id = _canonical_uuid(item.get("evidence_id"), field="manifest evidence_id")
        if evidence_id in manifest_items:
            raise SealedMemoryValidationError("manifest contains duplicate evidence IDs")
        manifest_items[evidence_id] = item

    returned_ids: set[str] = set()
    for row in normalized:
        for field in (
            "tenant_id",
            "case_id",
            "invoice_id",
            "finding_id",
            "current_state",
            "manifest_version",
            "evidence_hash",
            "approved_by",
            "sealed_at",
            "sealed_txn_ts",
            "contest_id",
            "contest_status",
            "current_invoice_no",
            "current_invoice_version_id",
            "current_invoice_sha256",
            "current_invoice_claimed_rate",
            "current_invoice_currency",
            "current_invoice_rate_unit",
            "current_invoice_charge_days",
            "current_recommendation",
            "current_calculation",
            "current_approval_state",
            "current_clause_id",
            "current_recorded_rate",
            "current_finding_claimed_rate",
            "current_finding_rate_unit",
            "current_finding_charge_days",
        ):
            if row.get(field) != first.get(field):
                raise SealedMemoryValidationError(f"inconsistent case field across rows: {field}")
        evidence_id = _canonical_uuid(row.get("evidence_id"), field="evidence_id")
        if evidence_id in returned_ids:
            raise SealedMemoryValidationError("duplicate returned evidence ID")
        returned_ids.add(evidence_id)
        item = manifest_items.get(evidence_id)
        if item is None:
            raise SealedMemoryValidationError("returned evidence is absent from manifest")
        if row.get("evidence_sealed") is not True:
            raise SealedMemoryValidationError("returned evidence is not sealed")
        content = _mapping(row.get("evidence_content"), field="evidence_content")
        computed_content_hash = prefixed_sha256(canonical_json_bytes(content)).removeprefix(
            "sha256:"
        )
        if str(row.get("content_sha256")) not in {
            computed_content_hash,
            f"sha256:{computed_content_hash}",
        }:
            raise SealedMemoryValidationError("evidence content hash mismatch")
        if canonical_json_bytes(item.get("content")) != canonical_json_bytes(content):
            raise SealedMemoryValidationError("evidence content does not match manifest")
        for field, value in content.items():
            if field not in item or canonical_json_bytes(item[field]) != canonical_json_bytes(
                value
            ):
                raise SealedMemoryValidationError(
                    f"flattened evidence {field} does not match sealed content"
                )
        comparisons = {
            "kind": row.get("evidence_kind"),
            "source_table": row.get("source_table"),
            "source_id": str(row.get("source_id")),
            "content_sha256": row.get("content_sha256"),
        }
        for field, returned_value in comparisons.items():
            if str(item.get(field)) != str(returned_value):
                raise SealedMemoryValidationError(f"evidence {field} does not match manifest")

    if returned_ids != set(manifest_items):
        raise SealedMemoryValidationError("returned evidence set does not match manifest")

    tariff_item = next(
        (
            item
            for item in manifest_items.values()
            if item.get("clause_id") and item.get("s3_version_id") and item.get("source_sha256")
        ),
        None,
    )
    if tariff_item is None:
        raise SealedMemoryValidationError("manifest has no exact tariff evidence version")
    calculation = _mapping(manifest.get("calculation"), field="calculation")

    if tariff_item.get("source_table") != "tariff_clauses":
        raise SealedMemoryValidationError("tariff evidence source table is invalid")
    if _canonical_uuid(tariff_item.get("source_id"), field="tariff source_id") != _canonical_uuid(
        tariff_item.get("clause_id"), field="clause_id"
    ):
        raise SealedMemoryValidationError("tariff evidence source does not match clause")
    if not tariff_item.get("invoice_s3_version_id") or not tariff_item.get("invoice_sha256"):
        raise SealedMemoryValidationError("manifest has no exact invoice evidence version")

    if first.get("current_approval_state") != "APPROVED":
        raise SealedMemoryValidationError("current finding approval is not approved")
    if str(first.get("current_invoice_no")) != str(tariff_item.get("invoice_no")):
        raise SealedMemoryValidationError("current invoice does not match manifest")
    if str(first.get("current_invoice_version_id")) != str(
        tariff_item.get("invoice_s3_version_id")
    ) or str(first.get("current_invoice_sha256")) != str(tariff_item.get("invoice_sha256")):
        raise SealedMemoryValidationError("current invoice source does not match manifest")
    if first.get("current_recommendation") != manifest.get("recommendation"):
        raise SealedMemoryValidationError("current finding recommendation does not match manifest")
    if canonical_json_bytes(
        _mapping(first.get("current_calculation"), field="current_calculation")
    ) != canonical_json_bytes(calculation):
        raise SealedMemoryValidationError("current finding calculation does not match manifest")
    if _canonical_uuid(
        first.get("current_clause_id"), field="current_clause_id"
    ) != _canonical_uuid(tariff_item.get("clause_id"), field="clause_id"):
        raise SealedMemoryValidationError("current finding clause does not match manifest")
    decimal_pairs = (
        (first.get("current_recorded_rate"), calculation.get("recorded_rate")),
        (first.get("current_finding_claimed_rate"), calculation.get("claimed_rate")),
        (first.get("current_invoice_claimed_rate"), tariff_item.get("invoice_claimed_rate")),
    )
    if not all(_same_decimal(left, right) for left, right in decimal_pairs):
        raise SealedMemoryValidationError("current rate values do not match manifest")
    if str(first.get("current_invoice_currency")) != str(tariff_item.get("rate_currency")):
        raise SealedMemoryValidationError("current invoice currency does not match manifest")
    if str(first.get("current_invoice_rate_unit")) != str(tariff_item.get("rate_unit")) or str(
        first.get("current_finding_rate_unit")
    ) != str(tariff_item.get("rate_unit")):
        raise SealedMemoryValidationError("current rate unit does not match manifest")

    try:
        charge_days = int(tariff_item.get("charge_days"))
    except (TypeError, ValueError) as exc:
        raise SealedMemoryValidationError("charge_days must be an integer") from exc
    try:
        current_invoice_days = int(first.get("current_invoice_charge_days"))
        current_finding_days = int(first.get("current_finding_charge_days"))
    except (TypeError, ValueError) as exc:
        raise SealedMemoryValidationError("current charge days must be integers") from exc
    if current_invoice_days != charge_days or current_finding_days != charge_days:
        raise SealedMemoryValidationError("current charge days do not match manifest")

    return SealedCaseMemory(
        contest_id=contest_id,
        contest_status=_required_text(first, "contest_status"),
        case_id=case_id,
        invoice_id=invoice_id,
        invoice_no=_required_text(tariff_item, "invoice_no"),
        finding_id=finding_id,
        current_state=current_state,
        recommendation=_required_text(manifest, "recommendation"),
        recorded_rate=_required_text(calculation, "recorded_rate"),
        claimed_rate=_required_text(calculation, "claimed_rate"),
        rate_currency=_required_text(tariff_item, "rate_currency"),
        rate_unit=_required_text(tariff_item, "rate_unit"),
        charge_days=charge_days,
        clause_id=_canonical_uuid(tariff_item.get("clause_id"), field="clause_id"),
        clause_text=_required_text(tariff_item, "clause_text"),
        capture_id=_canonical_uuid(tariff_item.get("capture_id"), field="capture_id"),
        source_version_id=_required_text(tariff_item, "s3_version_id"),
        source_sha256=_required_text(tariff_item, "source_sha256"),
        invoice_source_version_id=_required_text(tariff_item, "invoice_s3_version_id"),
        invoice_source_sha256=_required_text(tariff_item, "invoice_sha256"),
        approved_by=approved_by,
        sealed_at=sealed_at,
        sealed_txn_ts=_required_text(first, "sealed_txn_ts"),
        manifest_version=1,
        evidence_hash=evidence_hash,
        evidence_ids=tuple(sorted(returned_ids)),
    )

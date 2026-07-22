from __future__ import annotations

from copy import deepcopy

import pytest

from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.core.sealed_memory import SealedMemoryValidationError, validate_sealed_case_memory

TENANT_ID = "10000000-0000-4000-8000-000000000002"
CASE_ID = "30000000-0000-4000-8000-000000000001"
INVOICE_ID = "30000000-0000-4000-8000-000000000002"
FINDING_ID = "30000000-0000-4000-8000-000000000003"
EVIDENCE_ID = "30000000-0000-4000-8000-000000000004"
CLAUSE_ID = "30000000-0000-4000-8000-000000000006"
CAPTURE_ID = "30000000-0000-4000-8000-000000000007"
APPROVER_ID = "30000000-0000-4000-8000-000000000008"
CONTEST_ID = "30000000-0000-4000-8000-000000000009"


def sealed_rows() -> list[dict]:
    content = {
        "clause_id": CLAUSE_ID,
        "capture_id": CAPTURE_ID,
        "clause_text": "Synthetic free-time rate is USD 250.00 per day.",
        "s3_version_id": "fixture-source-version-001",
        "source_sha256": "b" * 64,
        "invoice_s3_version_id": "fixture-invoice-version-001",
        "invoice_sha256": "c" * 64,
        "invoice_no": "SYNTHETIC-INV-001",
        "invoice_claimed_rate": "350.00",
        "rate_amount": "250.00",
        "rate_currency": "USD",
        "rate_unit": "per_day",
        "charge_days": 7,
        "classification": "synthetic demonstration data",
    }
    content_sha256 = prefixed_sha256(canonical_json_bytes(content)).removeprefix("sha256:")
    item = {
        **content,
        "evidence_id": EVIDENCE_ID,
        "kind": "tariff_invoice_receipt",
        "source_table": "tariff_clauses",
        "source_id": CLAUSE_ID,
        "content_sha256": content_sha256,
        "content": content,
    }
    manifest = {
        "manifest_version": 1,
        "tenant_id": TENANT_ID,
        "case_id": CASE_ID,
        "invoice_id": INVOICE_ID,
        "finding_id": FINDING_ID,
        "evidence": [item],
        "calculation": {
            "recorded_rate": "250.00",
            "claimed_rate": "350.00",
            "overcharge": "700.00",
        },
        "recommendation": "FILE",
        "approved_by": APPROVER_ID,
        "approved_at": "2026-07-20T18:00:00Z",
    }
    return [
        {
            "tenant_id": TENANT_ID,
            "case_id": CASE_ID,
            "contest_id": CONTEST_ID,
            "contest_status": "OPEN",
            "invoice_id": INVOICE_ID,
            "finding_id": FINDING_ID,
            "current_state": "FILED",
            "manifest_version": 1,
            "evidence_manifest": manifest,
            "evidence_hash": prefixed_sha256(canonical_json_bytes(manifest)),
            "approved_by": APPROVER_ID,
            "sealed_at": "2026-07-20T18:00:00Z",
            "sealed_txn_ts": "1753034400.0000000000",
            "current_invoice_no": "SYNTHETIC-INV-001",
            "current_invoice_version_id": "fixture-invoice-version-001",
            "current_invoice_sha256": "c" * 64,
            "current_invoice_claimed_rate": "350.00",
            "current_invoice_currency": "USD",
            "current_invoice_rate_unit": "per_day",
            "current_invoice_charge_days": 7,
            "current_recommendation": "FILE",
            "current_calculation": manifest["calculation"],
            "current_approval_state": "APPROVED",
            "current_clause_id": CLAUSE_ID,
            "current_recorded_rate": "250.00",
            "current_finding_claimed_rate": "350.00",
            "current_finding_rate_unit": "per_day",
            "current_finding_charge_days": 7,
            "evidence_id": EVIDENCE_ID,
            "evidence_kind": "tariff_invoice_receipt",
            "source_table": "tariff_clauses",
            "source_id": CLAUSE_ID,
            "evidence_content": deepcopy(content),
            "content_sha256": content_sha256,
            "evidence_sealed": True,
        }
    ]


def test_correct_sealed_memory_is_minimally_projected_and_exact_version_bound():
    memory = validate_sealed_case_memory(
        sealed_rows(),
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_contest_id=CONTEST_ID,
    )

    assert memory is not None
    result = memory.as_dict()
    assert result["case_id"] == CASE_ID
    assert result["evidence_version"]["tariff"]["source_version_id"] == "fixture-source-version-001"
    assert (
        result["evidence_version"]["invoice"]["source_version_id"] == "fixture-invoice-version-001"
    )
    assert result["recorded_rate"] == "250.00"
    assert result["clause_id"] == CLAUSE_ID
    assert "content" not in result
    assert "s3_bucket" not in result
    assert "s3_key" not in result


def test_zero_rows_is_clean_not_found():
    assert (
        validate_sealed_case_memory(
            [],
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )
        is None
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows[0].update(tenant_id="20000000-0000-4000-8000-000000000002"), "tenant"),
        (lambda rows: rows[0].update(current_state="ANALYZED"), "no filed seal"),
        (lambda rows: rows[0].update(manifest_version=2), "version"),
        (lambda rows: rows[0].update(evidence_hash="sha256:" + "0" * 64), "hash"),
        (lambda rows: rows[0].update(evidence_sealed=False), "not sealed"),
    ],
)
def test_boundary_mismatch_fails_closed(mutate, message):
    rows = sealed_rows()
    mutate(rows)

    with pytest.raises(SealedMemoryValidationError, match=message):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


def test_returned_evidence_identifier_must_match_sealed_manifest():
    rows = sealed_rows()
    rows[0]["source_id"] = "30000000-0000-4000-8000-000000000099"

    with pytest.raises(SealedMemoryValidationError, match="source_id"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


def test_missing_exact_source_version_fails_closed():
    rows = deepcopy(sealed_rows())
    item = rows[0]["evidence_manifest"]["evidence"][0]
    del item["s3_version_id"]
    del item["content"]["s3_version_id"]
    del rows[0]["evidence_content"]["s3_version_id"]
    content_hash = prefixed_sha256(canonical_json_bytes(rows[0]["evidence_content"])).removeprefix(
        "sha256:"
    )
    rows[0]["content_sha256"] = content_hash
    item["content_sha256"] = content_hash
    rows[0]["evidence_hash"] = prefixed_sha256(canonical_json_bytes(rows[0]["evidence_manifest"]))

    with pytest.raises(SealedMemoryValidationError, match="exact tariff evidence version"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


def test_manifest_evidence_set_must_equal_returned_rows():
    rows = sealed_rows()
    extra = deepcopy(rows[0]["evidence_manifest"]["evidence"][0])
    extra["evidence_id"] = "30000000-0000-4000-8000-000000000099"
    rows[0]["evidence_manifest"]["evidence"].append(extra)
    rows[0]["evidence_hash"] = prefixed_sha256(canonical_json_bytes(rows[0]["evidence_manifest"]))

    with pytest.raises(SealedMemoryValidationError, match="evidence set"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


@pytest.mark.parametrize("state", ["CONTESTED", "RESOLVED"])
def test_post_filing_states_retain_verified_memory(state):
    rows = sealed_rows()
    rows[0]["current_state"] = state
    memory = validate_sealed_case_memory(
        rows,
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_contest_id=CONTEST_ID,
    )
    assert memory is not None
    assert memory.current_state == state


def test_equivalent_timestamp_offsets_match_manifest_instant():
    rows = sealed_rows()
    rows[0]["sealed_at"] = "2026-07-20 11:00:00-07:00"
    memory = validate_sealed_case_memory(
        rows,
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_contest_id=CONTEST_ID,
    )
    assert memory is not None
    assert memory.sealed_at == "2026-07-20T18:00:00Z"


def test_current_finding_or_invoice_divergence_fails_closed():
    rows = sealed_rows()
    rows[0]["current_invoice_version_id"] = "changed-version"
    with pytest.raises(SealedMemoryValidationError, match="current invoice source"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


def test_current_evidence_content_is_rehashed():
    rows = sealed_rows()
    rows[0]["evidence_content"]["rate_amount"] = "999.00"
    with pytest.raises(SealedMemoryValidationError, match="content hash"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )


def test_flattened_exact_version_cannot_diverge_from_nested_sealed_content():
    rows = sealed_rows()
    rows[0]["evidence_manifest"]["evidence"][0]["s3_version_id"] = "forged-version"
    rows[0]["evidence_hash"] = prefixed_sha256(canonical_json_bytes(rows[0]["evidence_manifest"]))
    with pytest.raises(SealedMemoryValidationError, match="flattened evidence s3_version_id"):
        validate_sealed_case_memory(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_contest_id=CONTEST_ID,
        )

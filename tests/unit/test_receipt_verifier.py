from __future__ import annotations

from copy import deepcopy

import pytest

from src.core.receipt import canonical_json_bytes, prefixed_sha256, sha256_hex
from src.core.receipt_verifier import LoadedSource, verify_sealed_receipt

TENANT_ID = "tenant-fixture-001"
CASE_ID = "case-fixture-0142"
FINDING_ID = "finding-fixture-001"
EVIDENCE_ID = "evidence-fixture-001"
CAPTURE_ID = "capture-fixture-001"
CLAUSE_ID = "clause-fixture-004"
INVOICE_ID = "invoice-fixture-084213"
CARRIER_ID = "carrier-fixture-001"
TARIFF_KEY = "fixtures/northstar/tariff.txt"
INVOICE_KEY = "fixtures/northstar/invoice.json"
TARIFF_BYTES = (
    b"Section 4 - Import demurrage: The rate is USD $250.00 per day for each "
    b"chargeable day after free time."
)
INVOICE_BYTES = (
    b'{"invoice_no":"INV-NOL-084213","claimed_rate":{"amount":"350.00",'
    b'"currency":"USD","unit":"per_day"},"charge_days":7,'
    b'"invoice_date":"2026-07-10","received_at":"2026-07-10T16:05:00+00:00"}'
)
CLAUSE_TEXT = TARIFF_BYTES.decode("utf-8")
TARIFF_OBSERVED = "2026-07-05T12:00:00+00:00"
TARIFF_COMMITTED = "2026-07-05T12:00:01+00:00"
CLAUSE_COMMITTED = "2026-07-05T12:00:02+00:00"
INVOICE_OBSERVED = "2026-07-10T16:00:00+00:00"
INVOICE_RECEIVED = "2026-07-10T16:05:00+00:00"
INVOICE_DATE = "2026-07-10"
APPROVED_AT = "2026-07-12T09:00:00+00:00"
CALCULATION = {
    "recorded_rate": "250.00",
    "claimed_rate": "350.00",
    "rate_currency": "USD",
    "rate_unit": "per_day",
    "charge_days": 7,
    "overcharge": "700.00",
    "recommendation": "dispute_overcharge",
}


def _content_hash(content: dict) -> str:
    return sha256_hex(canonical_json_bytes(content))


def _state() -> dict:
    content = {
        "tenant_id": TENANT_ID,
        "carrier_id": CARRIER_ID,
        "capture_id": CAPTURE_ID,
        "s3_bucket": "example-evidence-bucket",
        "s3_key": TARIFF_KEY,
        "s3_version_id": "fixture-tariff-version-001",
        "source_sha256": sha256_hex(TARIFF_BYTES),
        "source_size": len(TARIFF_BYTES),
        "clause_id": CLAUSE_ID,
        "clause_sha256": sha256_hex(CLAUSE_TEXT.encode("utf-8")),
        "clause_text": CLAUSE_TEXT,
        "verification_status": "VERIFIED",
        "verification_reason": "verified",
        "rate_amount": "250.00",
        "rate_currency": "USD",
        "rate_unit": "per_day",
        "effective_from": "2026-07-01",
        "effective_to": "2026-12-31",
        "observed_at": TARIFF_OBSERVED,
        "committed_at": TARIFF_COMMITTED,
        "clause_committed_at": CLAUSE_COMMITTED,
        "invoice_id": INVOICE_ID,
        "invoice_s3_bucket": "example-invoice-bucket",
        "invoice_s3_key": INVOICE_KEY,
        "invoice_s3_version_id": "fixture-invoice-version-001",
        "invoice_sha256": sha256_hex(INVOICE_BYTES),
        "invoice_source_size": len(INVOICE_BYTES),
        "invoice_no": "INV-NOL-084213",
        "invoice_observed_at": INVOICE_OBSERVED,
        "invoice_received_at": INVOICE_RECEIVED,
        "invoice_date": INVOICE_DATE,
        "invoice_claimed_rate": "350.00",
        "charge_days": 7,
        "calculation": deepcopy(CALCULATION),
        "recommendation": "dispute_overcharge",
        "human_approval_state": "NOT_PRESSED",
    }
    content_sha = _content_hash(content)
    manifest_item = {
        **content,
        "content": deepcopy(content),
        "evidence_id": EVIDENCE_ID,
        "kind": "tariff_invoice_receipt",
        "source_table": "tariff_clauses",
        "source_id": CLAUSE_ID,
        "content_sha256": content_sha,
    }
    manifest = {
        "manifest_version": 1,
        "tenant_id": TENANT_ID,
        "case_id": CASE_ID,
        "invoice_id": INVOICE_ID,
        "finding_id": FINDING_ID,
        "evidence": [manifest_item],
        "calculation": deepcopy(CALCULATION),
        "recommendation": "dispute_overcharge",
        "approved_by": "reviewer-fixture-001",
        "approved_at": APPROVED_AT,
    }
    return {
        "manifest": manifest,
        "case": {
            "id": CASE_ID,
            "tenant_id": TENANT_ID,
            "state": "FILED",
            "manifest_version": 1,
            "sealed_by": "reviewer-fixture-001",
            "sealed_txn_ts": "1783534098823702432.0000000000",
            "sealed_at_display": APPROVED_AT,
            "invoice_id": INVOICE_ID,
            "finding_id": FINDING_ID,
        },
        "capture": {
            "id": CAPTURE_ID,
            "tenant_id": TENANT_ID,
            "carrier_id": CARRIER_ID,
            "s3_key": TARIFF_KEY,
            "source_version_id": "fixture-tariff-version-001",
            "source_byte_size": len(TARIFF_BYTES),
            "doc_sha256": sha256_hex(TARIFF_BYTES),
            "captured_at": TARIFF_OBSERVED,
            "committed_at": TARIFF_COMMITTED,
        },
        "clause": {
            "id": CLAUSE_ID,
            "tenant_id": TENANT_ID,
            "snapshot_id": CAPTURE_ID,
            "clause_text": CLAUSE_TEXT,
            "sha256": sha256_hex(CLAUSE_TEXT.encode("utf-8")),
            "rate_amount": "250.00",
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "effective_from": "2026-07-01",
            "effective_to": "2026-12-31",
            "committed_at": CLAUSE_COMMITTED,
            "verification_status": "VERIFIED",
            "verification_reason": "verified",
        },
        "evidence": {
            "id": EVIDENCE_ID,
            "tenant_id": TENANT_ID,
            "case_id": CASE_ID,
            "kind": "tariff_invoice_receipt",
            "source_table": "tariff_clauses",
            "source_id": CLAUSE_ID,
            "content": content,
            "content_sha256": content_sha,
            "sealed": True,
        },
        "invoice": {
            "id": INVOICE_ID,
            "tenant_id": TENANT_ID,
            "invoice_no": "INV-NOL-084213",
            "s3_key": INVOICE_KEY,
            "source_version_id": "fixture-invoice-version-001",
            "sha256": sha256_hex(INVOICE_BYTES),
            "claimed_rate": "350.00",
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "charge_days": 7,
            "received_at": INVOICE_RECEIVED,
            "invoice_date": INVOICE_DATE,
        },
        "finding": {
            "id": FINDING_ID,
            "tenant_id": TENANT_ID,
            "invoice_id": INVOICE_ID,
            "recommendation": "dispute_overcharge",
            "calculation": deepcopy(CALCULATION),
            "human_approval_state": "APPROVED",
        },
        "sources": {
            ("example-evidence-bucket", TARIFF_KEY, "fixture-tariff-version-001"): LoadedSource(
                "example-evidence-bucket",
                TARIFF_KEY,
                "fixture-tariff-version-001",
                TARIFF_BYTES,
                TARIFF_OBSERVED,
            ),
            ("example-invoice-bucket", INVOICE_KEY, "fixture-invoice-version-001"): LoadedSource(
                "example-invoice-bucket",
                INVOICE_KEY,
                "fixture-invoice-version-001",
                INVOICE_BYTES,
                INVOICE_OBSERVED,
            ),
        },
    }


def _verify(
    state: dict | None = None,
    *,
    stored_hash: str | None = None,
    expected_tenant: str = TENANT_ID,
    expected_case: str = CASE_ID,
):
    state = deepcopy(state if state is not None else _state())
    manifest = state["manifest"]
    if stored_hash is None:
        stored_hash = prefixed_sha256(canonical_json_bytes(manifest))

    def source_loader(bucket: str, key: str, version_id: str) -> LoadedSource:
        return state["sources"][(bucket, key, version_id)]

    return verify_sealed_receipt(
        manifest=manifest,
        stored_evidence_hash=stored_hash,
        stored_case=state["case"],
        expected_tenant_id=expected_tenant,
        expected_case_id=expected_case,
        source_loader=source_loader,
        capture_loader=lambda tenant_id, row_id: state["capture"],
        clause_loader=lambda tenant_id, row_id: state["clause"],
        evidence_loader=lambda tenant_id, row_id: state["evidence"],
        invoice_loader=lambda tenant_id, row_id: state["invoice"],
        finding_loader=lambda tenant_id, row_id: state["finding"],
    )


def test_valid_receipt_reopens_both_exact_versions_and_passes():
    report = _verify()

    assert report.passed is True
    assert report.reasons == ()
    assert report.as_dict()["passed"] is True


@pytest.mark.parametrize("state", ["FILED", "CONTESTED", "RESOLVED"])
def test_receipt_remains_sealed_after_later_lifecycle_transitions(state):
    stored = _state()
    stored["case"]["state"] = state

    report = _verify(stored)

    assert report.passed is True
    assert "case_not_sealed" not in report.reasons


@pytest.mark.parametrize(
    ("target", "field", "altered", "reason"),
    [
        ("case", "tenant_id", "other-tenant", "stored_case_identity_mismatch"),
        ("case", "id", "other-case", "stored_case_identity_mismatch"),
        ("case", "state", "ANALYZED", "case_not_sealed"),
        ("case", "manifest_version", 2, "stored_manifest_version_mismatch"),
        ("case", "sealed_by", None, "sealed_by_mismatch"),
        ("case", "sealed_txn_ts", None, "sealed_txn_ts_missing"),
        ("case", "sealed_at_display", "2026-07-13T00:00:00Z", "sealed_at_mismatch"),
        ("capture", "tenant_id", "other-tenant", "capture_identity_mismatch"),
        ("capture", "id", "other-capture", "capture_identity_mismatch"),
        ("capture", "carrier_id", "other-carrier", "capture_identity_mismatch"),
        ("capture", "s3_key", "wrong/key", "capture_source_key_mismatch"),
        ("capture", "source_version_id", "wrong-version", "capture_source_version_mismatch"),
        ("capture", "doc_sha256", "0" * 64, "capture_source_hash_mismatch"),
        ("capture", "source_byte_size", 1, "capture_source_size_mismatch"),
        ("capture", "captured_at", "2026-07-06T00:00:00Z", "capture_observed_at_mismatch"),
        ("capture", "committed_at", "2026-07-06T00:00:00Z", "capture_committed_at_mismatch"),
        ("clause", "snapshot_id", "other-capture", "clause_identity_mismatch"),
        ("clause", "tenant_id", "other-tenant", "clause_identity_mismatch"),
        ("clause", "committed_at", "2026-07-06T00:00:00Z", "clause_committed_at_mismatch"),
        ("clause", "rate_amount", "275.00", "clause_rate_mismatch"),
        ("clause", "verification_status", "UNVERIFIED", "clause_verification_mismatch"),
        ("evidence", "case_id", "other-case", "evidence_row_identity_mismatch"),
        ("evidence", "tenant_id", "other-tenant", "evidence_row_identity_mismatch"),
        ("evidence", "sealed", False, "evidence_row_not_sealed"),
        ("evidence", "content_sha256", "0" * 64, "evidence_content_hash_mismatch"),
        ("invoice", "tenant_id", "other-tenant", "invoice_identity_mismatch"),
        ("invoice", "id", "other-invoice", "invoice_identity_mismatch"),
        ("invoice", "s3_key", "wrong/invoice", "invoice_source_identity_mismatch"),
        ("invoice", "source_version_id", "wrong-version", "invoice_source_identity_mismatch"),
        ("invoice", "sha256", "0" * 64, "invoice_source_identity_mismatch"),
        ("invoice", "received_at", "2026-07-11T00:00:00Z", "invoice_received_at_mismatch"),
        ("invoice", "invoice_date", "2026-07-11", "invoice_date_mismatch"),
        ("finding", "human_approval_state", "NOT_PRESSED", "human_approval_missing"),
        ("finding", "tenant_id", "other-tenant", "finding_identity_mismatch"),
        ("finding", "invoice_id", "other-invoice", "finding_identity_mismatch"),
        ("finding", "recommendation", "no_overcharge", "finding_binding_mismatch"),
    ],
)
def test_stored_row_negative_matrix(target, field, altered, reason):
    state = _state()
    state[target][field] = altered

    report = _verify(state)

    assert report.passed is False
    assert reason in report.reasons


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("capture", "capture_missing"),
        ("clause", "clause_missing"),
        ("evidence", "evidence_row_missing"),
        ("invoice", "invoice_missing"),
        ("finding", "finding_missing"),
    ],
)
def test_missing_stored_row_matrix(missing, reason):
    state = _state()
    state[missing] = None

    report = _verify(state)

    assert report.passed is False
    assert reason in report.reasons


def test_modified_tariff_source_bytes_fail():
    state = _state()
    ref = next(iter(state["sources"]))
    source = state["sources"][ref]
    state["sources"][ref] = LoadedSource(
        source.bucket, source.key, source.version_id, source.body + b" modified", source.observed_at
    )

    assert "source_hash_mismatch" in _verify(state).reasons


def test_wrong_tariff_version_id_fails_exact_fetch():
    state = _state()
    state["manifest"]["evidence"][0]["s3_version_id"] = "missing-version"

    assert "source_version_fetch_failed" in _verify(state).reasons


def test_modified_invoice_source_bytes_fail():
    state = _state()
    ref = ("example-invoice-bucket", INVOICE_KEY, "fixture-invoice-version-001")
    source = state["sources"][ref]
    state["sources"][ref] = LoadedSource(
        source.bucket, source.key, source.version_id, source.body + b" modified", source.observed_at
    )

    assert "invoice_source_hash_mismatch" in _verify(state).reasons


def test_invoice_claim_must_be_reparsed_from_exact_source_bytes():
    state = _state()
    ref = ("example-invoice-bucket", INVOICE_KEY, "fixture-invoice-version-001")
    altered_body = INVOICE_BYTES.replace(b"350.00", b"360.00")
    source = state["sources"][ref]
    state["sources"][ref] = LoadedSource(
        source.bucket, source.key, source.version_id, altered_body, source.observed_at
    )
    altered_sha = sha256_hex(altered_body)
    content = state["evidence"]["content"]
    content["invoice_sha256"] = altered_sha
    content["invoice_source_size"] = len(altered_body)
    state["invoice"]["sha256"] = altered_sha
    item = state["manifest"]["evidence"][0]
    item.update(content)
    item["content"] = deepcopy(content)
    item["content_sha256"] = _content_hash(content)
    state["evidence"]["content_sha256"] = _content_hash(content)

    report = _verify(state)

    assert "invoice_source_hash_mismatch" not in report.reasons
    assert "invoice_source_claim_mismatch" in report.reasons


def test_rebased_rate_metadata_still_fails_when_rate_is_absent_from_clause():
    state = _state()
    state["clause"]["rate_amount"] = "275.00"
    content = state["evidence"]["content"]
    content["rate_amount"] = "275.00"
    item = state["manifest"]["evidence"][0]
    item.update(content)
    item["content"] = deepcopy(content)
    item["content_sha256"] = _content_hash(content)
    state["evidence"]["content_sha256"] = _content_hash(content)

    report = _verify(state)

    assert "clause_rate_mismatch" not in report.reasons
    assert "rate_absent_from_clause" in report.reasons


def test_invoice_object_observation_time_mismatch_fails():
    state = _state()
    ref = ("example-invoice-bucket", INVOICE_KEY, "fixture-invoice-version-001")
    source = state["sources"][ref]
    state["sources"][ref] = LoadedSource(
        source.bucket, source.key, source.version_id, source.body, "2026-07-11T00:00:00Z"
    )

    assert "invoice_source_observed_at_mismatch" in _verify(state).reasons


def test_evidence_content_recomputation_detects_changed_content():
    state = _state()
    state["evidence"]["content"]["rate_amount"] = "251.00"

    report = _verify(state)

    assert "evidence_content_mismatch" in report.reasons
    assert "evidence_content_hash_mismatch" in report.reasons


def test_flattened_manifest_binding_must_equal_nested_evidence_content():
    state = _state()
    state["manifest"]["evidence"][0]["rate_amount"] = "251.00"

    assert "evidence_flattened_binding_mismatch" in _verify(state).reasons


def test_wrong_invoice_version_id_fails_database_binding_and_exact_fetch():
    state = _state()
    state["manifest"]["evidence"][0]["invoice_s3_version_id"] = "missing-version"
    state["manifest"]["evidence"][0]["content"]["invoice_s3_version_id"] = "missing-version"

    report = _verify(state)

    assert "invoice_source_identity_mismatch" in report.reasons
    assert "invoice_source_fetch_failed" in report.reasons


def test_empty_evidence_fails():
    state = _state()
    state["manifest"]["evidence"] = []

    assert "empty_evidence" in _verify(state).reasons


def test_altered_manifest_or_hash_fails():
    state = _state()
    original_hash = prefixed_sha256(canonical_json_bytes(state["manifest"]))
    state["manifest"]["approved_by"] = "unexpected-reviewer"

    assert "manifest_hash_mismatch" in _verify(state, stored_hash=original_hash).reasons
    assert "manifest_hash_mismatch" in _verify(_state(), stored_hash="sha256:" + "0" * 64).reasons


def test_expected_tenant_and_case_mismatch_fail():
    assert "tenant_mismatch" in _verify(expected_tenant="different-tenant").reasons
    assert "case_mismatch" in _verify(expected_case="different-case").reasons


def test_invoice_or_calculation_mismatch_fails():
    state = _state()
    state["invoice"]["claimed_rate"] = "360.00"
    assert "invoice_claim_mismatch" in _verify(state).reasons

    state = _state()
    state["manifest"]["calculation"]["overcharge"] = "699.00"
    assert "calculation_mismatch" in _verify(state).reasons

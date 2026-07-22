from __future__ import annotations

from copy import deepcopy

import pytest

from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.core.temporal_replay import (
    TemporalReplayValidationError,
    build_replay_response,
    canonical_hlc_literal,
    validate_replay_rows,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"
CASE_ID = "10000000-0000-4000-8000-000000000003"
EVIDENCE_ID = "10000000-0000-4000-8000-000000000004"
CLAUSE_ID = "10000000-0000-4000-8000-000000000005"
SNAPSHOT_ID = "10000000-0000-4000-8000-000000000006"
INVOICE_ID = "10000000-0000-4000-8000-000000000007"
FINDING_ID = "10000000-0000-4000-8000-000000000008"
HLC = "1784578667004573103.0000000000"
CLAUSE_SHA256 = "a" * 64
SOURCE_SHA256 = "b" * 64


def replay_rows(*, state: str = "FILED") -> list[dict]:
    content = {
        "clause_id": CLAUSE_ID,
        "capture_id": SNAPSHOT_ID,
        "rate_amount": "250.00",
        "clause_sha256": CLAUSE_SHA256,
        "source_sha256": SOURCE_SHA256,
    }
    content_hash = prefixed_sha256(canonical_json_bytes(content)).removeprefix("sha256:")
    manifest = {
        "manifest_version": 1,
        "tenant_id": TENANT_ID,
        "case_id": CASE_ID,
        "invoice_id": INVOICE_ID,
        "finding_id": FINDING_ID,
        "evidence": [
            {
                "evidence_id": EVIDENCE_ID,
                "kind": "tariff_invoice_receipt",
                "source_table": "tariff_clauses",
                "source_id": CLAUSE_ID,
                "content_sha256": content_hash,
                "content": content,
            }
        ],
    }
    return [
        {
            "tenant_id": TENANT_ID,
            "case_id": CASE_ID,
            "case_state": state,
            "sealed_txn_ts": HLC,
            "evidence_hash": prefixed_sha256(canonical_json_bytes(manifest)),
            "evidence_manifest": manifest,
            "manifest_version": 1,
            "evidence_id": EVIDENCE_ID,
            "evidence_kind": "tariff_invoice_receipt",
            "source_table": "tariff_clauses",
            "source_id": CLAUSE_ID,
            "evidence_content": content,
            "content_sha256": content_hash,
            "evidence_sealed": True,
            "clause_id": CLAUSE_ID,
            "tariff_rate": "250.00",
            "snapshot_id": SNAPSHOT_ID,
            "clause_sha256": CLAUSE_SHA256,
            "snapshot_source_sha256": SOURCE_SHA256,
            "version_label": "v2026-04-03",
        }
    ]


@pytest.mark.parametrize(
    "value",
    [
        "-1784578667004573103.0000000000",
        "+1784578667004573103.0000000000",
        "1784578667004573103.0",
        "1784578667004573103.00000000000",
        "1e18",
        " 1784578667004573103.0000000000",
        "1784578667004573103.0000000000; SELECT 1",
        "NaN",
        "0.0000000000",
    ],
)
def test_hlc_literal_rejects_every_noncanonical_or_injectable_form(value):
    with pytest.raises(TemporalReplayValidationError):
        canonical_hlc_literal(value)


def test_hlc_literal_accepts_exact_cockroach_decimal_form():
    assert canonical_hlc_literal(HLC) == HLC


def test_exact_historical_and_current_views_build_frozen_contract_shape():
    historical = validate_replay_rows(
        replay_rows(),
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=True,
    )
    current = validate_replay_rows(
        replay_rows(state="CONTESTED"),
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=False,
    )

    result = build_replay_response(historical=historical, current=current, queries=["then", "now"])

    assert set(result) == {
        "then",
        "now",
        "tamper_check",
        "sealed_copy",
        "retention",
        "queries",
    }
    assert result["then"]["as_of"] == HLC
    assert result["then"]["state"] == "FILED"
    assert result["now"]["state"] == "CONTESTED"
    assert result["then"]["source"] == "AS OF SYSTEM TIME"
    assert result["now"]["source"] == "current read"
    assert result["tamper_check"] == {"match": True}
    assert result["sealed_copy"]["rate"] == 250.0
    assert result["sealed_copy"]["content_sha256"].startswith("sha256:")
    assert result["sealed_copy"]["source"] == "case_evidence (sealed evidence copy)"
    assert result["retention"] == {
        "ttl_seconds": 7_776_000,
        "ttl_days": 90,
        "target_queryable": True,
        "language": (
            "Versioned S3 retains the dated source artifact. Within CockroachDB’s "
            "configured MVCC window, Tally can also replay the transactional case state "
            "at filing."
        ),
    }


def test_current_source_change_is_truthfully_reported_as_tamper_mismatch():
    historical = validate_replay_rows(
        replay_rows(),
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=True,
    )
    changed = replay_rows(state="CONTESTED")
    changed[0]["tariff_rate"] = "350.00"
    current = validate_replay_rows(
        changed,
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=False,
    )

    result = build_replay_response(historical=historical, current=current, queries=[])

    assert result["tamper_check"] == {"match": False}
    assert result["now"]["tariff_rate"] == 350.0
    assert result["sealed_copy"]["rate"] == 250.0


@pytest.mark.parametrize(
    ("field", "altered"),
    [
        ("clause_sha256", "c" * 64),
        ("snapshot_source_sha256", "d" * 64),
    ],
)
def test_current_source_hash_changes_are_truthfully_reported(field, altered):
    historical = validate_replay_rows(
        replay_rows(),
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=True,
    )
    changed = replay_rows(state="CONTESTED")
    changed[0][field] = altered
    current = validate_replay_rows(
        changed,
        expected_tenant_id=TENANT_ID,
        expected_case_id=CASE_ID,
        expected_hlc=HLC,
        historical=False,
    )

    assert build_replay_response(
        historical=historical, current=current, queries=[]
    )["tamper_check"] == {"match": False}


@pytest.mark.parametrize("field", ["clause_sha256", "snapshot_source_sha256"])
def test_historical_source_hash_corruption_fails_closed(field):
    rows = replay_rows()
    rows[0][field] = "0" * 64
    with pytest.raises(TemporalReplayValidationError, match="seal-time evidence"):
        validate_replay_rows(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_hlc=HLC,
            historical=True,
        )


def test_historical_corruption_fails_closed():
    rows = replay_rows()
    rows[0]["evidence_hash"] = "sha256:" + "0" * 64
    with pytest.raises(TemporalReplayValidationError, match="seal-time evidence"):
        validate_replay_rows(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_hlc=HLC,
            historical=True,
        )


def test_wrong_tenant_and_mismatched_hlc_fail_closed():
    rows = deepcopy(replay_rows())
    with pytest.raises(TemporalReplayValidationError, match="tenant"):
        validate_replay_rows(
            rows,
            expected_tenant_id="20000000-0000-4000-8000-000000000002",
            expected_case_id=CASE_ID,
            historical=True,
        )
    with pytest.raises(TemporalReplayValidationError, match="HLC"):
        validate_replay_rows(
            rows,
            expected_tenant_id=TENANT_ID,
            expected_case_id=CASE_ID,
            expected_hlc="1784578667004573104.0000000000",
            historical=True,
        )

"""Computed, offline acceptance tests for the exact Gate 4 ten-case harness."""

from __future__ import annotations

import copy
import json
import sys

from scripts import gate4_evaluation

EXPECTED_ACTUALS = (
    "PASS",
    "FLAG",
    "FLAG",
    "FLAG",
    "ABSTAIN",
    "ABSTAIN",
    "FAIL_CLOSED",
    "IDEMPOTENT",
    "VERIFICATION_FAILED",
    "REJECTED_TEMPORALLY",
)


def _results_by_criterion(report: dict[str, object]) -> dict[str, dict[str, object]]:
    results = report["results"]
    assert isinstance(results, list)
    return {str(result["criterion"]): result for result in results}


def test_fixture_is_exactly_the_public_safe_ten_case_contract():
    fixture = gate4_evaluation._load_fixture()

    assert fixture["classification"] == "synthetic demonstration data"
    assert tuple(case["criterion"] for case in fixture["cases"]) == (
        gate4_evaluation.EXPECTED_CRITERIA
    )
    assert len(fixture["cases"]) == 10
    assert len({case["case_id"] for case in fixture["cases"]}) == 10
    assert all(
        case["case_id"].startswith("GATE4-SYNTHETIC-DEMO-")
        for case in fixture["cases"]
    )
    fixture_text = gate4_evaluation.DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert fixture_text.count("Asterline Demo Shipping") == 4


def test_harness_computes_all_ten_contract_outcomes_from_production_paths():
    report = gate4_evaluation.run_harness()

    assert report["case_count"] == report["passed_count"] == 10
    assert report["failed_count"] == 0
    assert report["all_passed"] is True
    assert tuple(result["actual"] for result in report["results"]) == EXPECTED_ACTUALS

    results = _results_by_criterion(report)
    assert results["valid_recorded_rate"]["evidence"] == {
        "selected_recorded_rate": "250.00",
        "claimed_rate": "250.00",
        "charge_days": 7,
        "recommendation": "no_overcharge",
        "exact_source_verified": True,
    }
    assert results["later_wrong_rate"]["evidence"]["overcharge"] == "700.00"
    assert (
        results["later_wrong_rate"]["evidence"]["later_rate_temporal_status"]
        == "not_yet_effective"
    )
    assert results["late_invoice"]["evidence"]["days_after_last_charge"] == 36
    assert results["required_field_failure"]["evidence"]["missing_fields"] == [
        "proper_party_basis"
    ]
    assert results["invented_citation_abstains"]["evidence"]["reasons"] == [
        "clause_absent_from_source"
    ]
    assert results["missing_coverage_abstains"]["evidence"]["candidate_count"] == 0
    assert (
        results["cross_tenant_retrieval_fails"]["evidence"]["foreign_candidate_count"]
        == 0
    )

    duplicate = results["duplicate_filing_seal_idempotent"]["evidence"]
    assert duplicate == {
        "first_already_sealed": False,
        "second_already_sealed": True,
        "same_transaction_timestamp": True,
        "same_evidence_hash": True,
        "filing_effects_unchanged_after_second_call": True,
        "evidence_update_count": 1,
        "case_update_count": 1,
        "approval_update_count": 1,
        "ledger_event_count": 1,
        "sealed_evidence_count": 1,
    }
    assert results["corrupted_source_hash_fails"]["evidence"]["exact_source_status"] == (
        "hash_mismatch"
    )
    temporal = results["temporally_inapplicable_similar_source_rejected"]["evidence"]
    assert temporal["raw_top1_was_later_clause"] is True
    assert temporal["later_clause_selected"] is False
    assert temporal["historical_clause_selected"] is True


def test_expected_labels_cannot_drive_computed_actuals():
    fixture = copy.deepcopy(gate4_evaluation._load_fixture())
    fixture["cases"][0]["expected"] = "FLAG"

    report = gate4_evaluation.run_harness(fixture)

    first = report["results"][0]
    assert first["actual"] == "PASS"
    assert first["expected"] == "FLAG"
    assert first["passed"] is False
    assert report["passed_count"] == 9
    assert report["failed_count"] == 1
    assert report["all_passed"] is False


def test_cli_writes_the_computed_aggregate_json(tmp_path, monkeypatch, capsys):
    output = tmp_path / "gate4-results.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["gate4_evaluation", "--output", str(output)],
    )

    assert gate4_evaluation.main() == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report == gate4_evaluation.run_harness()
    assert report["classification"] == "public synthetic demonstration evaluation"
    assert report["case_count"] == report["passed_count"] == 10
    assert report["failed_count"] == 0
    assert report["all_passed"] is True
    assert capsys.readouterr().out.splitlines() == [
        "case_count=10",
        "passed_count=10",
        "failed_count=0",
        "all_passed=true",
    ]

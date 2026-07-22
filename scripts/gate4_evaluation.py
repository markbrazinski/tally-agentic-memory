"""Run the ten-case Gate 4 contract harness without network or live database I/O.

Every outcome is computed through an existing production domain or workflow
function.  The fixture contains expectations, but expectations are compared
only after execution and never influence the computed outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from src.core.extraction import ExtractionResult, apply_anti_hallucination_gate
from src.core.fields import FIELD_KEYS
from src.core.receipt import (
    TariffExtraction,
    calculate_overcharge,
    canonical_json_bytes,
    sha256_hex,
    verify_tariff_extraction,
)
from src.core.vector_retrieval import (
    EXACT_SOURCE_HASH_MISMATCH,
    TEMPORAL_NOT_YET_EFFECTIVE,
    ClauseDocument,
    ClauseIndex,
    RetrievalRequest,
)
from src.platform.clerk_pipeline import VERDICT_DEFECTIVE, run_extraction_steps
from src.platform.seal import seal_case

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPOSITORY_ROOT / "tests/fixtures/gate4/evaluation_cases.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "artifacts/recovery/gate-4/evaluation-results.json"
HARNESS_VERSION = 1

EXPECTED_CRITERIA = (
    "valid_recorded_rate",
    "later_wrong_rate",
    "late_invoice",
    "required_field_failure",
    "invented_citation_abstains",
    "missing_coverage_abstains",
    "cross_tenant_retrieval_fails",
    "duplicate_filing_seal_idempotent",
    "corrupted_source_hash_fails",
    "temporally_inapplicable_similar_source_rejected",
)


class Gate4EvaluationError(RuntimeError):
    """The public synthetic harness fixture or execution is invalid."""


def _load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate4EvaluationError("gate4_fixture_unreadable") from exc
    if not isinstance(value, Mapping):
        raise Gate4EvaluationError("gate4_fixture_invalid")
    fixture = dict(value)
    cases = fixture.get("cases")
    if (
        fixture.get("classification") != "synthetic demonstration data"
        or fixture.get("fixture_version") != 1
        or not isinstance(cases, list)
        or len(cases) != len(EXPECTED_CRITERIA)
    ):
        raise Gate4EvaluationError("gate4_fixture_contract_mismatch")
    criteria = tuple(case.get("criterion") for case in cases if isinstance(case, Mapping))
    case_ids = tuple(str(case.get("case_id", "")) for case in cases if isinstance(case, Mapping))
    if (
        criteria != EXPECTED_CRITERIA
        or len(case_ids) != len(cases)
        or len(set(case_ids)) != len(case_ids)
        or not all(case_id.startswith("GATE4-SYNTHETIC-DEMO-") for case_id in case_ids)
        or not all(
            isinstance(case, Mapping)
            and isinstance(case.get("expected"), str)
            and bool(case["expected"])
            for case in cases
        )
    ):
        raise Gate4EvaluationError("gate4_fixture_cases_invalid")
    return fixture


def _retrieval_fixture(fixture: Mapping[str, Any]) -> Mapping[str, Any]:
    retrieval = fixture.get("retrieval")
    identities = fixture.get("identities")
    if not isinstance(retrieval, Mapping) or not isinstance(identities, Mapping):
        raise Gate4EvaluationError("gate4_retrieval_fixture_invalid")
    return retrieval


def _documents(fixture: Mapping[str, Any]) -> tuple[ClauseDocument, ...]:
    retrieval = _retrieval_fixture(fixture)
    identities = fixture["identities"]
    values = retrieval.get("documents")
    if not isinstance(values, list) or len(values) != 2:
        raise Gate4EvaluationError("gate4_clause_fixture_invalid")
    documents: list[ClauseDocument] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise Gate4EvaluationError("gate4_clause_fixture_invalid")
        source_bytes = str(value["source_text"]).encode("utf-8")
        clause_text = str(value["clause_text"])
        documents.append(
            ClauseDocument(
                clause_id=str(value["clause_id"]),
                tenant_id=str(identities["tenant_id"]),
                carrier_id=str(identities["carrier_id"]),
                document_family=str(retrieval["document_family"]),
                capture_id=str(value["capture_id"]),
                source_version_id=str(value["source_version_id"]),
                source_sha256=sha256_hex(source_bytes),
                clause_sha256=sha256_hex(clause_text.encode("utf-8")),
                effective_from=date.fromisoformat(str(value["effective_from"])),
                effective_to=date.fromisoformat(str(value["effective_to"])),
                equipment=str(retrieval["equipment"]),
                route=str(retrieval["route"]),
                service=str(retrieval["service"]),
                rate_amount=Decimal(str(value["rate_amount"])),
                rate_currency="USD",
                rate_unit="per_day",
                clause_text=clause_text,
                embedding=tuple(value["embedding"]),
                embedding_integrity_verified=True,
                source_bytes=source_bytes,
            )
        )
    return tuple(documents)


def _request(
    fixture: Mapping[str, Any],
    *,
    tenant_id: str | None = None,
    carrier_id: str | None = None,
) -> RetrievalRequest:
    retrieval = _retrieval_fixture(fixture)
    identities = fixture["identities"]
    return RetrievalRequest(
        tenant_id=tenant_id or str(identities["tenant_id"]),
        carrier_id=carrier_id or str(identities["carrier_id"]),
        document_family=str(retrieval["document_family"]),
        charge_date=date.fromisoformat(str(retrieval["charge_date"])),
        equipment=str(retrieval["equipment"]),
        route=str(retrieval["route"]),
        service=str(retrieval["service"]),
        query_embedding=tuple(retrieval["query_embedding"]),
    )


def _clerk_result(*, invoice_date: str, missing_field: str | None = None):
    raw_fields: dict[str, dict[str, object]] = {}
    source_lines = ["SYNTHETIC DEMONSTRATION DATA"]
    for key in FIELD_KEYS:
        value = "2026-07-10" if key == "free_time_end" else f"synthetic-value-{key}"
        verbatim = f"{key}: {value}"
        raw_fields[key] = {
            "value": value,
            "verbatim": verbatim,
            "page": 1,
            "confidence": 1.0,
        }
        if key != missing_field:
            source_lines.append(verbatim)
    gated: ExtractionResult = apply_anti_hallucination_gate(
        {
            "fields": raw_fields,
            "invoice_no": "GATE4-SYNTHETIC-INVOICE-CLERK",
            "currency": "USD",
            "notes_footnotes": [],
        },
        "\n".join(source_lines),
    )
    return run_extraction_steps(
        gated,
        billed_party_name=None,
        invoice_date_raw=invoice_date,
    )


def _valid_recorded_rate(fixture: Mapping[str, Any]) -> tuple[str, dict[str, object]]:
    result = ClauseIndex.build(_documents(fixture)).retrieve(_request(fixture))
    selected = result.selected
    calculation = calculate_overcharge(
        recorded_rate=selected.rate_amount if selected else Decimal("0"),
        claimed_rate=Decimal("250.00"),
        charge_days=7,
    )
    passed = bool(
        selected
        and selected.rate_amount == Decimal("250.00")
        and selected.exact_source_status == "verified"
        and calculation.recommendation == "no_overcharge"
        and calculation.overcharge == Decimal("0.00")
    )
    return ("PASS" if passed else "UNEXPECTED"), {
        "selected_recorded_rate": format(selected.rate_amount, ".2f") if selected else None,
        "claimed_rate": "250.00",
        "charge_days": 7,
        "recommendation": calculation.recommendation,
        "exact_source_verified": bool(selected and selected.exact_source_status == "verified"),
    }


def _later_wrong_rate(fixture: Mapping[str, Any]) -> tuple[str, dict[str, object]]:
    documents = _documents(fixture)
    result = ClauseIndex.build(documents).retrieve(_request(fixture))
    selected = result.selected
    later = next(candidate for candidate in result.candidates if "LATER-350" in candidate.clause_id)
    calculation = calculate_overcharge(
        recorded_rate=selected.rate_amount if selected else Decimal("0"),
        claimed_rate=Decimal("350.00"),
        charge_days=7,
    )
    flagged = bool(
        selected
        and selected.rate_amount == Decimal("250.00")
        and later.temporal_status == TEMPORAL_NOT_YET_EFFECTIVE
        and not later.selected
        and calculation.recommendation == "dispute_overcharge"
        and calculation.overcharge == Decimal("700.00")
    )
    return ("FLAG" if flagged else "UNEXPECTED"), {
        "selected_recorded_rate": format(selected.rate_amount, ".2f") if selected else None,
        "claimed_later_rate": "350.00",
        "overcharge": format(calculation.overcharge, ".2f"),
        "later_rate_temporal_status": later.temporal_status,
    }


def _late_invoice(_: Mapping[str, Any]) -> tuple[str, dict[str, object]]:
    result = _clerk_result(invoice_date="2026-08-15")
    flagged = (
        result.verdict == VERDICT_DEFECTIVE
        and result.cited_rule == "30-day window"
        and result.window_result.days == 36
        and result.window_result.within_30 is False
    )
    return ("FLAG" if flagged else "UNEXPECTED"), {
        "verdict": result.verdict,
        "cited_rule": result.cited_rule,
        "days_after_last_charge": result.window_result.days,
        "within_30_days": result.window_result.within_30,
    }


def _required_field_failure(_: Mapping[str, Any]) -> tuple[str, dict[str, object]]:
    result = _clerk_result(invoice_date="2026-07-20", missing_field="proper_party_basis")
    missing = [field.key for field in result.field_results if not field.present]
    flagged = (
        result.verdict == VERDICT_DEFECTIVE
        and result.cited_rule == "541.6(a)(7)"
        and missing == ["proper_party_basis"]
    )
    return ("FLAG" if flagged else "UNEXPECTED"), {
        "verdict": result.verdict,
        "cited_rule": result.cited_rule,
        "missing_fields": missing,
    }


def _invented_citation_abstains(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    recorded = _documents(fixture)[0]
    extraction = TariffExtraction.from_mapping(
        {
            "rate_amount": "450.00",
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "rate_text": "USD $450.00 per day",
            "effective_from": "2026-07-01",
            "effective_to": "2026-07-31",
            "clause_text": "Invented Section 99: USD $450.00 per day.",
            "source_locator": "synthetic-section-99",
            "confidence": "1.0",
        }
    )
    verification = verify_tariff_extraction(recorded.source_bytes or b"", extraction)
    abstained = not verification.eligible and "clause_absent_from_source" in verification.reasons
    return ("ABSTAIN" if abstained else "UNEXPECTED"), {
        "eligible": verification.eligible,
        "reasons": list(verification.reasons),
        "invented_clause_carried_forward": verification.eligible,
    }


def _missing_coverage_abstains(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    result = ClauseIndex.build(_documents(fixture)).retrieve(
        _request(
            fixture,
            carrier_id=str(fixture["identities"]["missing_coverage_carrier_id"]),
        )
    )
    abstained = result.abstained and not result.candidates and result.abstention_reasons == (
        "no_scoped_candidates",
    )
    return ("ABSTAIN" if abstained else "UNEXPECTED"), {
        "candidate_count": len(result.candidates),
        "selected": result.selected is not None,
        "abstention_reasons": list(result.abstention_reasons),
    }


def _cross_tenant_retrieval_fails(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    result = ClauseIndex.build(_documents(fixture)).retrieve(
        _request(fixture, tenant_id=str(fixture["identities"]["wrong_tenant_id"]))
    )
    failed_closed = result.abstained and not result.candidates and result.selected is None
    return ("FAIL_CLOSED" if failed_closed else "UNEXPECTED"), {
        "foreign_candidate_count": len(result.candidates),
        "selected": result.selected is not None,
        "abstention_reasons": list(result.abstention_reasons),
    }


class _SealCursor:
    def __init__(self, connection: "_SealConnection") -> None:
        self.connection = connection
        self.one: tuple[object, ...] | None = None
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_SealCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> "_SealCursor":
        normalized = " ".join(sql.split())
        self.one, self.rows = self.connection.dispatch(normalized, params or ())
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _SealConnection:
    """One synthetic row-set implementing the database boundary used by seal_case."""

    def __init__(self, identities: Mapping[str, Any]) -> None:
        self.identities = identities
        self.state = "ANALYZED"
        self.sealed_txn_ts: Decimal | None = None
        self.evidence_hash: str | None = None
        self.manifest: dict[str, Any] | None = None
        self.sealed_by: str | None = None
        self.sealed_at: str | None = None
        self.manifest_version: int | None = None
        self.evidence_sealed = False
        self.approval_state = "NOT_PRESSED"
        self.ledger_count = 0
        self.audit_count = 0
        self.evidence_update_count = 0
        self.case_update_count = 0
        self.approval_update_count = 0
        self.content = {
            "classification": "synthetic demonstration data",
            "calculation": {
                "recorded_rate": "250.00",
                "claimed_rate": "350.00",
                "charge_days": 7,
                "overcharge": "700.00",
                "recommendation": "dispute_overcharge",
            },
            "recommendation": "dispute_overcharge",
        }

    def cursor(self) -> _SealCursor:
        return _SealCursor(self)

    def _evidence_rows(self) -> list[tuple[object, ...]]:
        return [
            (
                self.identities["seal_evidence_id"],
                "synthetic_tariff_receipt",
                "tariff_clauses",
                "GATE4-SYNTHETIC-CLAUSE-RECORDED-250",
                self.content,
                sha256_hex(canonical_json_bytes(self.content)),
                self.evidence_sealed,
            )
        ]

    def dispatch(
        self, sql: str, params: tuple[object, ...]
    ) -> tuple[tuple[object, ...] | None, list[tuple[object, ...]]]:
        if sql.startswith("SELECT state, sealed_txn_ts, evidence_hash, invoice_id"):
            return (
                self.state,
                self.sealed_txn_ts,
                self.evidence_hash,
                self.identities["seal_invoice_id"],
                self.identities["seal_finding_id"],
                self.identities["carrier_id"],
                "Synthetic demonstration dispute",
                self.manifest,
                self.sealed_by,
                self.sealed_at,
                self.manifest_version,
            ), []
        if sql.startswith("SELECT id, kind, source_table, source_id, content, content_sha256"):
            return None, self._evidence_rows()
        if sql.startswith("SELECT human_approval_state FROM findings"):
            return (self.approval_state,), []
        if sql.startswith("UPDATE case_evidence SET sealed=true"):
            self.evidence_sealed = True
            self.evidence_update_count += 1
            return None, []
        if sql.startswith("UPDATE cases SET state='FILED'"):
            self.state = "FILED"
            self.case_update_count += 1
            self.sealed_txn_ts = Decimal("1784682000000000000.0000000000")
            self.sealed_at = str(params[0])
            self.sealed_by = str(params[1])
            self.evidence_hash = str(params[2])
            self.manifest = json.loads(str(params[3]))
            self.manifest_version = 1
            return (
                self.sealed_txn_ts,
                self.identities["carrier_id"],
                "Synthetic demonstration dispute",
                self.sealed_by,
                self.sealed_at,
                self.manifest_version,
            ), []
        if sql.startswith("UPDATE findings SET human_approval_state='APPROVED'"):
            self.approval_state = "APPROVED"
            self.approval_update_count += 1
            return None, [(self.identities["seal_finding_id"],)]
        if sql.startswith("INSERT INTO ledger_events"):
            self.ledger_count += 1
            return None, []
        if sql.startswith("INSERT INTO query_log"):
            self.audit_count += 1
            return None, []
        raise Gate4EvaluationError("gate4_seal_adapter_unrecognized_statement")


class _SealDAL:
    def __init__(self, connection: _SealConnection, identities: Mapping[str, Any]) -> None:
        self.connection = connection
        self.tenant = SimpleNamespace(
            tenant_id=str(identities["tenant_id"]),
            actor="gate4-synthetic-evaluation",
        )

    def run_with_retry(self, operation: Callable[[_SealConnection], Any]) -> Any:
        return operation(self.connection)


def _duplicate_filing_seal_idempotent(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    identities = fixture["identities"]
    connection = _SealConnection(identities)
    dal = _SealDAL(connection, identities)
    arguments = {
        "case_id": str(identities["seal_case_id"]),
        "sealed_by_user_id": str(identities["seal_reviewer_id"]),
        "sealed_at_display": "2026-07-21T20:00:00Z",
    }
    first = seal_case(dal, **arguments)
    filing_effects_after_first = (
        connection.evidence_update_count,
        connection.case_update_count,
        connection.approval_update_count,
        connection.ledger_count,
    )
    second = seal_case(dal, **arguments)
    filing_effects_after_second = (
        connection.evidence_update_count,
        connection.case_update_count,
        connection.approval_update_count,
        connection.ledger_count,
    )
    idempotent = bool(
        first["already_sealed"] is False
        and second["already_sealed"] is True
        and first["state"] == second["state"] == "FILED"
        and first["sealed_txn_ts"] == second["sealed_txn_ts"]
        and first["evidence_hash"] == second["evidence_hash"]
        and first["evidence_count"] == second["evidence_count"] == 1
        and connection.ledger_count == 1
        and filing_effects_after_second == filing_effects_after_first == (1, 1, 1, 1)
        and connection.evidence_sealed
        and connection.approval_state == "APPROVED"
    )
    return ("IDEMPOTENT" if idempotent else "UNEXPECTED"), {
        "first_already_sealed": first["already_sealed"],
        "second_already_sealed": second["already_sealed"],
        "same_transaction_timestamp": first["sealed_txn_ts"] == second["sealed_txn_ts"],
        "same_evidence_hash": first["evidence_hash"] == second["evidence_hash"],
        "filing_effects_unchanged_after_second_call": (
            filing_effects_after_second == filing_effects_after_first
        ),
        "evidence_update_count": connection.evidence_update_count,
        "case_update_count": connection.case_update_count,
        "approval_update_count": connection.approval_update_count,
        "ledger_event_count": connection.ledger_count,
        "sealed_evidence_count": second["evidence_count"],
    }


def _corrupted_source_hash_fails(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    documents = _documents(fixture)
    recorded = documents[0]
    corrupted = replace(recorded, source_bytes=(recorded.source_bytes or b"") + b" CORRUPTED")
    result = ClauseIndex.build((corrupted, documents[1])).retrieve(_request(fixture))
    evaluated = next(
        candidate for candidate in result.candidates if candidate.clause_id == recorded.clause_id
    )
    failed = (
        evaluated.exact_source_status == EXACT_SOURCE_HASH_MISMATCH
        and "exact_source_hash_mismatch" in evaluated.rejection_reasons
        and result.abstained
    )
    return ("VERIFICATION_FAILED" if failed else "UNEXPECTED"), {
        "exact_source_status": evaluated.exact_source_status,
        "selected": result.selected is not None,
        "rejection_reasons": list(evaluated.rejection_reasons),
    }


def _temporally_inapplicable_similar_source_rejected(
    fixture: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    result = ClauseIndex.build(_documents(fixture)).retrieve(_request(fixture))
    later = next(candidate for candidate in result.candidates if "LATER-350" in candidate.clause_id)
    historical = next(
        candidate for candidate in result.candidates if "RECORDED-250" in candidate.clause_id
    )
    rejected = bool(
        result.candidates
        and result.candidates[0].clause_id == later.clause_id
        and later.temporal_status == TEMPORAL_NOT_YET_EFFECTIVE
        and "temporal_not_yet_effective" in later.rejection_reasons
        and not later.selected
        and result.selected is not None
        and result.selected.clause_id == historical.clause_id
    )
    return ("REJECTED_TEMPORALLY" if rejected else "UNEXPECTED"), {
        "raw_top1_was_later_clause": bool(
            result.candidates and result.candidates[0].clause_id == later.clause_id
        ),
        "later_clause_temporal_status": later.temporal_status,
        "later_clause_selected": later.selected,
        "historical_clause_selected": bool(
            result.selected and result.selected.clause_id == historical.clause_id
        ),
    }


EVALUATORS: dict[str, Callable[[Mapping[str, Any]], tuple[str, dict[str, object]]]] = {
    "valid_recorded_rate": _valid_recorded_rate,
    "later_wrong_rate": _later_wrong_rate,
    "late_invoice": _late_invoice,
    "required_field_failure": _required_field_failure,
    "invented_citation_abstains": _invented_citation_abstains,
    "missing_coverage_abstains": _missing_coverage_abstains,
    "cross_tenant_retrieval_fails": _cross_tenant_retrieval_fails,
    "duplicate_filing_seal_idempotent": _duplicate_filing_seal_idempotent,
    "corrupted_source_hash_fails": _corrupted_source_hash_fails,
    "temporally_inapplicable_similar_source_rejected": (
        _temporally_inapplicable_similar_source_rejected
    ),
}


def run_harness(fixture: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = dict(fixture) if fixture is not None else _load_fixture()
    if fixture is not None:
        # Apply the same validation to caller-supplied mappings without making
        # expectations part of any evaluator's input decision.
        cases = value.get("cases")
        if not isinstance(cases, list) or tuple(
            case.get("criterion") for case in cases if isinstance(case, Mapping)
        ) != EXPECTED_CRITERIA:
            raise Gate4EvaluationError("gate4_fixture_cases_invalid")
    results: list[dict[str, Any]] = []
    for case in value["cases"]:
        criterion = str(case["criterion"])
        evaluator = EVALUATORS.get(criterion)
        if evaluator is None:
            raise Gate4EvaluationError("gate4_evaluator_missing")
        actual, evidence = evaluator(value)
        expected = str(case["expected"])
        results.append(
            {
                "case_id": str(case["case_id"]),
                "criterion": criterion,
                "expected": expected,
                "actual": actual,
                "passed": actual == expected,
                "evidence": evidence,
            }
        )
    passed_count = sum(int(result["passed"]) for result in results)
    fixture_bytes = canonical_json_bytes(value)
    return {
        "classification": "public synthetic demonstration evaluation",
        "harness_version": HARNESS_VERSION,
        "fixture_version": value["fixture_version"],
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "case_count": len(results),
        "passed_count": passed_count,
        "failed_count": len(results) - passed_count,
        "all_passed": passed_count == len(results),
        "results": results,
    }


def write_public_results(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        fixture = _load_fixture(args.fixture)
        report = run_harness(fixture)
        write_public_results(args.output, report)
    except (Gate4EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"gate4_evaluation_error={type(exc).__name__}")
        return 2
    print(f"case_count={report['case_count']}")
    print(f"passed_count={report['passed_count']}")
    print(f"failed_count={report['failed_count']}")
    print(f"all_passed={str(report['all_passed']).lower()}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

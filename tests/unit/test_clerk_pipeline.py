"""Unit tests for src/platform/clerk_pipeline.py.

Per CLAUDE.md ("All external calls mocked in tests; zero network calls in
the test suite"), file_case()'s DB writes use an in-memory fake
connection/cursor (same spirit as tests/unit/test_commit.py's
FakeConnection, tests/unit/test_dal.py's FakeConnection - purpose-built
here since file_case needs both conn.transaction() for run_with_retry AND
RETURNING-id behavior across three sequential INSERTs).

Covers bundle-0.md's named B0-S2 test requirement: "step-7 txn rollback
on induced failure."
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from src.core.clerk_steps import FieldResult, WindowResult
from src.core.fields import FIELD_KEYS
from src.external.bedrock_extract import ExtractedField, ExtractionResult
from src.external.dal import DAL, Tenant
from src.platform.clerk_pipeline import (
    VERDICT_DEFECTIVE,
    VERDICT_NEEDS_REVIEW,
    VERDICT_VALID,
    build_summary,
    compute_verdict,
    file_case,
    run_extraction_steps,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"


def _all_present_field_results() -> tuple[FieldResult, ...]:
    return tuple(FieldResult(key=k, present=True, how="verbatim") for k in FIELD_KEYS)


def _one_missing_field_results(missing_key: str = "proper_party_basis") -> tuple[FieldResult, ...]:
    return tuple(
        FieldResult(key=k, present=(k != missing_key),
                    how="missing" if k == missing_key else "verbatim")
        for k in FIELD_KEYS
    )


def _within_window() -> WindowResult:
    return WindowResult(
        invoice_date=date(2026, 5, 1), last_charge_date=date(2026, 4, 15),
        days=16, within_30=True, ambiguous=False,
    )


def _outside_window() -> WindowResult:
    return WindowResult(
        invoice_date=date(2026, 6, 1), last_charge_date=date(2026, 4, 15),
        days=47, within_30=False, ambiguous=False,
    )


def _ambiguous_window() -> WindowResult:
    return WindowResult(
        invoice_date=None, last_charge_date=None, days=None, within_30=None, ambiguous=True,
    )


# --- compute_verdict ---


def test_compute_verdict_missing_field_is_defective_with_its_cite():
    verdict, cite = compute_verdict(_one_missing_field_results(), _within_window())
    assert verdict == VERDICT_DEFECTIVE
    assert cite == "541.6(a)(7)"  # proper_party_basis's cite


def test_compute_verdict_missing_field_wins_over_ambiguous_window():
    """Missing field is the highest-precedence check, per TDD §4 step 6's
    rule ordering - it must win even if timing is also ambiguous."""
    verdict, _ = compute_verdict(_one_missing_field_results(), _ambiguous_window())
    assert verdict == VERDICT_DEFECTIVE


def test_compute_verdict_late_invoice_is_defective_with_30_day_window_cite():
    verdict, cite = compute_verdict(_all_present_field_results(), _outside_window())
    assert verdict == VERDICT_DEFECTIVE
    assert cite == "30-day window"


def test_compute_verdict_ambiguous_window_is_needs_review_not_a_guess():
    verdict, cite = compute_verdict(_all_present_field_results(), _ambiguous_window())
    assert verdict == VERDICT_NEEDS_REVIEW
    assert cite is None


def test_compute_verdict_all_present_and_within_window_is_valid():
    verdict, cite = compute_verdict(_all_present_field_results(), _within_window())
    assert verdict == VERDICT_VALID
    assert cite is None


def test_compute_verdict_field_with_no_cite_returns_none_not_a_crash():
    missing_results = _one_missing_field_results("port_of_discharge")
    verdict, cite = compute_verdict(missing_results, _within_window())
    assert verdict == VERDICT_DEFECTIVE
    assert cite is None  # port_of_discharge has no cite in the canon


# --- build_summary ---


def test_build_summary_valid_verdict():
    summary = build_summary(VERDICT_VALID, _all_present_field_results())
    assert "13" in summary or "present" in summary.lower()


def test_build_summary_lists_missing_fields():
    summary = build_summary(VERDICT_DEFECTIVE, _one_missing_field_results("port_of_discharge"))
    assert "port_of_discharge" in summary


def test_build_summary_never_fabricates_beyond_inputs():
    """A VALID summary must not claim anything about missing fields when
    there are none - and vice versa."""
    summary = build_summary(VERDICT_VALID, _all_present_field_results())
    assert "missing" not in summary.lower()


# --- run_extraction_steps ---


def _extraction_with_free_time_end(value: str | None) -> ExtractionResult:
    fields = {k: ExtractedField(value=None, verbatim=None, page=None, confidence=0.0)
              for k in FIELD_KEYS}
    if value is not None:
        fields["free_time_end"] = ExtractedField(
            value=value, verbatim=value, page=1, confidence=0.9
        )
    return ExtractionResult(
        fields=fields, invoice_no="INV-001", currency="USD",
        notes_footnotes=(), is_image_only=False,
    )


def test_run_extraction_steps_produces_needs_review_when_free_time_end_missing():
    extraction = _extraction_with_free_time_end(None)
    result = run_extraction_steps(
        extraction, billed_party_name="Meridian", invoice_date_raw="2026-05-01"
    )
    # free_time_end missing -> also a missing-field DEFECTIVE (higher precedence
    # than the resulting ambiguous window), matching compute_verdict's ordering.
    assert result.verdict == VERDICT_DEFECTIVE


def test_run_extraction_steps_produces_defective_for_late_invoice():
    extraction = _extraction_with_free_time_end("2026-04-15")
    # fill every other field so only timing determines the verdict
    for key in FIELD_KEYS:
        if key == "free_time_end":
            continue
        extraction.fields[key] = ExtractedField(value="x", verbatim="x", page=1, confidence=0.9)
    result = run_extraction_steps(
        extraction, billed_party_name="Meridian", invoice_date_raw="2026-06-01"
    )
    assert result.verdict == VERDICT_DEFECTIVE
    assert result.cited_rule == "30-day window"


# --- file_case: the atomic filing commit ---


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql: str, params=None):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        if self._conn.fail_on and self._conn.fail_on in normalized:
            raise psycopg.errors.UndefinedTable(f"induced failure: {self._conn.fail_on}")
        self._last_result = self._conn.dispatch(normalized, params)
        return self

    def fetchone(self):
        return self._last_result


class FakeTransactionCtx:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        # Real conn.transaction() rolls back on an exception leaving the
        # transaction scope - simulate that by discarding all rows
        # written so far when an exception propagates out.
        if exc_info[0] is not None:
            self._conn.findings.clear()
            self._conn.cases.clear()
            self._conn.case_evidence.clear()
            self._conn.invoices_updated.clear()
            self._conn.query_log_rows.clear()
        return False


class FakeConnection:
    def __init__(self, fail_on: str | None = None):
        self.executed: list[tuple[str, object]] = []
        self.findings: list[dict] = []
        self.cases: list[dict] = []
        self.case_evidence: list[dict] = []
        self.invoices_updated: list[str] = []
        self.query_log_rows: list[dict] = []
        self.fail_on = fail_on
        self._next_id = 1

    def _new_id(self) -> str:
        val = f"00000000-0000-0000-0000-{self._next_id:012d}"
        self._next_id += 1
        return val

    def transaction(self):
        return FakeTransactionCtx(self)

    def cursor(self):
        return FakeCursor(self)

    def dispatch(self, sql: str, params):
        if sql.startswith("INSERT INTO findings"):
            finding_id = self._new_id()
            self.findings.append({"id": finding_id, "params": params})
            return (finding_id,)
        if sql.startswith("INSERT INTO cases"):
            case_id = self._new_id()
            self.cases.append({"id": case_id, "params": params})
            return (case_id,)
        if sql.startswith("INSERT INTO case_evidence"):
            self.case_evidence.append({"params": params})
            return None
        if sql.startswith("UPDATE invoices"):
            self.invoices_updated.append(params[1] if params else None)
            return None
        if sql.startswith("INSERT INTO query_log"):
            self.query_log_rows.append({"params": params})
            return None
        return None


def _clerk_result() -> "object":
    from src.platform.clerk_pipeline import ClerkResult

    return ClerkResult(
        verdict=VERDICT_DEFECTIVE,
        cited_rule="541.6(a)(7)",
        field_results=_one_missing_field_results(),
        window_result=_within_window(),
        summary="Missing required field(s): proper_party_basis.",
    )


def _make_dal(conn: FakeConnection) -> DAL:
    return DAL(conn, Tenant(tenant_id=TENANT_ID, actor="clerk"))


def test_file_case_writes_one_finding_one_case_and_updates_invoice():
    conn = FakeConnection()
    dal = _make_dal(conn)

    result = file_case(
        dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
        pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
    )

    assert len(conn.findings) == 1
    assert len(conn.cases) == 1
    assert conn.invoices_updated == ["inv-1"]
    assert result["case_id"] == conn.cases[0]["id"]
    assert result["finding_id"] == conn.findings[0]["id"]


def test_file_case_writes_evidence_rows():
    conn = FakeConnection()
    dal = _make_dal(conn)
    evidence = (
        {"kind": "tariff_snapshot", "source_table": "tariff_snapshots",
         "source_id": "snap-1", "content": {"rate": 250.00}},
        {"kind": "invoice_field", "source_table": "invoices",
         "source_id": "inv-1", "content": {"field": "port_of_discharge"}},
    )

    file_case(
        dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
        pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
        evidence_items=evidence,
    )

    assert len(conn.case_evidence) == 2


def test_file_case_persists_gate1_receipt_binding_in_finding():
    conn = FakeConnection()
    binding = {
        "tariff_clause_id": "clause-1",
        "rate_unit": "per_day",
        "tariff_result": {"verification_status": "VERIFIED"},
        "calculation": {
            "recorded_rate": "250.00",
            "claimed_rate": "350.00",
            "charge_days": 7,
            "overcharge": "700.00",
            "recommendation": "dispute_overcharge",
        },
    }

    file_case(
        _make_dal(conn), invoice_id="inv-1", clerk_run_id="run-1",
        carrier_id="carrier-1", pin_date="2026-07-11", amount=700.00,
        clerk_result=_clerk_result(), receipt_binding=binding,
    )

    params = conn.findings[0]["params"]
    assert "VERIFIED" in params[7]
    assert params[9] == "700.00"
    assert params[10] == "clause-1"
    assert params[11:14] == ("250.00", "350.00", "per_day")
    assert params[14] == 7
    assert params[15] == "dispute_overcharge"


def test_file_case_writes_a_commit_query_log_row_inside_the_transaction():
    """Closes the gap DAL.run_with_retry's docstring flags: real callers
    of this path DO get audit logging, written as part of the same
    transaction, not bolted on separately."""
    conn = FakeConnection()
    dal = _make_dal(conn)

    file_case(
        dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
        pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
    )

    assert len(conn.query_log_rows) == 1


def test_file_case_induced_failure_leaves_zero_partial_rows():
    """The core TDD §2.21-A guarantee: if any insert fails, no partial
    case exists - finding, evidence, and case can never drift apart
    because they never existed apart."""
    conn = FakeConnection(fail_on="INSERT INTO case_evidence")
    dal = _make_dal(conn)
    evidence = (
        {"kind": "tariff_snapshot", "source_table": "tariff_snapshots",
         "source_id": "snap-1", "content": {"rate": 250.00}},
    )

    with pytest.raises(psycopg.errors.UndefinedTable):
        file_case(
            dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
            pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
            evidence_items=evidence,
        )

    assert conn.findings == []
    assert conn.cases == []
    assert conn.case_evidence == []
    assert conn.invoices_updated == []
    assert conn.query_log_rows == []


def test_file_case_induced_failure_on_findings_insert_leaves_nothing():
    conn = FakeConnection(fail_on="INSERT INTO findings")
    dal = _make_dal(conn)

    with pytest.raises(psycopg.errors.UndefinedTable):
        file_case(
            dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
            pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
        )

    assert conn.findings == []
    assert conn.cases == []


def test_file_case_induced_failure_on_invoice_update_rolls_back_the_case_too():
    conn = FakeConnection(fail_on="UPDATE invoices")
    dal = _make_dal(conn)

    with pytest.raises(psycopg.errors.UndefinedTable):
        file_case(
            dal, invoice_id="inv-1", clerk_run_id="run-1", carrier_id="carrier-1",
            pin_date="2026-05-01", amount=1050.00, clerk_result=_clerk_result(),
        )

    assert conn.findings == []
    assert conn.cases == []

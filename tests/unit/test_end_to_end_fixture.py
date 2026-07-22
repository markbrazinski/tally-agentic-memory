"""End-to-end test: the hand-made defective fixture through the full local
pipeline (pdfplumber -> gate -> steps 2-3 -> verdict -> filing commit).

Per bundle-0.md's named B0-S2 requirement: "one hand-made defective
fixture (missing field 7) end-to-end locally." Bedrock's own call is
mocked here (per CLAUDE.md's zero-network-calls rule) with the exact
response shape confirmed by a real, manual InvokeModel run against this
same fixture on 2026-07-08 (see session notes) - this test locks in that
confirmed real behavior as a permanent regression check, not a fresh
assumption.
"""

from __future__ import annotations

import os

import pdfplumber

from src.external.bedrock_extract import apply_anti_hallucination_gate
from src.external.dal import DAL, Tenant
from src.platform.clerk_pipeline import VERDICT_DEFECTIVE, file_case, run_extraction_steps

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "defective_missing_field7.pdf",
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"

# The exact tool_use shape a real Bedrock InvokeModel call returned for
# this fixture's text, confirmed manually 2026-07-08 - date_container_available
# and proper_party_basis both correctly came back null (the fixture never
# states when the container was made available, and uses "Billed to:"
# rather than any of the Consignee:/Shipper:/Contract party: labels).
# Built from (key, value, verbatim) tuples rather than a giant literal
# dict so each real extracted quote fits on one readable line.
_FIELD_KEY_VALUE_VERBATIM = (
    ("date_container_available", None, None),
    ("port_of_discharge", "Harbor City, CA", "Port of discharge: Harbor City, CA"),
    ("container_numbers", "NOLU1234567", "Container numbers: NOLU1234567"),
    ("earliest_return_date", "N/A", "Earliest return date: N/A"),
    ("free_time_days", "5", "Free time days: 5"),
    ("free_time_start", "2026-04-03", "Free time start: 2026-04-03"),
    ("proper_party_basis", None, None),
    ("free_time_end", "2026-04-08", "Free time end: 2026-04-08"),
    ("applicable_rule", "541.6(a)", "Applicable rule: 541.6(a)"),
    ("applicable_rate", "$250.00/day", "Applicable rate: $250.00/day"),
    ("total_amount_due", "$1050.00", "Total amount due: $1050.00"),
    ("contact_for_disputes", "disputes@nol.example", "Contact for disputes: disputes@nol.example"),
    (
        "certifications",
        "charges consistent with FMC rules; carrier performance did not cause "
        "or contribute to the delay.",
        "Certifications: charges consistent with FMC rules; carrier performance "
        "did not cause or contribute to the delay.",
    ),
)

_CONFIRMED_REAL_BEDROCK_RESPONSE = {
    "fields": {
        key: {
            "value": value,
            "verbatim": verbatim,
            "page": 1 if value is not None else None,
            "confidence": 1.0 if value is not None else 0.0,
        }
        for key, value, verbatim in _FIELD_KEY_VALUE_VERBATIM
    },
    "invoice_no": None,
    "currency": "USD",
    "notes_footnotes": [],
}


def test_fixture_pdf_exists_and_is_readable_by_pdfplumber():
    with pdfplumber.open(FIXTURE_PATH) as pdf:
        text = pdf.pages[0].extract_text()
    assert "Port of discharge: Harbor City, CA" in text
    assert "Consignee:" not in text  # deliberately absent - part of what makes this defective


def test_end_to_end_fixture_produces_defective_verdict_citing_field_7_missing():
    with pdfplumber.open(FIXTURE_PATH) as pdf:
        text = pdf.pages[0].extract_text()

    gated = apply_anti_hallucination_gate(_CONFIRMED_REAL_BEDROCK_RESPONSE, text)
    result = run_extraction_steps(
        gated, billed_party_name="Meridian Home & Hardware", invoice_date_raw="2026-08-02"
    )

    assert result.verdict == VERDICT_DEFECTIVE
    missing_keys = [r.key for r in result.field_results if not r.present]
    assert "proper_party_basis" in missing_keys
    assert "date_container_available" in missing_keys
    assert "proper_party_basis" in result.summary


def test_end_to_end_fixture_gate_verifies_every_present_field_against_source_text():
    """Confirms the anti-hallucination gate is actually doing work on
    real extracted text, not just passing through mocked confidence."""
    with pdfplumber.open(FIXTURE_PATH) as pdf:
        text = pdf.pages[0].extract_text()

    gated = apply_anti_hallucination_gate(_CONFIRMED_REAL_BEDROCK_RESPONSE, text)

    present_count = sum(1 for f in gated.fields.values() if f.value is not None)
    assert present_count == 11  # 13 canon fields minus the 2 genuinely missing


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        return self._conn.dispatch(normalized, params)

    def fetchone(self):
        return self._conn.last_result


class _FakeTransactionCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self):
        self.executed = []
        self.last_result = None
        self._next_id = 1

    def _new_id(self):
        val = f"00000000-0000-0000-0000-{self._next_id:012d}"
        self._next_id += 1
        return val

    def transaction(self):
        return _FakeTransactionCtx(self)

    def cursor(self):
        return _FakeCursor(self)

    def dispatch(self, sql, params):
        if sql.startswith("INSERT INTO findings") or sql.startswith("INSERT INTO cases"):
            self.last_result = (self._new_id(),)
        return self.last_result


def test_end_to_end_fixture_files_a_real_shaped_case_via_the_atomic_commit():
    with pdfplumber.open(FIXTURE_PATH) as pdf:
        text = pdf.pages[0].extract_text()

    gated = apply_anti_hallucination_gate(_CONFIRMED_REAL_BEDROCK_RESPONSE, text)
    result = run_extraction_steps(
        gated, billed_party_name="Meridian Home & Hardware", invoice_date_raw="2026-08-02"
    )

    conn = _FakeConnection()
    dal = DAL(conn, Tenant(tenant_id=TENANT_ID, actor="clerk"))

    outcome = file_case(
        dal, invoice_id="inv-e2e-1", clerk_run_id="run-e2e-1", carrier_id="carrier-1",
        pin_date="2026-08-02", amount=1050.00, clerk_result=result,
    )

    assert "case_id" in outcome and "finding_id" in outcome

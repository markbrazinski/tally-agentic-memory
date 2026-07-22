"""Database-bound Gate 1 receipt-pipeline tests with no external services."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest

from src.core.receipt import (
    InvoiceClaim,
    TariffExtraction,
    verify_tariff_extraction,
)
from src.external.dal import DAL, Tenant
from src.external.versioned_source import RetainedObject
from src.platform.receipt_pipeline import (
    EvidenceConflictError,
    ExistingReceiptConflictError,
    TemporalEvidenceError,
    file_gate1_case,
    persist_verified_inputs,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"
TARIFF_BODY = (
    b"Section 4 - Import demurrage: The rate is USD $250.00 per day for each "
    b"chargeable day after free time."
)


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._rows: list[tuple] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql: str, params: tuple = ()):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        if self._conn.fail_on and self._conn.fail_on in normalized:
            raise psycopg.errors.UndefinedTable(f"induced failure: {self._conn.fail_on}")
        self._rows = self._conn.dispatch(normalized, params)
        self.description = [("result",)] if normalized.startswith("SELECT") else None
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeTransaction:
    _STATE_NAMES = (
        "snapshots",
        "clauses",
        "invoices",
        "clerk_runs",
        "findings",
        "cases",
        "case_evidence",
        "invoice_updates",
        "query_log_rows",
    )

    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._before: dict[str, object] = {}

    def __enter__(self):
        self._conn.transaction_entries += 1
        self._before = {
            name: copy.deepcopy(getattr(self._conn, name)) for name in self._STATE_NAMES
        }
        return self

    def __exit__(self, *exc_info):
        if exc_info[0] is not None:
            self._conn.rollbacks += 1
            for name, value in self._before.items():
                setattr(self._conn, name, value)
        else:
            self._conn.commits += 1
        return False


class FakeConnection:
    """Small stateful SQL double for both pipeline transactions."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.snapshots: list[dict] = []
        self.clauses: list[dict] = []
        self.invoices: list[dict] = []
        self.clerk_runs: list[dict] = []
        self.findings: list[dict] = []
        self.cases: list[dict] = []
        self.case_evidence: list[dict] = []
        self.invoice_updates: list[str] = []
        self.query_log_rows: list[dict] = []
        self.fail_on: str | None = None
        self.transaction_entries = 0
        self.commits = 0
        self.rollbacks = 0
        self._next_id = 1

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return value

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction(self)

    def dispatch(self, sql: str, params: tuple) -> list[tuple]:
        if sql.startswith("INSERT INTO tariff_snapshots"):
            tenant_id, carrier_id, lane, captured_at = params[0], params[1], params[2], params[5]
            existing = self._find_snapshot(tenant_id, carrier_id, lane, captured_at)
            if existing:
                return []
            row = {
                "id": self._new_id("snapshot"),
                "tenant_id": tenant_id,
                "carrier_id": carrier_id,
                "lane": lane,
                "captured_at": captured_at,
                "committed_at": "2026-07-20T12:00:00Z",
                "s3_key": params[7],
                "source_version_id": params[8],
                "source_byte_size": params[9],
                "doc_sha256": params[10],
                "doc_text": params[11],
                "source_url": params[6],
            }
            self.snapshots.append(row)
            return [(row["id"], row["committed_at"])]

        if sql.startswith("SELECT id, committed_at") and "FROM tariff_snapshots" in sql:
            row = self._find_snapshot(*params)
            return [(
                row["id"], row["committed_at"], row["s3_key"],
                row["source_version_id"], row["source_byte_size"],
                row["doc_sha256"], row["doc_text"], row["source_url"],
            )] if row else []

        if sql.startswith("INSERT INTO tariff_clauses"):
            tenant_id, snapshot_id, clause_sha = params[0], params[2], params[9]
            existing = self._find_clause(tenant_id, snapshot_id, clause_sha)
            if existing:
                return []
            row = {
                "id": self._new_id("clause"),
                "tenant_id": tenant_id,
                "snapshot_id": snapshot_id,
                "sha256": clause_sha,
                "committed_at": "2026-07-20T12:00:01Z",
                "carrier_id": params[1],
                "clause_ref": params[3],
                "clause_text": params[4],
                "rate_amount": params[5],
                "rate_currency": params[6],
                "rate_unit": params[7],
                "free_time_basis": params[8],
                "effective_from": params[10],
                "effective_to": params[11],
                "source_locator": params[12],
                "confidence": params[13],
                "verification_status": "VERIFIED",
                "verification_reason": params[14],
            }
            self.clauses.append(row)
            return [(row["id"], row["committed_at"])]

        if sql.startswith("SELECT id, committed_at, carrier_id"):
            row = self._find_clause(*params)
            return [(
                row["id"], row["committed_at"], row["carrier_id"], row["clause_ref"],
                row["clause_text"], row["rate_amount"], row["rate_currency"],
                row["rate_unit"], row["free_time_basis"], row["effective_from"],
                row["effective_to"], row["source_locator"], row["confidence"],
                row["verification_status"], row["verification_reason"],
            )] if row else []

        if sql.startswith("INSERT INTO invoices"):
            tenant_id, invoice_sha = params[0], params[6]
            existing = self._find_invoice(tenant_id, invoice_sha)
            if existing:
                return []
            row = {
                "id": self._new_id("invoice"),
                "tenant_id": tenant_id,
                "sha256": invoice_sha,
                "status": "EXTRACTED",
                "s3_key": params[4],
                "source_version_id": params[5],
                "raw_text": params[7],
                "claimed_rate": params[12],
                "currency": params[10],
                "invoice_date": params[11],
                "rate_unit": params[13],
                "charge_days": params[14],
                "received_at": params[3],
            }
            self.invoices.append(row)
            return [(row["id"],)]

        if sql.startswith("SELECT id, s3_key, source_version_id"):
            row = self._find_invoice(*params)
            return [(
                row["id"], row["s3_key"], row["source_version_id"], row["sha256"],
                row["raw_text"], row["claimed_rate"], row["currency"],
                row["invoice_date"], row["rate_unit"], row["charge_days"],
                row["received_at"],
            )] if row else []

        if sql.startswith("SELECT id FROM clerk_runs"):
            tenant_id, invoice_id = params
            row = next(
                (
                    item
                    for item in self.clerk_runs
                    if item["tenant_id"] == tenant_id and item["invoice_id"] == invoice_id
                ),
                None,
            )
            return [(row["id"],)] if row else []

        if sql.startswith("INSERT INTO clerk_runs"):
            row = {
                "id": self._new_id("run"),
                "tenant_id": params[0],
                "invoice_id": params[1],
            }
            self.clerk_runs.append(row)
            return [(row["id"],)]

        if sql.startswith("SELECT c.id, c.finding_id"):
            tenant_id, invoice_id = params
            row = next(
                (
                    item
                    for item in self.cases
                    if item["tenant_id"] == tenant_id and item["invoice_id"] == invoice_id
                ),
                None,
            )
            if row is None:
                return []
            finding = next(item for item in self.findings if item["id"] == row["finding_id"])
            evidence = next(
                item for item in self.case_evidence if item["params"][1] == row["id"]
            )
            return [(
                row["id"], row["finding_id"], finding["params"][10],
                finding["params"][16], finding["params"][15], evidence["params"][2],
                evidence["params"][3], evidence["params"][4], evidence["params"][5],
            )]

        if sql.startswith("INSERT INTO findings"):
            row = {"id": self._new_id("finding"), "params": params}
            self.findings.append(row)
            return [(row["id"],)]

        if sql.startswith("INSERT INTO cases"):
            row = {
                "id": self._new_id("case"),
                "tenant_id": params[0],
                "invoice_id": params[1],
                "finding_id": params[2],
                "params": params,
            }
            self.cases.append(row)
            return [(row["id"],)]

        if sql.startswith("INSERT INTO case_evidence"):
            self.case_evidence.append({"params": params})
            return []

        if sql.startswith("UPDATE invoices"):
            invoice_id = params[1]
            self.invoice_updates.append(invoice_id)
            for row in self.invoices:
                if row["tenant_id"] == params[0] and row["id"] == invoice_id:
                    row["status"] = "ANALYZED"
            return []

        if sql.startswith("INSERT INTO query_log"):
            self.query_log_rows.append({"params": params})
            return []

        return []

    def _find_snapshot(self, tenant_id, carrier_id, lane, captured_at):
        return next(
            (
                row
                for row in self.snapshots
                if (row["tenant_id"], row["carrier_id"], row["lane"], row["captured_at"])
                == (tenant_id, carrier_id, lane, captured_at)
            ),
            None,
        )

    def _find_clause(self, tenant_id, snapshot_id, clause_sha):
        return next(
            (
                row
                for row in self.clauses
                if (row["tenant_id"], row["snapshot_id"], row["sha256"])
                == (tenant_id, snapshot_id, clause_sha)
            ),
            None,
        )

    def _find_invoice(self, tenant_id, invoice_sha):
        return next(
            (
                row
                for row in self.invoices
                if (row["tenant_id"], row["sha256"]) == (tenant_id, invoice_sha)
            ),
            None,
        )


def _extraction(rate: str = "250.00") -> TariffExtraction:
    return TariffExtraction.from_mapping(
        {
            "rate_amount": rate,
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "rate_text": "USD $250.00 per day",
            "effective_from": "2026-07-01",
            "effective_to": "2026-12-31",
            "clause_text": TARIFF_BODY.decode(),
            "source_locator": "Section 4",
            "confidence": "0.99",
        }
    )


def _claim(claimed_rate: str = "350.00") -> InvoiceClaim:
    return InvoiceClaim(
        invoice_no="INV-NOL-0001",
        claimed_rate=Decimal(claimed_rate),
        rate_currency="USD",
        rate_unit="per_day",
        charge_days=7,
        invoice_date=date(2026, 7, 10),
        received_at="2026-07-10T16:00:00Z",
    )


def _objects() -> tuple[RetainedObject, RetainedObject]:
    tariff = RetainedObject(
        bucket="example-bucket",
        key="fixtures/tariff.txt",
        version_id="fixture-tariff-v1",
        body=TARIFF_BODY,
        observed_at=datetime(2026, 7, 2, 8, tzinfo=UTC),
    )
    invoice = RetainedObject(
        bucket="example-bucket",
        key="fixtures/invoice.json",
        version_id="fixture-invoice-v1",
        body=b'{"invoice_no":"INV-NOL-0001"}',
        observed_at=datetime(2026, 7, 10, 16, tzinfo=UTC),
    )
    return tariff, invoice


def _dal(conn: FakeConnection) -> DAL:
    return DAL(conn, Tenant(tenant_id=TENANT_ID, actor="gate1-test"))


def _persist(conn: FakeConnection, *, claimed_rate: str = "350.00"):
    tariff, invoice = _objects()
    extraction = _extraction()
    verification = verify_tariff_extraction(tariff.body, extraction)
    claim = _claim(claimed_rate)
    stored = persist_verified_inputs(
        _dal(conn),
        carrier_id="carrier-fixture",
        lane="NP-SP",
        tariff=tariff,
        tariff_source_url="https://fixtures.example.invalid/tariff",
        extraction=extraction,
        verification=verification,
        invoice=invoice,
        invoice_claim=claim,
    )
    return stored, tariff, invoice, extraction, verification, claim


def test_verified_inputs_insert_once_and_rerun_reuses_all_ids():
    conn = FakeConnection()

    first, *_ = _persist(conn)
    second, *_ = _persist(conn)

    assert second == first
    assert len(conn.snapshots) == 1
    assert len(conn.clauses) == 1
    assert len(conn.invoices) == 1
    assert len(conn.clerk_runs) == 1
    assert conn.commits == 2


def test_snapshot_idempotency_conflict_fails_closed():
    conn = FakeConnection()
    _persist(conn)
    conn.snapshots[0]["source_version_id"] = "different-version"

    with pytest.raises(EvidenceConflictError, match="tariff snapshot"):
        _persist(conn)


def test_invoice_idempotency_conflict_fails_closed():
    conn = FakeConnection()
    _persist(conn)
    conn.invoices[0]["source_version_id"] = "different-version"

    with pytest.raises(EvidenceConflictError, match="invoice"):
        _persist(conn)


def test_clause_idempotency_conflict_fails_closed():
    conn = FakeConnection()
    _persist(conn)
    conn.clauses[0]["rate_unit"] = None

    with pytest.raises(EvidenceConflictError, match="tariff clause"):
        _persist(conn)


def test_clause_reuse_ignores_nondeterministic_confidence_metadata():
    conn = FakeConnection()
    first, *_ = _persist(conn)
    conn.clauses[0]["confidence"] = Decimal("0.5000")
    conn.clauses[0]["source_locator"] = "equivalent locator spelling"

    second, *_ = _persist(conn)

    assert second.clause_id == first.clause_id
    assert len(conn.clauses) == 1


def test_tariff_must_apply_on_invoice_date():
    conn = FakeConnection()
    tariff, invoice = _objects()
    extraction = _extraction()
    verification = verify_tariff_extraction(tariff.body, extraction)
    claim = _claim()
    claim = InvoiceClaim(
        **{**claim.__dict__, "invoice_date": date(2027, 1, 1)}
    )

    with pytest.raises(TemporalEvidenceError, match="not effective"):
        persist_verified_inputs(
            _dal(conn), carrier_id="carrier-fixture", lane="NP-SP", tariff=tariff,
            tariff_source_url="https://fixtures.example.invalid/tariff",
            extraction=extraction, verification=verification, invoice=invoice,
            invoice_claim=claim,
        )


def test_tariff_observation_must_precede_invoice_observation():
    conn = FakeConnection()
    tariff, invoice = _objects()
    tariff = RetainedObject(
        bucket=tariff.bucket, key=tariff.key, version_id=tariff.version_id,
        body=tariff.body, observed_at=datetime(2026, 7, 12, tzinfo=UTC),
    )
    extraction = _extraction()
    verification = verify_tariff_extraction(tariff.body, extraction)

    with pytest.raises(TemporalEvidenceError, match="before the invoice"):
        persist_verified_inputs(
            _dal(conn), carrier_id="carrier-fixture", lane="NP-SP", tariff=tariff,
            tariff_source_url="https://fixtures.example.invalid/tariff",
            extraction=extraction, verification=verification, invoice=invoice,
            invoice_claim=_claim(),
        )


def test_unverified_tariff_refuses_before_any_database_work():
    conn = FakeConnection()
    tariff, invoice = _objects()
    bad_extraction = _extraction("251.00")
    verification = verify_tariff_extraction(tariff.body, bad_extraction)
    assert verification.eligible is False

    with pytest.raises(ValueError, match="not eligible"):
        persist_verified_inputs(
            _dal(conn),
            carrier_id="carrier-fixture",
            lane="NP-SP",
            tariff=tariff,
            tariff_source_url="https://fixtures.example.invalid/tariff",
            extraction=bad_extraction,
            verification=verification,
            invoice=invoice,
            invoice_claim=_claim(),
        )

    assert conn.executed == []
    assert conn.transaction_entries == 0


def test_equal_250_claim_files_nothing_and_does_not_touch_filing_tables():
    conn = FakeConnection()
    stored, tariff, invoice, extraction, verification, claim = _persist(conn, claimed_rate="250.00")
    statements_before = len(conn.executed)

    result = file_gate1_case(
        _dal(conn),
        carrier_id="carrier-fixture",
        stored=stored,
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=claim,
        pin_date=date(2026, 7, 11),
    )

    assert result["filed"] is False
    assert result["calculation"]["overcharge"] == "0.00"
    assert len(conn.executed) == statements_before
    assert conn.findings == conn.cases == conn.case_evidence == []


def test_overcharge_writes_one_finding_case_and_bound_evidence_then_rerun_reuses_case():
    conn = FakeConnection()
    stored, tariff, invoice, extraction, verification, claim = _persist(conn)

    first = file_gate1_case(
        _dal(conn),
        carrier_id="carrier-fixture",
        stored=stored,
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=claim,
        pin_date=date(2026, 7, 11),
    )
    second = file_gate1_case(
        _dal(conn),
        carrier_id="carrier-fixture",
        stored=stored,
        tariff=tariff,
        invoice=invoice,
        extraction=extraction,
        verification=verification,
        invoice_claim=claim,
        pin_date=date(2026, 7, 11),
    )

    assert first["filed"] is True and first["already_filed"] is False
    assert second["filed"] is True and second["already_filed"] is True
    assert second["case_id"] == first["case_id"]
    assert second["finding_id"] == first["finding_id"]
    assert first["calculation"]["overcharge"] == "700.00"
    assert len(conn.findings) == 1
    assert len(conn.cases) == 1
    assert len(conn.case_evidence) == 1
    assert conn.invoice_updates == [stored.invoice_id]


def test_existing_case_with_different_evidence_fails_closed():
    conn = FakeConnection()
    stored, tariff, invoice, extraction, verification, claim = _persist(conn)
    file_gate1_case(
        _dal(conn), carrier_id="carrier-fixture", stored=stored, tariff=tariff,
        invoice=invoice, extraction=extraction, verification=verification,
        invoice_claim=claim, pin_date=date(2026, 7, 11),
    )
    params = list(conn.case_evidence[0]["params"])
    content = json.loads(params[5])
    content["s3_version_id"] = "different-version"
    params[5] = json.dumps(content)
    conn.case_evidence[0]["params"] = tuple(params)

    with pytest.raises(ExistingReceiptConflictError, match="different receipt evidence"):
        file_gate1_case(
            _dal(conn), carrier_id="carrier-fixture", stored=stored, tariff=tariff,
            invoice=invoice, extraction=extraction, verification=verification,
            invoice_claim=claim, pin_date=date(2026, 7, 11),
        )


def test_induced_filing_failure_rolls_back_finding_case_evidence_and_invoice_update():
    conn = FakeConnection()
    stored, tariff, invoice, extraction, verification, claim = _persist(conn)
    conn.fail_on = "UPDATE invoices"

    with pytest.raises(psycopg.errors.UndefinedTable, match="induced failure"):
        file_gate1_case(
            _dal(conn),
            carrier_id="carrier-fixture",
            stored=stored,
            tariff=tariff,
            invoice=invoice,
            extraction=extraction,
            verification=verification,
            invoice_claim=claim,
            pin_date=date(2026, 7, 11),
        )

    assert conn.rollbacks == 1
    assert conn.findings == []
    assert conn.cases == []
    assert conn.case_evidence == []
    assert conn.invoice_updates == []
    assert conn.invoices[0]["status"] == "EXTRACTED"

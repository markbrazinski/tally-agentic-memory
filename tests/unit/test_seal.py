"""Unit tests for src/platform/seal.py's atomic seal transaction.

Per CLAUDE.md ("All external calls mocked in tests"), uses an in-memory
fake connection - no real CockroachDB call happens here. Live end-to-end
verification against the real cluster is done manually per session
notes, same pattern as B0-S1/S2's DAL and clerk_pipeline modules.
"""

from __future__ import annotations

import json
from decimal import Decimal

import psycopg
import pytest

from src.external.dal import DAL, Tenant
from src.platform.seal import (
    ApprovalRecordError,
    CaseNotFoundError,
    CaseNotSealableError,
    EmptyEvidenceError,
    EvidenceIntegrityError,
    SealedCaseIntegrityError,
    _build_manifest,
    _content_hash,
    _evidence_hash,
    seal_case,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"
CASE_ID = "10000000-0000-4000-8000-000000000001"
CARRIER_ID = "00000000-0000-0000-0000-000000000002"
USER_ID = "00000000-0000-0000-0000-000000000003"


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last_result = None
        self._last_rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        if self._conn.fail_on and self._conn.fail_on in normalized:
            raise psycopg.errors.UndefinedTable(f"induced failure: {self._conn.fail_on}")
        self._last_result, self._last_rows = self._conn.dispatch(normalized, params)
        return self

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._last_rows


class FakeTransactionCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if exc_info[0] is not None:
            self._conn.rolled_back = True
            self._conn.case_state = self._conn.original_case_state
            self._conn.ledger_events.clear()
            self._conn.query_log_rows.clear()
            self._conn.evidence_sealed = False
            self._conn.finding_approval_state = "NOT_PRESSED"
            self._conn.existing_evidence_hash = None
            self._conn.existing_manifest = None
            self._conn.existing_sealed_txn_ts = None
            self._conn.existing_sealed_by = None
            self._conn.existing_sealed_at_display = None
            self._conn.existing_manifest_version = None
        return False


class FakeConnection:
    """Simulates one case row + its evidence rows across the seal
    transaction's several statements."""

    def __init__(
        self,
        case_state: str = "ANALYZED",
        evidence_contents: tuple[dict, ...] | None = None,
        evidence_hashes: tuple[str, ...] | None = None,
        existing_sealed_txn_ts=None,
        existing_evidence_hash=None,
        existing_manifest=None,
        existing_sealed_by=None,
        existing_sealed_at_display=None,
        existing_manifest_version=None,
        fail_on: str | None = None,
        case_exists: bool = True,
        finding_exists: bool = True,
        finding_approval_state: str | None = None,
        evidence_sealed: bool | None = None,
    ):
        self.executed: list[tuple] = []
        self.case_state = case_state
        self.original_case_state = case_state
        self.case_exists = case_exists
        self.finding_exists = finding_exists
        self.evidence_contents = (
            (
                {"calculation": {"recommendation": "dispute_overcharge"}, "ordinal": 1},
                {"calculation": {"recommendation": "dispute_overcharge"}, "ordinal": 2},
            )
            if evidence_contents is None
            else evidence_contents
        )
        self.evidence_hashes = (
            tuple(_content_hash(content) for content in self.evidence_contents)
            if evidence_hashes is None
            else evidence_hashes
        )
        terminal = case_state == "FILED"
        self.evidence_sealed = terminal if evidence_sealed is None else evidence_sealed
        self.finding_approval_state = (
            ("APPROVED" if terminal else "NOT_PRESSED")
            if finding_approval_state is None
            else finding_approval_state
        )
        self.existing_sealed_txn_ts = (
            Decimal("1783534098823702432.0000000000")
            if terminal and existing_sealed_txn_ts is None
            else existing_sealed_txn_ts
        )
        self.existing_sealed_by = (
            USER_ID if terminal and existing_sealed_by is None else existing_sealed_by
        )
        self.existing_sealed_at_display = (
            "2026-04-18T14:02:11Z"
            if terminal and existing_sealed_at_display is None
            else existing_sealed_at_display
        )
        self.existing_manifest_version = (
            1 if terminal and existing_manifest_version is None else existing_manifest_version
        )
        if terminal and existing_manifest is None:
            existing_manifest = _build_manifest(
                tenant_id=TENANT_ID,
                case_id=CASE_ID,
                invoice_id="invoice-1",
                finding_id="finding-1",
                evidence_rows=self._evidence_rows(),
                approved_by=str(self.existing_sealed_by),
                approved_at=str(self.existing_sealed_at_display),
            )
        self.existing_manifest = existing_manifest
        self.existing_evidence_hash = (
            _evidence_hash(existing_manifest)
            if terminal and existing_manifest is not None and existing_evidence_hash is None
            else existing_evidence_hash
        )
        self.fail_on = fail_on
        self.ledger_events: list[dict] = []
        self.query_log_rows: list[dict] = []
        self.rolled_back = False

    def transaction(self):
        return FakeTransactionCtx(self)

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def _evidence_rows(self):
        return [
            (
                f"evidence-{i}",
                "tariff_fact",
                "tariff_clauses",
                f"clause-{i}",
                content,
                content_hash,
                self.evidence_sealed,
            )
            for i, (content, content_hash) in enumerate(
                zip(self.evidence_contents, self.evidence_hashes, strict=True), 1
            )
        ]

    def dispatch(self, sql, params):
        if sql.startswith("SELECT state, sealed_txn_ts, evidence_hash, invoice_id"):
            if not self.case_exists:
                return None, []
            row = (
                self.case_state,
                self.existing_sealed_txn_ts,
                self.existing_evidence_hash,
                "invoice-1",
                "finding-1",
                CARRIER_ID,
                "draft dispute text",
                self.existing_manifest,
                self.existing_sealed_by,
                self.existing_sealed_at_display,
                self.existing_manifest_version,
            )
            return row, []
        if sql.startswith("SELECT id, kind, source_table, source_id, content, content_sha256"):
            return None, self._evidence_rows()
        if sql.startswith("SELECT human_approval_state FROM findings"):
            if not self.finding_exists:
                return None, []
            return (self.finding_approval_state,), []
        if sql.startswith("UPDATE case_evidence SET sealed=true"):
            self.evidence_sealed = True
            return None, []
        if sql.startswith("UPDATE cases SET state='FILED'"):
            self.case_state = "FILED"
            sealed_txn_ts = Decimal("1783534098823702432.0000000000")
            self.existing_sealed_txn_ts = sealed_txn_ts
            self.existing_sealed_at_display = params[0]
            self.existing_sealed_by = params[1]
            self.existing_evidence_hash = params[2]
            self.existing_manifest = json.loads(params[3])
            self.existing_manifest_version = 1
            return (
                sealed_txn_ts,
                CARRIER_ID,
                "draft dispute text",
                self.existing_sealed_by,
                self.existing_sealed_at_display,
                self.existing_manifest_version,
            ), []
        if sql.startswith("UPDATE findings SET human_approval_state='APPROVED'"):
            if not self.finding_exists:
                return None, []
            self.finding_approval_state = "APPROVED"
            return None, [("finding-1",)]
        if sql.startswith("INSERT INTO ledger_events"):
            self.ledger_events.append({"params": params})
            return None, []
        if sql.startswith("INSERT INTO query_log"):
            self.query_log_rows.append({"params": params})
            return None, []
        return None, []


def _dal(conn: FakeConnection) -> DAL:
    return DAL(conn, Tenant(tenant_id=TENANT_ID, actor="rachel.martinez"))


def test_seal_case_not_found_raises():
    conn = FakeConnection(case_exists=False)
    with pytest.raises(CaseNotFoundError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )


def test_seal_case_blocked_state_raises_not_sealable():
    conn = FakeConnection(case_state="CONTESTED")
    with pytest.raises(CaseNotSealableError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )


def test_seal_case_resolved_state_also_blocked():
    conn = FakeConnection(case_state="RESOLVED")
    with pytest.raises(CaseNotSealableError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )


def test_seal_case_fresh_seal_flips_state_and_seals_evidence():
    conn = FakeConnection(case_state="ANALYZED")
    result = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )

    assert result["already_sealed"] is False
    assert result["state"] == "FILED"
    assert conn.evidence_sealed is True
    assert conn.case_state == "FILED"


def test_seal_case_fresh_seal_writes_one_ledger_event():
    conn = FakeConnection()
    seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    assert len(conn.ledger_events) == 1


def test_seal_case_fresh_seal_writes_one_audit_query_log_row():
    conn = FakeConnection()
    seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    assert len(conn.query_log_rows) == 1
    sql_text = conn.query_log_rows[0]["params"][1]
    assert "APPROVE & FILE" in sql_text
    assert CASE_ID in sql_text


def test_seal_case_evidence_hash_is_canonical_manifest_sha256():
    conn = FakeConnection()
    result = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    expected = _evidence_hash(result["evidence_manifest"])
    assert result["evidence_hash"] == expected
    assert result["evidence_hash"].startswith("sha256:")
    assert result["evidence_manifest"]["manifest_version"] == 1
    assert len(result["evidence_manifest"]["evidence"]) == 2
    first = result["evidence_manifest"]["evidence"][0]
    assert first["content"] == conn.evidence_contents[0]
    assert first["content_sha256"] == _content_hash(first["content"])


def test_seal_case_evidence_hash_is_deterministic_for_same_manifest():
    conn_a = FakeConnection()
    conn_b = FakeConnection()
    result_a = seal_case(
        _dal(conn_a), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    result_b = seal_case(
        _dal(conn_b), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    assert result_a["evidence_hash"] == result_b["evidence_hash"]


def test_seal_case_altered_evidence_changes_manifest_hash():
    original = (
        {"calculation": {"recommendation": "dispute_overcharge"}, "ordinal": 1},
        {"calculation": {"recommendation": "dispute_overcharge"}, "ordinal": 2},
    )
    altered = (original[0], {**original[1], "ordinal": 99})
    result_a = seal_case(
        _dal(FakeConnection(evidence_contents=original)),
        case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-07-11T14:02:11Z",
    )
    result_b = seal_case(
        _dal(FakeConnection(evidence_contents=altered)),
        case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-07-11T14:02:11Z",
    )

    assert result_a["evidence_hash"] != result_b["evidence_hash"]


def test_seal_case_empty_evidence_is_rejected_before_writes():
    conn = FakeConnection(evidence_contents=())

    with pytest.raises(EmptyEvidenceError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-07-11T14:02:11Z",
        )

    assert conn.case_state == "ANALYZED"
    assert conn.ledger_events == []


def test_seal_case_records_human_approval_separately_from_recommendation():
    conn = FakeConnection()

    result = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-07-11T14:02:11Z",
    )

    assert result["evidence_manifest"]["recommendation"] == "dispute_overcharge"
    assert result["evidence_manifest"]["approved_by"] == USER_ID
    assert result["sealed_by"] == USER_ID
    assert result["sealed_at_display"] == "2026-07-11T14:02:11Z"
    assert result["manifest_version"] == 1
    assert conn.finding_approval_state == "APPROVED"
    ledger_details = json.loads(conn.ledger_events[0]["params"][3])
    assert ledger_details["approved_by"] == USER_ID
    assert ledger_details["approved_at"] == "2026-07-11T14:02:11Z"
    assert ledger_details["evidence_hash"] == result["evidence_hash"]


def test_seal_case_response_matches_contract_fixture_shape():
    conn = FakeConnection()
    result = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )
    fixture_keys = {"state", "sealed_at_display", "sealed_txn_ts", "evidence_hash",
                     "evidence_count", "commit_line"}
    assert fixture_keys.issubset(set(result.keys()))
    assert result["evidence_count"] == 2
    assert "single transaction" in result["commit_line"]


def test_seal_case_already_filed_is_idempotent_no_write():
    """The double-press case (TDD §7): a second Approve on an
    already-FILED case validates existing state without rewriting cases,
    case_evidence, findings, or ledger events."""
    conn = FakeConnection(
        case_state="FILED",
        existing_sealed_txn_ts=Decimal("1783534098823702432.0000000000"),
    )
    result = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-04-18T14:02:11Z",
    )

    assert result["already_sealed"] is True
    assert result["evidence_hash"] == conn.existing_evidence_hash
    assert conn.evidence_sealed is True
    assert not any(sql.startswith("UPDATE case_evidence") for sql, _ in conn.executed)
    assert not any(sql.startswith("UPDATE cases") for sql, _ in conn.executed)
    assert not any(sql.startswith("UPDATE findings") for sql, _ in conn.executed)
    assert conn.ledger_events == []  # no new ledger event
    # the idempotent path still logs one query_log row (disclosure, not silence)
    assert len(conn.query_log_rows) == 1
    assert "already sealed" in conn.query_log_rows[0]["params"][1]


def test_approval_timestamp_is_canonical_utc_across_database_round_trip():
    conn = FakeConnection()

    first = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-07-11T10:02:11+01:00",
    )
    second = seal_case(
        _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
        sealed_at_display="2026-07-11T09:02:11Z",
    )

    assert first["sealed_at_display"] == "2026-07-11T09:02:11Z"
    assert first["evidence_manifest"]["approved_at"] == "2026-07-11T09:02:11Z"
    assert second["already_sealed"] is True
    assert second["evidence_hash"] == first["evidence_hash"]


def test_seal_case_legacy_sealed_state_fails_closed_as_unknown():
    conn = FakeConnection(case_state="SEALED")

    with pytest.raises(CaseNotSealableError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )

    assert conn.ledger_events == []


def test_seal_case_unknown_state_fails_closed_without_writes():
    conn = FakeConnection(case_state="CANCELLED")

    with pytest.raises(CaseNotSealableError):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )

    assert conn.evidence_sealed is False
    assert conn.ledger_events == []
    assert conn.query_log_rows == []


def test_seal_case_rejects_tampered_canonical_evidence_hash_and_rolls_back():
    conn = FakeConnection(evidence_hashes=("0" * 64, "1" * 64))

    with pytest.raises(EvidenceIntegrityError, match="canonical content hash mismatch"):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )

    assert conn.rolled_back is True
    assert conn.case_state == "ANALYZED"
    assert conn.ledger_events == []


def test_seal_case_rejects_filed_case_with_tampered_manifest_hash():
    conn = FakeConnection(case_state="FILED")
    conn.existing_evidence_hash = "sha256:" + "0" * 64

    with pytest.raises(SealedCaseIntegrityError, match="manifest hash"):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )

    assert conn.query_log_rows == []
    assert conn.ledger_events == []


def test_seal_case_rejects_filed_case_without_stored_approval():
    conn = FakeConnection(case_state="FILED", finding_approval_state="NOT_PRESSED")

    with pytest.raises(SealedCaseIntegrityError, match="human approval"):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )


def test_seal_case_missing_finding_approval_aborts_and_rolls_back_every_write():
    conn = FakeConnection(finding_exists=False)

    with pytest.raises(ApprovalRecordError, match="exactly one"):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )

    assert conn.rolled_back is True
    assert conn.case_state == "ANALYZED"
    assert conn.evidence_sealed is False
    assert conn.ledger_events == []


def test_seal_case_induced_failure_leaves_state_unchanged():
    """Induced failure on the ledger_events insert must roll back the
    cases/case_evidence writes too - one transaction, all or nothing."""
    conn = FakeConnection(case_state="ANALYZED", fail_on="INSERT INTO ledger_events")
    with pytest.raises(psycopg.errors.UndefinedTable):
        seal_case(
            _dal(conn), case_id=CASE_ID, sealed_by_user_id=USER_ID,
            sealed_at_display="2026-04-18T14:02:11Z",
        )
    assert conn.rolled_back is True
    assert conn.case_state == "ANALYZED"  # rolled back to pre-seal state

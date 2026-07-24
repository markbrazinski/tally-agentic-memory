from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.external.dal import DAL, Tenant
from src.external.invoice_source_store import StoredInvoiceSource
from src.platform.intake_repository import (
    FinalizeReceipt,
    IdempotencyConflictError,
    complete_duplicate_ingestion,
    finalize_received_invoice,
    find_invoice_by_sha,
    record_source_stored,
    reserve_ingestion,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.executed.append((normalized, params))
        if normalized.startswith("INSERT INTO ingestion_requests"):
            key = (params[0], params[1])
            self.conn.ingestions.setdefault(
                key,
                {
                    "request_hash": params[2],
                    "state": "RESERVED",
                    "invoice_id": params[5],
                    "source_id": params[6],
                    "snapshot": None,
                    "stored": (None, None, None, None, None),
                },
            )
        elif normalized.startswith(
            "SELECT request_hash, state, reserved_invoice_id, reserved_source_id"
        ):
            value = self.conn.ingestions[(params[0], params[1])]
            self.one = (
                value["request_hash"],
                value["state"],
                value["invoice_id"],
                value["source_id"],
                value["snapshot"],
                *value["stored"],
            )
        elif normalized.startswith("SELECT request_hash, state FROM ingestion_requests"):
            value = self.conn.ingestions[(params[0], params[1])]
            self.one = (value["request_hash"], value["state"])
        elif normalized.startswith("SELECT i.id, s.id"):
            self.one = self.conn.invoice_by_sha.get((params[0], params[1]))
        elif normalized.startswith(
            "SELECT request_hash, state, response_snapshot"
        ):
            value = self.conn.ingestions[(params[0], params[1])]
            self.one = (
                value["request_hash"],
                value["state"],
                value["snapshot"],
            )
        elif normalized.startswith("SELECT 1 FROM invoices i"):
            expected = self.conn.invoice_by_sha.get((params[0], params[3]))
            self.one = (1,) if expected == (params[1], params[2]) else None
        elif normalized.startswith("UPDATE ingestion_requests") and (
            "SOURCE_STORED_DB_PENDING" in normalized
        ):
            value = self.conn.ingestions[(params[5], params[6])]
            value["state"] = "SOURCE_STORED_DB_PENDING"
            value["stored"] = tuple(params[:5])
        elif normalized == "SELECT now();":
            self.one = (NOW,)
        elif normalized.startswith("INSERT INTO "):
            table = normalized.removeprefix("INSERT INTO ").split()[0]
            self.conn.insert_counts[table] = self.conn.insert_counts.get(table, 0) + 1
        elif normalized.startswith("UPDATE ingestion_requests") and "COMPLETED" in normalized:
            if "deduplicated_invoice_id" in normalized:
                value = self.conn.ingestions[(params[3], params[4])]
                value["duplicate"] = (params[1], params[2])
            else:
                value = self.conn.ingestions[(params[2], params[3])]
            value["state"] = "COMPLETED"
            value["snapshot"] = json.loads(params[0])
        return self

    def fetchone(self):
        return self.one


class FakeConnection:
    def __init__(self):
        self.ingestions = {}
        self.invoice_by_sha = {}
        self.insert_counts = {}
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction()

    def close(self):
        pass


def _dal():
    return DAL(FakeConnection(), Tenant(tenant_id=TENANT_ID, actor="reviewer"))


def _stored():
    return StoredInvoiceSource(
        bucket_ref_private="private-bucket",
        object_key_private="invoice-sources/source/invoice.pdf",
        version_id_private="version-1",
        sha256="a" * 64,
        byte_length=123,
    )


def test_reservation_is_idempotent_and_conflicts_on_different_payload():
    dal = _dal()

    first = reserve_ingestion(
        dal,
        idempotency_key="upload-1",
        request_hash="request-a",
        actor_id=None,
        actor_display="reviewer",
    )
    replay = reserve_ingestion(
        dal,
        idempotency_key="upload-1",
        request_hash="request-a",
        actor_id=None,
        actor_display="reviewer",
    )

    assert replay.invoice_id == first.invoice_id
    assert replay.source_id == first.source_id
    assert first.is_new is True
    assert replay.is_new is False
    assert replay.stored_source is None
    with pytest.raises(IdempotencyConflictError, match="IDEMPOTENCY_CONFLICT"):
        reserve_ingestion(
            dal,
            idempotency_key="upload-1",
            request_hash="request-b",
            actor_id=None,
            actor_display="reviewer",
        )


def test_finalize_creates_one_invoice_source_task_event_and_outbox():
    dal = _dal()
    reservation = reserve_ingestion(
        dal,
        idempotency_key="upload-1",
        request_hash="request-a",
        actor_id=None,
        actor_display="reviewer",
    )
    record_source_stored(
        dal,
        idempotency_key="upload-1",
        request_hash="request-a",
        source=_stored(),
    )
    pending = reserve_ingestion(
        dal,
        idempotency_key="upload-1",
        request_hash="request-a",
        actor_id=None,
        actor_display="reviewer",
    )
    assert pending.state == "SOURCE_STORED_DB_PENDING"
    assert pending.stored_source == _stored()
    command = FinalizeReceipt(
        idempotency_key="upload-1",
        request_hash="request-a",
        carrier_id="10000000-0000-4000-8000-000000000010",
        display_name="INV-1048.pdf",
        mime_type="application/pdf",
        stored_source=_stored(),
        actor_id=None,
        actor_display="reviewer",
        provenance_classification="DEMO_SCENARIO",
        public_disclosure="Representative demonstration invoice.",
    )

    first = finalize_received_invoice(dal, command)
    replay = finalize_received_invoice(dal, command)

    assert first == replay
    assert first["invoice"]["invoice_id"] == reservation.invoice_id
    assert first["invoice"]["status"] == "RECEIVED"
    assert first["invoice"]["status_sequence"] == 1
    assert "sha256" not in json.dumps(first).lower()
    assert "version-1" not in json.dumps(first)
    assert dal.conn.insert_counts == {
        "invoices": 1,
        "invoice_sources": 1,
        "workflow_tasks": 1,
        "invoice_events": 1,
        "event_outbox": 1,
    }


def test_duplicate_source_completes_request_without_new_domain_records():
    dal = _dal()
    invoice_id = "10000000-0000-4000-8000-000000000020"
    source_id = "10000000-0000-4000-8000-000000000021"
    dal.conn.invoice_by_sha[(TENANT_ID, "a" * 64)] = (invoice_id, source_id)
    reserve_ingestion(
        dal,
        idempotency_key="upload-duplicate",
        request_hash="request-duplicate",
        actor_id=None,
        actor_display="reviewer",
    )
    snapshot = {"invoice": {"invoice_id": invoice_id}}

    assert find_invoice_by_sha(dal, "a" * 64) == (invoice_id, source_id)
    result = complete_duplicate_ingestion(
        dal,
        idempotency_key="upload-duplicate",
        request_hash="request-duplicate",
        invoice_id=invoice_id,
        source_id=source_id,
        source_sha256="a" * 64,
        response_snapshot=snapshot,
    )
    replay = complete_duplicate_ingestion(
        dal,
        idempotency_key="upload-duplicate",
        request_hash="request-duplicate",
        invoice_id=invoice_id,
        source_id=source_id,
        source_sha256="a" * 64,
        response_snapshot=snapshot,
    )

    assert result == replay == snapshot
    ingestion = dal.conn.ingestions[(TENANT_ID, "upload-duplicate")]
    assert ingestion["duplicate"] == (invoice_id, source_id)
    assert dal.conn.insert_counts == {}

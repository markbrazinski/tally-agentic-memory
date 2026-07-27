"""P4 gap-driven access-evidence binding: transactional repository behaviour.

Proves (zero live DB, FakeConn): a passing verification records its verdict
durably, flips only the June-11 snapshot PENDING->VERIFIED, emits
reconstruction.source_bound, and enqueues a fresh START_RECONSTRUCTION; a
failing verification records a REFUSED verdict and does NOT bind, emit, or
enqueue (fail closed, no fallback); a repeat run is idempotent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from src.external.dal import DAL, Tenant
from src.platform.access_evidence_repository import (
    AccessEvidenceLease,
    complete_access_binding,
)

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _lease(worker_id="w1"):
    return AccessEvidenceLease(
        task_id="task-ev-1", invoice_id="inv-1", attempt=1, worker_id=worker_id,
        knowledge_cutoff_at=NOW, snapshot_public_ref="SE-INV1048-AX-0611",
        expected_container_ref="TLLU4829317", expected_date=date(2026, 6, 11),
        initiated_by=None, actor_display="Rachel",
    )


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT state, current_attempt, lease_owner FROM workflow_tasks"):
            self.one = ("RUNNING", 1, "w1")
        elif s.startswith("SELECT outcome, reason_code FROM access_evidence_verifications"):
            self.one = self.conn.prior_verdict
        elif s.startswith("SELECT m.public_ref, m.container_ref"):
            f = self.conn.snapshot_facts
            self.one = (
                f["public_ref"], f["container_ref"], f["snapshot_date"],
                f["normalized_facts"], "TERMINAL_ACCESS_SNAPSHOT",
                f["source_state"],
            )
        elif s.startswith("INSERT INTO access_evidence_verifications"):
            self.conn.verdicts.append({"outcome": params[7], "reason": params[8]})
        elif s.startswith("UPDATE shipment_event_memory SET source_version_state='VERIFIED'"):
            self.conn.bound += 1
        elif s.startswith("SELECT status_sequence FROM invoices"):
            self.one = (5,)
        elif s.startswith("SELECT now()"):
            self.one = (NOW,)
        elif s.startswith("INSERT INTO invoice_events"):
            self.conn.events.append(params[4])  # event_type
        elif s.startswith("INSERT INTO event_outbox"):
            self.conn.outbox += 1
        elif s.startswith("INSERT INTO workflow_tasks"):
            self.conn.enqueued.append(params[3] if len(params) > 3 else None)
        elif s.startswith("UPDATE invoices"):
            pass
        elif s.startswith("UPDATE workflow_tasks SET state='COMPLETED'"):
            self.conn.finished += 1
        return self

    def fetchone(self):
        return self.one


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, facts, prior_verdict=None):
        self.snapshot_facts = facts
        self.prior_verdict = prior_verdict
        self.verdicts = []
        self.bound = 0
        self.events = []
        self.outbox = 0
        self.enqueued = []
        self.finished = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "access-evidence-worker"))


_GOOD = {
    "public_ref": "SE-INV1048-AX-0611", "container_ref": "TLLU4829317",
    "snapshot_date": date(2026, 6, 11), "source_state": "VERIFIED",
    "normalized_facts": {"access_status": "AVAILABLE", "gate_status": "OPEN",
                         "blocking_hold": "NONE"},
}


def test_passing_verification_binds_and_requeues():
    conn = FakeConn(_GOOD)
    result = complete_access_binding(_dal(conn), lease=_lease())
    assert result.outcome == "VERIFIED"
    assert conn.verdicts == [{"outcome": "VERIFIED", "reason": None}]  # durable, before bind
    assert conn.bound == 1  # PENDING -> VERIFIED on exactly the June-11 row
    assert "reconstruction.source_bound" in conn.events
    assert result.reconstruction_requeued and len(conn.enqueued) == 1


def test_wrong_container_refuses_and_does_not_bind():
    bad = {**_GOOD, "container_ref": "MSCU0000000"}
    conn = FakeConn(bad)
    result = complete_access_binding(_dal(conn), lease=_lease())
    assert result.outcome == "REFUSED" and result.reason_code == "CONTAINER_MISMATCH"
    assert conn.verdicts == [{"outcome": "REFUSED", "reason": "CONTAINER_MISMATCH"}]
    assert conn.bound == 0 and conn.enqueued == []  # fail closed, no fallback
    assert "reconstruction.source_bound" not in conn.events


def test_unavailable_source_refuses():
    bad = {**_GOOD, "source_state": "PENDING"}
    conn = FakeConn(bad)
    result = complete_access_binding(_dal(conn), lease=_lease())
    assert result.outcome == "REFUSED" and result.reason_code == "SOURCE_VERSION_UNAVAILABLE"
    assert conn.bound == 0


def test_idempotent_when_already_verified():
    conn = FakeConn(_GOOD, prior_verdict=("VERIFIED", None))
    result = complete_access_binding(_dal(conn), lease=_lease())
    assert result.outcome == "VERIFIED"
    assert conn.bound == 0 and conn.enqueued == []  # no second bind/enqueue
    assert conn.verdicts == []  # no second verdict row

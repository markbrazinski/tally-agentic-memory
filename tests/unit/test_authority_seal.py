"""Gate 5 seal-transaction tests: approval bound to frozen version, stale
rejection, idempotent replay, conflict, atomic binding of all inputs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.external.dal import DAL, Tenant
from src.platform.authority_seal_repository import (
    ApprovalConflictError,
    RecommendationNotApprovableError,
    StaleRecommendationError,
    approve_and_seal,
)

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
REC_ID = "recommendation-1"
DIGEST = "sha256:frozen-digest"


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):  # noqa: C901 - prefix dispatch
        n = " ".join(sql.split())
        p = params or ()
        self.one = None
        self._rows = []
        if n == "SELECT now();":
            self.one = (NOW,)
        elif n.startswith("SELECT id, reconstruction_id, invoice_id, version, digest"):
            r = self.conn.rec
            self.one = (REC_ID, "recon-1", "invoice-1", r["version"], r["digest"],
                        r["state"], "DISPUTE", "rule-1", 70000, 175000, 245000, "USD")
        elif n.startswith("SELECT id, state, request_hash, response_snapshot FROM approvals"):
            self.one = self.conn.existing_approval
        elif n.startswith("SELECT active_claim_set_version FROM invoices"):
            self.one = (1,)
        elif n.startswith("SELECT s3_object_key_private"):
            self.one = ("intake/inv-1", "v1")
        elif n.startswith("SELECT version FROM reconstructions"):
            self.one = (1,)
        elif n.startswith("SELECT public_ref, rate_minor, currency, effective_from"):
            self.one = ("RULE-1", 25000, "USD", date(2026, 6, 1), None)
        elif n.startswith("SELECT charge_date, invoice_rate_minor"):
            self._rows = [
                (date(2026, 6, d), 35000, 25000, 10000, "RATE_DISCREPANCY")
                for d in range(8, 15)
            ]
        elif n.startswith("SELECT COALESCE(max(revision),0)+1 FROM decision_seals"):
            self.one = (self.conn.next_revision,)
        elif n.startswith("SELECT status_sequence FROM invoices"):
            self.one = (self.conn.seq,)
        elif n.startswith("INSERT INTO approvals"):
            self.conn.approvals += 1
        elif n.startswith("INSERT INTO decision_seals"):
            self.conn.seals += 1
            self.conn.bound_refs = p[12]
        elif n.startswith("INSERT INTO invoice_events"):
            # event_type is a SQL literal ('decision.sealed'), not a param.
            self.conn.events.append("decision.sealed" if "decision.sealed" in n
                                    else "other")
        elif n.startswith("INSERT INTO event_outbox"):
            self.conn.outbox += 1
        elif n.startswith("INSERT INTO query_log"):
            self.conn.audit += 1
        elif n.startswith("UPDATE invoices"):
            self.conn.seq += p[3]
        return self

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, *, state="FROZEN", version=1, digest=DIGEST,
                 existing_approval=None, next_revision=1):
        self.rec = {"state": state, "version": version, "digest": digest}
        self.existing_approval = existing_approval
        self.next_revision = next_revision
        self.seq = 5
        self.approvals = 0
        self.seals = 0
        self.events = []
        self.outbox = 0
        self.audit = 0
        self.bound_refs = None

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "rachel.martinez"))


def _approve(conn, *, expected_version=1, expected_digest=DIGEST, key="idem-1"):
    return approve_and_seal(
        _dal(conn), recommendation_id=REC_ID, expected_version=expected_version,
        expected_digest=expected_digest, idempotency_key=key,
        approver_user_id="user-1", approver_display="rachel.martinez",
    )


def test_approve_binds_and_seals_atomically():
    conn = FakeConn()
    result = _approve(conn)
    assert not result.already_sealed
    assert result.recommendation_type == "DISPUTE"
    assert result.seal_digest.startswith("sha256:")
    assert conn.approvals == 1
    assert conn.seals == 1
    assert "decision.sealed" in conn.events
    assert conn.outbox == 1
    assert conn.audit == 1  # in-transaction audit row
    # all exact inputs bound
    import json
    refs = json.loads(conn.bound_refs)
    types = {r["type"] for r in refs}
    assert {"recommendation", "reconstruction", "claim_set", "applicable_rule"} <= types


def test_stale_version_rejected():
    conn = FakeConn(version=2)  # recommendation advanced to v2
    with pytest.raises(StaleRecommendationError):
        _approve(conn, expected_version=1)
    assert conn.seals == 0


def test_stale_digest_rejected():
    conn = FakeConn(digest="sha256:new-digest")
    with pytest.raises(StaleRecommendationError):
        _approve(conn, expected_digest="sha256:old-digest")
    assert conn.seals == 0


def test_not_frozen_rejected():
    conn = FakeConn(state="SUPERSEDED")
    with pytest.raises(RecommendationNotApprovableError):
        _approve(conn)
    assert conn.seals == 0


def test_idempotent_replay_returns_existing_no_second_seal():
    request_hash = _request_hash()
    snapshot = '{"seal_id":"seal-existing","revision":1,' \
               '"seal_digest":"sha256:x","recommendation_type":"DISPUTE"}'
    conn = FakeConn(existing_approval=("appr-existing", "APPROVED", request_hash,
                                       snapshot))
    result = _approve(conn)
    assert result.already_sealed
    assert result.seal_id == "seal-existing"
    assert conn.seals == 0  # no second seal written


def test_same_key_different_request_conflicts():
    conn = FakeConn(existing_approval=("appr-existing", "APPROVED",
                                       "different-request-hash", "{}"))
    with pytest.raises(ApprovalConflictError):
        _approve(conn)
    assert conn.seals == 0


def _request_hash():
    from src.core.seal_manifest import approval_request_hash
    return approval_request_hash(
        recommendation_id=REC_ID, recommendation_version=1,
        recommendation_digest=DIGEST, decision="APPROVE",
    )

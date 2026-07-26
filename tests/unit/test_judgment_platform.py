"""Gate 4 platform tests: worker orchestration + repository transaction.

Proves: the worker computes the recommendation from persisted days (no model);
the repository freezes one immutable recommendation + N judgment rows atomically;
idempotent replay on the fingerprint; late-worker fencing; empty days fail.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.core.judgment import DayInput
from src.external.dal import DAL, Tenant
from src.platform import judgment_worker as worker
from src.platform.intake_tasks import TaskLeaseLostError
from src.platform.judgment_repository import (
    JudgmentTaskLease,
    complete_judgment,
)

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
HERO_DATES = [date(2026, 6, d) for d in range(8, 15)]


def _hero_inputs(invoice=35000, applicable=25000):
    return [
        DayInput(d, invoice, applicable, "USD", "PRESENT_VERIFIED", True)
        for d in HERO_DATES
    ]


def _lease(worker_id="w1"):
    return JudgmentTaskLease(
        task_id="task-1", invoice_id="invoice-1", reconstruction_id="recon-1",
        attempt=1, worker_id=worker_id, input_fingerprint="fp-1",
        initiated_by=None, actor_display="Rachel",
    )


# ---- worker orchestration (monkeypatched repository) ----

def test_worker_completes_from_persisted_days(monkeypatch):
    monkeypatch.setattr(worker, "claim_next_judgment_task", lambda *a, **k: _lease())
    monkeypatch.setattr(worker, "load_day_inputs", lambda *a, **k: _hero_inputs())
    captured = {}
    monkeypatch.setattr(
        worker, "complete_judgment",
        lambda dal, **k: captured.update(k) or _fake_completion("DISPUTE", 70000),
    )
    result = worker.run_one_judgment_task(object(), worker_id="w1")
    assert result.recommendation_type == "DISPUTE"
    assert len(captured["days"]) == 7


def test_worker_no_days_fails(monkeypatch):
    monkeypatch.setattr(worker, "claim_next_judgment_task", lambda *a, **k: _lease())
    monkeypatch.setattr(worker, "load_day_inputs", lambda *a, **k: [])
    failed = {}
    monkeypatch.setattr(worker, "fail_judgment",
                        lambda dal, **k: failed.update(k) or "BLOCKED")
    result = worker.run_one_judgment_task(object(), worker_id="w1")
    assert result is None
    assert failed["error_code"] == "NO_CHARGED_DAYS"


def _fake_completion(rec_type, disputed):
    from src.platform.judgment_repository import JudgmentCompletion
    return JudgmentCompletion("rec-1", 1, rec_type, disputed, 175000, 7)


# ---- repository transaction (in-memory fake DB) ----

class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        p = params or ()
        self.one = None
        if n == "SELECT now();":
            self.one = (NOW,)
        elif n.startswith("SELECT state, current_attempt, lease_owner FROM workflow_tasks"):
            t = self.conn.task
            self.one = (t["state"], t["current_attempt"], t["lease_owner"]) if t else None
        elif n.startswith("SELECT id, version, recommendation_type"):
            self.one = self.conn.existing_rec
        elif n.startswith("SELECT COALESCE(max(version),0)+1 FROM recommendations"):
            self.one = (1,)
        elif n.startswith("SELECT id FROM applicable_rules"):
            self.one = ("rule-1",)
        elif n.startswith("SELECT status_sequence FROM invoices"):
            self.one = (self.conn.seq,)
        elif n.startswith("INSERT INTO recommendations"):
            self.conn.recommendations.append(p[7])  # recommendation_type
            self.conn.reason_codes.append(p[-1])  # reason_codes JSON (last param)
        elif n.startswith("INSERT INTO charged_day_judgments"):
            self.conn.judgments += 1
        elif n.startswith("INSERT INTO invoice_events"):
            self.conn.events.append(p[4])
        elif n.startswith("INSERT INTO event_outbox"):
            self.conn.outbox += 1
        elif n.startswith("UPDATE invoices"):
            self.conn.seq += p[3]
        return self

    def fetchone(self):
        return self.one


class FakeConn:
    def __init__(self, existing_rec=None):
        self.task = {"state": "RUNNING", "current_attempt": 1, "lease_owner": "w1"}
        self.existing_rec = existing_rec
        self.seq = 5
        self.recommendations = []
        self.reason_codes = []
        self.judgments = 0
        self.events = []
        self.outbox = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "judgment-worker"))


def test_freezes_recommendation_and_judgments():
    conn = FakeConn()
    result = complete_judgment(_dal(conn), lease=_lease(), days=_hero_inputs())
    assert result.recommendation_type == "DISPUTE"
    assert result.disputed_amount_minor == 70000
    assert len(conn.recommendations) == 1
    assert conn.judgments == 7  # one judgment row per day
    assert "decision.recommendation_ready" in conn.events
    assert conn.outbox == 1


def test_restraint_875_approve():
    conn = FakeConn()
    result = complete_judgment(_dal(conn), lease=_lease(), days=_hero_inputs(12500, 12500))
    assert result.recommendation_type == "APPROVE_FOR_PAYMENT"
    assert result.supported_amount_minor == 87500


def _six_of_seven_inputs():
    """The authority-transition revision 1: six sourced days + June 11 missing
    source coverage. Delta §2.9 proof item 1."""
    days = []
    for d in HERO_DATES:
        if d == date(2026, 6, 11):
            days.append(DayInput(d, 35000, None, "USD", "MISSING", True))
        else:
            days.append(DayInput(d, 35000, 25000, "USD", "PRESENT_VERIFIED", True))
    return days


def test_six_of_seven_withholds_authority():
    # 6/7 coverage: the recommendation must be REQUEST_EVIDENCE, project
    # NEEDS_EVIDENCE (never READY_FOR_REVIEW), emit decision.authority_withheld,
    # and record MISSING_DAY_SOURCE. No financial (DISPUTE) amount is authorized.
    conn = FakeConn()
    result = complete_judgment(_dal(conn), lease=_lease(), days=_six_of_seven_inputs())
    assert result.recommendation_type == "REQUEST_EVIDENCE"
    assert "decision.authority_withheld" in conn.events
    assert "decision.recommendation_ready" not in conn.events
    # reason_codes persisted on the recommendation row (param index 18).
    import json as _json
    codes = _json.loads(conn.reason_codes[0]) if conn.reason_codes else []
    assert "MISSING_DAY_SOURCE" in codes


def test_seven_of_seven_disputes_after_binding():
    # Revision 2: once June 11 is sourced, deterministic code reaches DISPUTE
    # 70000 and projects READY_FOR_REVIEW via decision.recommendation_ready.
    conn = FakeConn()
    result = complete_judgment(_dal(conn), lease=_lease(), days=_hero_inputs())
    assert result.recommendation_type == "DISPUTE"
    assert result.disputed_amount_minor == 70000
    assert "decision.recommendation_ready" in conn.events


def test_idempotent_replay_no_second_freeze():
    conn = FakeConn(existing_rec=("rec-existing", 1, "DISPUTE", 70000, 175000, 7))
    result = complete_judgment(_dal(conn), lease=_lease(), days=_hero_inputs())
    assert result.recommendation_id == "rec-existing"
    assert len(conn.recommendations) == 0  # nothing new frozen


def test_late_worker_fenced():
    conn = FakeConn()
    stale = JudgmentTaskLease(**{**_lease().__dict__, "worker_id": "w2"})
    with pytest.raises(TaskLeaseLostError):
        complete_judgment(_dal(conn), lease=stale, days=_hero_inputs())
    assert len(conn.recommendations) == 0

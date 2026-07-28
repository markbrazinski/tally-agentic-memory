"""Gate 3 repository transaction tests against an in-memory fake DB.

Proves: retrieval run + ranked candidates + applicable rule persist atomically;
a VERIFIED decision writes one applicable_rule and stamps applicable_rate_minor
on the charged days; a REJECTED/CONFLICTED decision writes NO applicable rule;
idempotent replay on the query fingerprint; late-worker fencing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from src.core.applicable_rule import (
    ApplicabilityQuery,
    RuleCandidate,
    decide_applicable_rule,
)
from src.external.dal import DAL, Tenant
from src.platform.applicable_rule_repository import (
    RuleTaskLease,
    complete_rule,
)
from src.platform.intake_tasks import TaskLeaseLostError

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
HERO_DATES = tuple(date(2026, 6, d) for d in range(8, 15))


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
        self.conn.log.append(n)
        self.one = None
        if n == "SELECT now();":
            self.one = (NOW,)
        elif n.startswith("SELECT state, current_attempt, lease_owner FROM workflow_tasks"):
            t = self.conn.task
            self.one = (t["state"], t["current_attempt"], t["lease_owner"]) if t else None
        elif n.startswith("SELECT id FROM rule_retrieval_runs"):
            self.one = self.conn.run_by_fp.get(p[2])
        elif n.startswith("SELECT candidate_count FROM rule_retrieval_runs"):
            self.one = (len(self.conn.candidates),)
        elif n.startswith("SELECT id, rate_minor, validation_state FROM applicable_rules"):
            self.one = None  # replay case: no applicable rule in this fake
        elif n.startswith("SELECT days_complete, days_total FROM reconstructions"):
            self.one = self.conn.coverage
        elif n.startswith("INSERT INTO workflow_tasks"):
            self.conn.judgment_emitted += 1
        elif n.startswith("SELECT status_sequence FROM invoices"):
            self.one = (self.conn.status_sequence,)
        elif n.startswith("INSERT INTO rule_retrieval_runs"):
            self.conn.runs.append(p[1])
            self.conn.run_by_fp[p[4]] = (p[1],)
        elif n.startswith("INSERT INTO rule_candidates"):
            self.conn.candidates.append({"state": p[7], "rejection": p[8]})
        elif n.startswith("INSERT INTO applicable_rules"):
            self.conn.applicable_rules.append({"rate_minor": p[9]})
        elif n.startswith("UPDATE reconstruction_charged_days"):
            self.conn.stamped_rate = p[0]
        elif n.startswith("INSERT INTO charged_day_rule_bindings"):
            self.conn.bindings += 1
        elif n.startswith("INSERT INTO invoice_events"):
            self.conn.events.append(p[4])
        elif n.startswith("INSERT INTO event_outbox"):
            self.conn.outbox += 1
        elif n.startswith("UPDATE invoices"):
            self.conn.status_sequence += p[3]
        return self

    def fetchone(self):
        return self.one


class FakeConn:
    def __init__(self):
        self.task = {"state": "RUNNING", "current_attempt": 1, "lease_owner": "w1"}
        self.status_sequence = 5
        self.runs = []
        self.run_by_fp = {}
        self.candidates = []
        self.applicable_rules = []
        self.events = []
        self.outbox = 0
        self.bindings = 0
        self.stamped_rate = None
        # (days_complete, days_total). Default: coverage complete.
        self.coverage = (7, 7)
        self.judgment_emitted = 0
        self.log = []

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "rule-worker"))


def _lease(worker="w1"):
    return RuleTaskLease(
        task_id="task-1", invoice_id="invoice-1", reconstruction_id="recon-1",
        attempt=1, worker_id=worker, knowledge_cutoff_at=NOW, input_fingerprint="fp-1",
        carrier_id="carrier-1", scope_code="DEMURRAGE:USOAK:DRY",
        charge_dates=tuple(d.isoformat() for d in HERO_DATES),
        invoice_currency="USD", initiated_by=None, actor_display="Rachel",
    )


def _candidate(rank=1, eff_from=date(2026, 6, 1), rate="250.00", ref="Clause 4.2"):
    return RuleCandidate(
        clause_id="c-" + ref, public_ref="RULE-" + ref, clause_ref=ref, rank=rank,
        distance=0.1, clause_text="Demurrage rate: $250 per calendar day.",
        display_excerpt="Demurrage rate: $250 per calendar day",
        rate_amount=Decimal(rate), rate_currency="USD", rate_unit="CALENDAR_DAY",
        effective_from=eff_from, effective_to=None, scope_code="DEMURRAGE:USOAK:DRY",
        equipment_type="DRY", route_code="USOAK", service_context=None,
        verification_status="VERIFIED", source_locator="s3://p", superseded=False,
    )


def _query():
    return ApplicabilityQuery(
        charged_dates=HERO_DATES, invoice_currency="USD",
        expected_unit="CALENDAR_DAY", scope_code="DEMURRAGE:USOAK:DRY",
        expected_rate_phrase="$250", equipment_type="DRY", route_code="USOAK",
    )


def _complete(conn, candidates, decision):
    return complete_rule(
        _dal(conn), lease=_lease(), query_text="demurrage ...",
        query_fingerprint="qfp-1", embedding_model="amazon.titan-embed-text-v2:0",
        embedding_input_sha256="e" * 64,
        vector_index_name="tariff_clause_embedding_search_idx",
        candidates=candidates, decision=decision,
    )


def test_verified_persists_rule_and_stamps_rate():
    conn = FakeConn()
    cands = [_candidate()]
    decision = decide_applicable_rule(cands, _query())
    result = _complete(conn, cands, decision)
    assert result.state == "VERIFIED"
    assert len(conn.runs) == 1
    assert len(conn.candidates) == 1
    assert len(conn.applicable_rules) == 1
    assert conn.stamped_rate == 25000  # applicable_rate_minor stamped on days
    assert conn.bindings == 1
    assert "evidence.rule_verified" in conn.events
    assert conn.outbox == 1


def test_rejected_with_complete_coverage_hands_off_to_judgment():
    # Demo v3 INV-1047: no verified rule BUT coverage complete → NEEDS_EVIDENCE
    # AND the judgment task is emitted, so the evaluator produces a genuine
    # REQUEST_EVIDENCE + RULE_NOT_VERIFIED recommendation (not a stalled task).
    conn = FakeConn()
    conn.coverage = (7, 7)
    cands = [_candidate(eff_from=date(2026, 7, 1))]  # wrong date → rejected
    decision = decide_applicable_rule(cands, _query())
    result = _complete(conn, cands, decision)
    assert result.state == "REJECTED"
    assert len(conn.applicable_rules) == 0  # no applicable rule
    assert conn.stamped_rate is None
    assert "evidence.rule_not_applicable" in conn.events
    assert conn.judgment_emitted == 1  # handed off to judgment


def test_rejected_with_incomplete_coverage_does_not_judge():
    # A reconstruction that is itself incomplete already refused at the
    # reconstruction stage; a no-rule outcome must NOT re-judge it.
    conn = FakeConn()
    conn.coverage = (6, 7)
    cands = [_candidate(eff_from=date(2026, 7, 1))]
    decision = decide_applicable_rule(cands, _query())
    result = _complete(conn, cands, decision)
    assert result.state == "REJECTED"
    assert conn.judgment_emitted == 0  # no handoff


def test_verified_still_hands_off_to_judgment():
    conn = FakeConn()
    cands = [_candidate()]
    decision = decide_applicable_rule(cands, _query())
    result = _complete(conn, cands, decision)
    assert result.state == "VERIFIED"
    assert conn.judgment_emitted == 1  # hero path unchanged


def test_conflicting_rates_writes_no_rule():
    conn = FakeConn()
    a = _candidate(rank=1, ref="A")
    b = _candidate(rank=2, ref="B", rate="300.00")
    b = RuleCandidate(**{**b.__dict__,
                         "clause_text": "Demurrage rate: $300 per calendar day."})
    q = ApplicabilityQuery(
        charged_dates=HERO_DATES, invoice_currency="USD",
        expected_unit="CALENDAR_DAY", scope_code="DEMURRAGE:USOAK:DRY",
        expected_rate_phrase="per calendar day", equipment_type="DRY",
        route_code="USOAK",
    )
    decision = decide_applicable_rule([a, b], q)
    result = _complete(conn, [a, b], decision)
    assert result.state == "CONFLICTED"
    assert len(conn.applicable_rules) == 0
    assert "evidence.rule_conflict" in conn.events


def test_idempotent_replay_on_fingerprint():
    conn = FakeConn()
    conn.run_by_fp["qfp-1"] = ("existing-run",)
    conn.runs.append("existing-run")
    # Simulate an already-persisted run; second delivery must not double-write.
    cands = [_candidate()]
    decision = decide_applicable_rule(cands, _query())
    result = _complete(conn, cands, decision)
    assert result.retrieval_run_id == "existing-run"
    assert len(conn.applicable_rules) == 0  # no new rule row written on replay


def test_late_worker_fenced():
    conn = FakeConn()
    conn.task = {"state": "RUNNING", "current_attempt": 1, "lease_owner": "w1"}
    stale = RuleTaskLease(**{**_lease().__dict__, "worker_id": "w2"})
    cands = [_candidate()]
    decision = decide_applicable_rule(cands, _query())
    with pytest.raises(TaskLeaseLostError):
        complete_rule(
            _dal(conn), lease=stale, query_text="q", query_fingerprint="qfp-1",
            embedding_model="m", embedding_input_sha256="e" * 64,
            vector_index_name="tariff_clause_embedding_search_idx",
            candidates=cands, decision=decision,
        )
    assert len(conn.applicable_rules) == 0

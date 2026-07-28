"""Projection + lease-helper tests: downstream contract shape and privacy.

The reconstruction projection is the ONE contract Gate 3/4 consume. These tests
prove its shape and — critically — that it never leaks private identifiers
(S3 bucket/key/version, SQL, correlation internals, private anchors).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from src.external.dal import DAL, Tenant
from src.platform.reconstruction_api import (
    _unresolved_reason,
    load_reconstruction_projection,
)
from src.platform.reconstruction_repository import _expand_charge_dates

NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def test_unresolved_reason_maps_missing_tariff():
    # Demo v3 INV-1047: RULE_NOT_VERIFIED surfaces as the plain refusal sentence.
    assert _unresolved_reason(["RULE_NOT_VERIFIED"]) == "Governing tariff not verified"
    # First recognized code wins; raw codes remain machine-facing.
    assert _unresolved_reason(
        ["MISSING_DAY_ACCESS_EVIDENCE"]
    ) == "Required terminal-access snapshot not yet bound"
    # No reason codes → nothing to resolve.
    assert _unresolved_reason([]) is None
    assert _unresolved_reason(["UNKNOWN_CODE"]) is None


def test_expand_charge_dates_seven_inclusive():
    by_field = {
        "period_start": ("2026-06-08", None, None),
        "period_end": ("2026-06-14", None, None),
    }
    dates = _expand_charge_dates(by_field)
    assert dates == tuple(f"2026-06-{d:02d}" for d in range(8, 15))
    assert len(dates) == 7


def test_expand_charge_dates_missing_returns_empty():
    assert _expand_charge_dates({}) == ()
    assert _expand_charge_dates({"period_start": ("2026-06-08", None, None)}) == ()


def test_expand_charge_dates_reversed_returns_empty():
    by_field = {
        "period_start": ("2026-06-14", None, None),
        "period_end": ("2026-06-08", None, None),
    }
    assert _expand_charge_dates(by_field) == ()


class _ProjCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.one = None
        self._rows = []
        if n.startswith("SELECT id, version, state, knowledge_cutoff_at"):
            self.one = self.conn.head
        elif n.startswith("SELECT public_ref, event_type, occurred_at"):
            self._rows = self.conn.timeline
        elif n.startswith("SELECT id, charge_date, chargeability"):
            self._rows = self.conn.days
        elif n.startswith("SELECT b.charged_day_id, e.public_ref, b.role"):
            self._rows = self.conn.bindings
        elif n.startswith("SELECT requirement_code, coverage_state, detail"):
            self._rows = self.conn.coverage
        elif n.startswith("SELECT public_ref, clause_ref, display_excerpt"):
            self.one = self.conn.applicable_rule
        return self

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self._rows


class _ProjConn:
    def __init__(self):
        self.head = (
            "recon-1", 1, "COMPLETE", NOW, "America/Los_Angeles",
            5, 7, 7, "7 charged days reconstructed with complete source coverage",
        )
        self.timeline = [
            ("SE-005", "GATE_OUT", NOW, NOW, True, "VERIFIED", "row SE-005",
             "DEMO_SCENARIO", 4),
        ]
        self.days = [
            ("day-1", date(2026, 6, 8), "CHARGEABLE", "PRESENT_VERIFIED",
             "SOURCE_COMPLETE", 35000, None, "USD", "PENDING", None, "[]"),
        ]
        self.bindings = [("day-1", "SE-005", "CHARGE_END")]
        self.coverage = [("GATE_OUT", "PRESENT_VERIFIED", None)]
        self.applicable_rule = None  # Gate 3 fills this; None until verified

    def cursor(self):
        return _ProjCursor(self)

    def transaction(self):
        raise AssertionError("projection is read-only, no transaction")

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant("10000000-0000-4000-8000-000000000002", "reader"))


def test_projection_shape_is_downstream_contract():
    projection = load_reconstruction_projection(_dal(_ProjConn()), invoice_id="invoice-1")
    assert projection["reconstruction_id"] == "recon-1"
    assert projection["version"] == 1
    assert projection["state"] == "COMPLETE"
    assert projection["source_disclosure"] == "Representative demonstration data"
    assert projection["coverage"]["days_total"] == 7
    assert projection["coverage"]["days_complete"] == 7
    day = projection["charged_days"][0]
    assert day["date"] == "2026-06-08"
    assert day["invoice_rate_minor"] == 35000
    assert day["applicable_rate_minor"] is None  # set by Gate 3/4, not Gate 2
    assert day["event_refs"] == ["SE-005"]
    event = projection["timeline"][0]
    assert event["recorded_before_invoice"] is True
    assert event["verification_state"] == "VERIFIED"


def test_projection_has_no_private_identifiers():
    import json

    projection = load_reconstruction_projection(_dal(_ProjConn()), invoice_id="invoice-1")
    blob = json.dumps(projection).lower()
    for forbidden in (
        "s3_bucket", "s3_object_key", "s3_version", "bucket", "sql",
        "mcp_query_ref", "correlation", "private", "select ",
    ):
        assert forbidden not in blob, f"private token leaked: {forbidden}"


def test_projection_includes_applicable_rule_when_verified():
    conn = _ProjConn()
    conn.applicable_rule = (
        "RULE-Clause 4.2", "Clause 4.2", "Demurrage rate: $250 per calendar day",
        25000, "USD", "CALENDAR_DAY", date(2026, 6, 1), None,
        "DEMURRAGE:USOAK:DRY", "VERIFIED",
    )
    projection = load_reconstruction_projection(_dal(conn), invoice_id="invoice-1")
    rule = projection["applicable_rule"]
    assert rule["rate_minor"] == 25000
    assert rule["validation_state"] == "VERIFIED"
    assert rule["retrieval"]["tool"] == "CockroachDB Distributed Vector Indexing"
    assert rule["retrieval"]["state"] == "RETRIEVED"  # retrieval != applicability


def test_projection_applicable_rule_none_until_gate3():
    projection = load_reconstruction_projection(_dal(_ProjConn()), invoice_id="invoice-1")
    assert projection["applicable_rule"] is None


def test_projection_missing_returns_none():
    class Empty(_ProjConn):
        def __init__(self):
            super().__init__()
            self.head = None

    assert load_reconstruction_projection(_dal(Empty()), invoice_id="x") is None

"""Reconstruction memory seed: the representative package + the June-11 gap.

Proves the load-bearing P3 contract without a live DB: the retained June-11
TERMINAL_ACCESS_SNAPSHOT is seeded PENDING (so the MCP view — which exposes only
source_version_state='VERIFIED' rows — excludes it in revision 1), while the six
other access snapshots and the boundary events are VERIFIED. The June-11 row
still EXISTS in memory from the start; the gap is its verification state, not its
absence. No second-pass insert is involved.
"""

from __future__ import annotations

from datetime import date, datetime

from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import load_source_package, seed_reconstruction_memory

TENANT = "10000000-0000-4000-8000-000000000002"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO shipment_event_memory"):
            # columns: ..., event_type(4), source_public_ref(5),
            # source_version_state(6), display_anchor(7), ..., effective_from(10)
            self.conn.events.append(
                {
                    "public_ref": params[1],
                    "event_type": params[4],
                    "source_version_state": params[6],
                    "effective_from": params[10],
                }
            )
        elif s.startswith("INSERT INTO reconstruction_source_artifacts"):
            self.conn.artifacts += 1
        return self

    def fetchone(self):
        return None


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self):
        self.events = []
        self.artifacts = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "reconstruction-seed"))


def test_seed_marks_only_june11_access_pending():
    conn = FakeConn()
    seed_reconstruction_memory(_dal(conn), invoice_id="inv-1")

    access = [e for e in conn.events if e["event_type"] == "TERMINAL_ACCESS_SNAPSHOT"]
    assert len(access) == 7  # one per charged day, June 8..14

    pending = [e for e in access if e["source_version_state"] == "PENDING"]
    verified = [e for e in access if e["source_version_state"] == "VERIFIED"]
    assert len(pending) == 1 and len(verified) == 6

    june11 = pending[0]
    assert june11["effective_from"] == date(2026, 6, 11)
    # The boundary events remain VERIFIED (retained, source-bound).
    boundary = [e for e in conn.events if e["event_type"] != "TERMINAL_ACCESS_SNAPSHOT"]
    assert all(e["source_version_state"] == "VERIFIED" for e in boundary)


def test_all_access_recorded_before_invoice():
    # Every access snapshot is retained pre-invoice (recorded_at < received_at,
    # the June-22 cutoff). Uses the fixture directly for the timestamps.
    pkg = load_source_package()
    cutoff = datetime.fromisoformat("2026-06-22T08:00:00-07:00")
    access = [e for e in pkg["events"] if e["event_type"] == "TERMINAL_ACCESS_SNAPSHOT"]
    assert len(access) == 7
    for e in access:
        assert datetime.fromisoformat(e["recorded_at"]) < cutoff

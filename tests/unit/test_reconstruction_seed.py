"""Reconstruction memory seed: the representative package + the June-11 gap.

Proves the load-bearing P3 contract without a live DB: the retained June-11
TERMINAL_ACCESS_SNAPSHOT is seeded PENDING (so the MCP view — which exposes only
source_version_state='VERIFIED' rows — excludes it in revision 1), while the six
other access snapshots and the boundary events are VERIFIED. The June-11 row
still EXISTS in memory from the start; the gap is its verification state, not its
absence. No second-pass insert is involved.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import load_source_package, seed_reconstruction_memory

TENANT = "10000000-0000-4000-8000-000000000002"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


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


def test_hero_seed_is_complete_memory_no_access_heartbeat():
    # Demo v3: the HERO (default) fixture is complete-memory. It carries only the
    # boundary events (availability / free-time / gate-out) and NO per-day
    # terminal-access snapshot, so reconstruction resolves 7/7 automatically —
    # no held source, no operator release. This is the filmed hero.
    from src.external.reconstruction_seed import load_source_package

    conn = FakeConn()
    seed_reconstruction_memory(_dal(conn), invoice_id="inv-1")
    access = [e for e in conn.events if e["event_type"] == "TERMINAL_ACCESS_SNAPSHOT"]
    assert access == []  # no access heartbeat in the hero
    # The default package ships only the boundary events + their two artifacts.
    pkg = load_source_package()
    assert not any(e["event_type"] == "TERMINAL_ACCESS_SNAPSHOT" for e in pkg["events"])
    assert {a["public_ref"] for a in pkg["source_artifacts"]} == {
        "SRC-MILESTONE-INV-1048", "SRC-AVAILABILITY-INV-1048",
    }


def test_incomplete_memory_safety_fixture_marks_only_june11_pending():
    # Preserved fail-closed proof (Demo v3 "technical proof only"): the ISOLATED
    # incomplete-memory fixture still holds June-11 PENDING among 7 access
    # snapshots. Loaded explicitly — it never drives the hero seed.
    from src.external.reconstruction_seed import (
        INCOMPLETE_MEMORY_FIXTURE,
        load_source_package,
    )

    pkg = load_source_package(INCOMPLETE_MEMORY_FIXTURE)
    conn = FakeConn()
    seed_reconstruction_memory(_dal(conn), invoice_id="inv-1", package=pkg)

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


def _shipment_memory_columns() -> set[str]:
    """Columns of shipment_event_memory as the migrations actually define them:
    the CREATE TABLE body (011) plus any ADD COLUMN (017). Catches a seed/verifier
    that references a column no migration ever added (the normalized_facts gap)."""
    cols: set[str] = set()
    ddl = "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")))
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS shipment_event_memory\s*\((.*?)\n\);",
        ddl, re.DOTALL,
    ).group(1)
    for line in body.splitlines():
        m = re.match(r"\s*([a-z_]+)\s+(?:UUID|STRING|TIMESTAMPTZ|DATE|JSONB)\b", line)
        if m:
            cols.add(m.group(1))
    for m in re.finditer(
        r"ALTER TABLE shipment_event_memory\s+ADD COLUMN IF NOT EXISTS\s+([a-z_]+)",
        ddl,
    ):
        cols.add(m.group(1))
    return cols


def test_seed_insert_columns_exist_in_schema():
    # The seed's shipment_event_memory INSERT must reference only real columns.
    # normalized_facts was queried by the verifier + carried by the fixture but
    # never added to the table — a mismatch fakes can't catch. Guard it here.
    import inspect

    import src.external.reconstruction_seed as seed_mod

    src = inspect.getsource(seed_mod)
    insert = re.search(
        r"INSERT INTO shipment_event_memory\s*\((.*?)\)\s*VALUES", src, re.DOTALL
    ).group(1)
    used = {c.strip() for c in insert.replace("\n", " ").split(",") if c.strip()}
    schema = _shipment_memory_columns()
    assert "normalized_facts" in schema, "migration must define normalized_facts"
    missing = used - schema
    assert not missing, f"seed inserts columns absent from schema: {missing}"


def test_all_access_recorded_before_invoice():
    # Every access snapshot in the ISOLATED incomplete-memory proof is retained
    # pre-invoice (recorded_at < received_at, the June-22 cutoff). This property
    # belongs to the fail-closed test fixture, not the complete-memory hero.
    from src.external.reconstruction_seed import INCOMPLETE_MEMORY_FIXTURE

    pkg = load_source_package(INCOMPLETE_MEMORY_FIXTURE)
    cutoff = datetime.fromisoformat("2026-06-22T08:00:00-07:00")
    access = [e for e in pkg["events"] if e["event_type"] == "TERMINAL_ACCESS_SNAPSHOT"]
    assert len(access) == 7
    for e in access:
        assert datetime.fromisoformat(e["recorded_at"]) < cutoff

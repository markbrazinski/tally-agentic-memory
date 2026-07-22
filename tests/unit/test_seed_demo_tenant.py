"""Unit tests for src/external/seed_demo_tenant.py's run_seed().

Mocked per CLAUDE.md ("zero network calls in the test suite") - same
FakeConnection/FakeCursor spirit as tests/unit/test_commit.py, sized for
run_seed()'s own surface (context-manager connect(), fetchone()-based
tenant lookup, three INSERT shapes: tenants, carriers, users).

run_seed() sits on the unattended production tick path (capture/tick.py's
_resolve_tenant_id() calls it on every capture Lambda invocation) - this
suite is what CLAUDE.md's "a fallback isn't real if it isn't tested" bar
requires for code reachable from that path, extended here to cover the
new users INSERT added in this session (previously the whole module had
zero test coverage).
"""

from __future__ import annotations

from unittest.mock import patch

from src.external.seed_demo_tenant import DEMO_CARRIERS, DEMO_TENANT_NAME, DEMO_USER, run_seed

EXISTING_TENANT_ID = "10000000-0000-4000-8000-000000000002"


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql: str, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        self._last_result = self._conn.dispatch(sql, params)
        return self

    def fetchone(self):
        return self._last_result


class FakeConnection:
    """Tracks every executed statement; simulates tenant-exists vs.
    tenant-missing by whether existing_tenant_id is set."""

    def __init__(self, existing_tenant_id: str | None = None):
        self.executed: list[tuple[str, object]] = []
        self.existing_tenant_id = existing_tenant_id

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def cursor(self):
        return FakeCursor(self)

    def dispatch(self, sql: str, params):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id FROM tenants"):
            return (self.existing_tenant_id,) if self.existing_tenant_id else None
        if normalized.startswith("INSERT INTO tenants"):
            return (EXISTING_TENANT_ID,)
        return None


def test_run_seed_creates_tenant_when_none_exists():
    conn = FakeConnection(existing_tenant_id=None)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        tenant_id = run_seed()

    assert tenant_id == EXISTING_TENANT_ID
    insert_tenant_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO tenants")]
    assert len(insert_tenant_calls) == 1
    assert insert_tenant_calls[0][1] == (DEMO_TENANT_NAME,)


def test_run_seed_reuses_existing_tenant_without_inserting():
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        tenant_id = run_seed()

    assert tenant_id == EXISTING_TENANT_ID
    insert_tenant_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO tenants")]
    assert insert_tenant_calls == []


def test_run_seed_inserts_one_carrier_row_per_demo_carrier():
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        run_seed()

    carrier_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO carriers")]
    assert len(carrier_calls) == len(DEMO_CARRIERS)


def test_run_seed_carrier_insert_uses_on_conflict_do_nothing_for_idempotency():
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        run_seed()

    carrier_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO carriers")]
    assert all("ON CONFLICT (tenant_id, scac) DO NOTHING" in sql for sql, _ in carrier_calls)


def test_run_seed_inserts_rachel_martinez_as_approver():
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        run_seed()

    user_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO users")]
    assert len(user_calls) == 1
    sql, params = user_calls[0]
    assert params == (
        EXISTING_TENANT_ID,
        DEMO_USER["email"],
        DEMO_USER["display_name"],
        DEMO_USER["title"],
        DEMO_USER["role"],
    )
    assert DEMO_USER["role"] == "approver"


def test_run_seed_user_insert_uses_on_conflict_do_nothing_for_idempotency():
    """Matches users_email_idx UNIQUE INDEX (tenant_id, email) in
    migrations/002_bundle0_schema.sql - re-running run_seed() against an
    already-seeded tenant must not raise or duplicate the row."""
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        run_seed()

    user_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO users")]
    assert "ON CONFLICT (tenant_id, email) DO NOTHING" in user_calls[0][0]


def test_run_seed_is_idempotent_across_two_calls_on_an_already_seeded_tenant():
    conn = FakeConnection(existing_tenant_id=EXISTING_TENANT_ID)
    with patch("src.external.seed_demo_tenant.connect", return_value=conn):
        first = run_seed()
        second = run_seed()

    assert first == second == EXISTING_TENANT_ID
    insert_tenant_calls = [e for e in conn.executed if e[0].startswith("INSERT INTO tenants")]
    assert insert_tenant_calls == []

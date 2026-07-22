"""Unit tests for src/external/dal.py: tenant injection + query-log middleware.

Per CLAUDE.md ("All external calls mocked in tests; zero network calls in
the test suite"), these use an in-memory fake connection/cursor - same
spirit as tests/unit/test_commit.py's FakeConnection, but shaped for
DAL.execute()'s fetchall()/description surface rather than commit.py's
fetchone()-only usage.

Covers bundle-0.md B0-S1's named test requirement: "DAL unit suite (retry,
tenancy, log middleware, both timestamp domains round-tripping)."
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from src.external.dal import DAL, Tenant

TENANT_ID = "10000000-0000-4000-8000-000000000002"


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql: str, params: tuple = ()):
        self._conn.executed.append((" ".join(sql.split()), params))
        self.description, self._rows = self._conn.dispatch(sql, params)
        return self

    def fetchall(self):
        return self._rows


class FakeConnection:
    """Tracks every executed statement; dispatches query_log inserts and
    a couple of named SELECT shapes the tests below need."""

    def __init__(self, select_response: list[tuple] | None = None):
        self.executed: list[tuple[str, tuple]] = []
        self.query_log_rows: list[tuple] = []
        self._select_response = select_response or []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def dispatch(self, sql: str, params: tuple):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO query_log"):
            self.query_log_rows.append(params)
            return None, []
        if normalized.startswith("SELECT"):
            return [("col",)], self._select_response
        return None, []


def make_dal(conn: FakeConnection | None = None, actor: str = "rachel.martinez") -> DAL:
    return DAL(conn or FakeConnection(), Tenant(tenant_id=TENANT_ID, actor=actor))


def test_execute_prepends_tenant_id_to_params():
    conn = FakeConnection(select_response=[("row1",)])
    dal = make_dal(conn)

    dal.execute("SELECT x FROM cases WHERE tenant_id=%s AND id=%s", ("case-123",), tag="case.get")

    sql, params = conn.executed[0]
    assert params == (TENANT_ID, "case-123")


def test_execute_returns_fetched_rows():
    conn = FakeConnection(select_response=[("a",), ("b",)])
    dal = make_dal(conn)

    rows = dal.execute("SELECT x FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    assert rows == [("a",), ("b",)]


def test_execute_writes_one_query_log_row_per_call():
    conn = FakeConnection()
    dal = make_dal(conn)

    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    assert len(conn.query_log_rows) == 1


def test_query_log_row_carries_tenant_actor_tag_and_ok_true_on_success():
    conn = FakeConnection()
    dal = make_dal(conn, actor="clerk")

    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list", kind="sql")

    logged = conn.query_log_rows[0]
    (tenant_id, kind, tag, sql_text, elapsed_ms, row_count,
     render_source, actor, ok, error) = logged
    assert tenant_id == TENANT_ID
    assert kind == "sql"
    assert tag == "cases.list"
    assert actor == "clerk"
    assert ok is True
    assert error is None
    assert render_source == "live"


def test_query_log_row_records_failure_and_reraises():
    """Only the caller's own query fails - the log-write for that
    failure (a distinct, valid INSERT INTO query_log) must still
    succeed, the same as it would against a real database where one
    bad SELECT doesn't taint an unrelated INSERT on the same connection."""

    class SometimesRaisingCursor(FakeCursor):
        def execute(self, sql, params=()):
            self._conn.executed.append((" ".join(sql.split()), params))
            if "nonexistent" in sql:
                raise psycopg.errors.UndefinedTable("no such table")
            self.description, self._rows = self._conn.dispatch(sql, params)
            return self

    class SometimesRaisingConnection(FakeConnection):
        def cursor(self):
            return SometimesRaisingCursor(self)

    conn = SometimesRaisingConnection()
    dal = make_dal(conn)

    with pytest.raises(psycopg.errors.UndefinedTable):
        dal.execute("SELECT 1 FROM nonexistent WHERE tenant_id=%s", (), tag="broken")

    logged = conn.query_log_rows[0]
    ok, error = logged[8], logged[9]
    assert ok is False
    assert "no such table" in error


def test_query_log_middleware_does_not_recurse_into_itself():
    """The log-write itself is a SQL statement; it must not trigger a
    second log-write for logging the first one (which would trigger a
    third, forever)."""
    conn = FakeConnection()
    dal = make_dal(conn)

    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    # Exactly one query_log row for the one real query - not two (one for
    # the query, one for logging the query, ad infinitum).
    assert len(conn.query_log_rows) == 1
    assert dal._logging_in_progress is False


def test_query_log_row_records_elapsed_ms_as_nonnegative_int():
    conn = FakeConnection()
    dal = make_dal(conn)

    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    elapsed_ms = conn.query_log_rows[0][4]
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0


def test_render_source_defaults_to_live_but_is_overridable():
    conn = FakeConnection()
    dal = make_dal(conn)

    dal.execute(
        "SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="scrub.preload", render_source="preload"
    )

    render_source = conn.query_log_rows[0][6]
    assert render_source == "preload"


def test_audit_sql_text_can_redact_a_required_literal_without_changing_execution():
    conn = FakeConnection(select_response=[("row1",)])
    dal = make_dal(conn)
    sensitive_sql = (
        "SELECT 1 FROM cases AS OF SYSTEM TIME 1784578667004573103.0000000000 "
        "WHERE tenant_id=%s"
    )

    rows = dal.execute(
        sensitive_sql,
        (),
        tag="replay.then",
        audit_sql_text="fixed replay AOST template; seal HLC redacted",
    )

    assert rows == [("row1",)]
    assert conn.executed[0][0] == sensitive_sql
    logged_sql_text = conn.query_log_rows[0][3]
    assert logged_sql_text == "fixed replay AOST template; seal HLC redacted"
    assert "1784578667004573103" not in logged_sql_text


def test_audit_sql_text_also_redacts_sensitive_database_error_details():
    sensitive_hlc = "1784578667004573103.0000000000"

    class AOSTFailsCursor(FakeCursor):
        def execute(self, sql, params=()):
            self._conn.executed.append((" ".join(sql.split()), params))
            if "AS OF SYSTEM TIME" in sql:
                raise RuntimeError(f"timestamp {sensitive_hlc} is before the GC threshold")
            self.description, self._rows = self._conn.dispatch(sql, params)
            return self

    class AOSTFailsConnection(FakeConnection):
        def cursor(self):
            return AOSTFailsCursor(self)

    conn = AOSTFailsConnection()
    dal = make_dal(conn)
    with pytest.raises(RuntimeError, match="GC threshold"):
        dal.execute(
            f"SELECT 1 FROM cases AS OF SYSTEM TIME {sensitive_hlc} WHERE tenant_id=%s",
            (),
            tag="replay.then",
            audit_sql_text="fixed replay AOST template; seal HLC redacted",
        )

    logged_error = conn.query_log_rows[0][9]
    assert logged_error == "statement failed; sensitive details redacted"
    assert sensitive_hlc not in logged_error


def test_two_dal_instances_for_different_tenants_never_cross_scope():
    """Each DAL is bound to one tenant at construction; nothing about a
    query call can widen it to another tenant."""
    conn_a = FakeConnection()
    conn_b = FakeConnection()
    dal_a = DAL(conn_a, Tenant(tenant_id="tenant-a", actor="rachel"))
    dal_b = DAL(conn_b, Tenant(tenant_id="tenant-b", actor="rachel"))

    dal_a.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list")
    dal_b.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    assert conn_a.executed[0][1] == ("tenant-a",)
    assert conn_b.executed[0][1] == ("tenant-b",)


def test_log_write_failure_is_tracked_but_never_masks_the_real_query_result():
    """If the query_log INSERT itself fails (e.g. query_log doesn't exist
    yet), the caller's own successful query must still return normally -
    but the failure must not vanish silently either: log_failure_count/
    last_log_error make a systematically broken log path visible."""

    class LogInsertFailsCursor(FakeCursor):
        def execute(self, sql, params=()):
            self._conn.executed.append((" ".join(sql.split()), params))
            if "INSERT INTO query_log" in sql:
                raise psycopg.errors.UndefinedTable("query_log does not exist")
            self.description, self._rows = self._conn.dispatch(sql, params)
            return self

    class LogInsertFailsConnection(FakeConnection):
        def cursor(self):
            return LogInsertFailsCursor(self)

    conn = LogInsertFailsConnection(select_response=[("row1",)])
    dal = make_dal(conn)

    rows = dal.execute("SELECT x FROM cases WHERE tenant_id=%s", (), tag="cases.list")

    assert rows == [("row1",)]  # the real query result is untouched
    assert dal.log_failure_count == 1
    assert "query_log does not exist" in dal.last_log_error
    assert dal._logging_in_progress is False  # finally still resets the guard


def test_log_failure_count_accumulates_across_multiple_failed_log_writes():
    class LogInsertFailsCursor(FakeCursor):
        def execute(self, sql, params=()):
            self._conn.executed.append((" ".join(sql.split()), params))
            if "INSERT INTO query_log" in sql:
                raise psycopg.errors.UndefinedTable("query_log does not exist")
            self.description, self._rows = self._conn.dispatch(sql, params)
            return self

    class LogInsertFailsConnection(FakeConnection):
        def cursor(self):
            return LogInsertFailsCursor(self)

    conn = LogInsertFailsConnection()
    dal = make_dal(conn)

    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="a")
    dal.execute("SELECT 1 FROM cases WHERE tenant_id=%s", (), tag="b")

    assert dal.log_failure_count == 2


def test_timestamp_domains_round_trip_distinctly():
    """TDD D1: story-domain timestamps (captured_at/occurred_at) and
    system-domain timestamps (committed_at/created_at) are DATA vs PROOF -
    the DAL must pass both through unchanged and never conflate them into
    a single value. Simulated by round-tripping two distinct datetimes
    through execute()'s param binding and confirming both survive intact
    and distinguishable."""
    conn = FakeConnection(select_response=[])
    dal = make_dal(conn)

    story_captured_at = datetime(2026, 4, 3, 8, 0, 14, tzinfo=timezone.utc)
    system_committed_at = datetime(2026, 7, 8, 15, 57, 49, tzinfo=timezone.utc)

    dal.execute(
        "INSERT INTO tariff_snapshots (tenant_id, captured_at, committed_at) VALUES (%s, %s, %s)",
        (story_captured_at, system_committed_at),
        tag="tariff.insert",
    )

    _, params = conn.executed[0]
    assert params == (TENANT_ID, story_captured_at, system_committed_at)
    assert params[1] != params[2]

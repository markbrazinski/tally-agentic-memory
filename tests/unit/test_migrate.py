"""Unit tests for src/external/migrate.py's pure/file-listing logic.

Mocked per CLAUDE.md ("zero network calls in the test suite") - only
pending_migrations() (pure filesystem listing) is tested directly here.
apply_all()/PRE_RUNNER_MIGRATIONS' interaction with a real cluster is
exercised by make db-verify against the live example-cluster cluster, not by
this unit suite (same split as recording/commit.py's own tests vs.
restore_live.py's real-cluster verification).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from src.external.migrate import (
    PRE_RUNNER_MIGRATIONS,
    _ensure_tracking_table,
    _split_sql_statements,
    apply_all,
    pending_migrations,
)


def test_pending_migrations_lists_sql_files_in_sorted_order(tmp_path):
    (tmp_path / "002_second.sql").write_text("SELECT 1;")
    (tmp_path / "001a_first.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("not a migration")

    result = pending_migrations(str(tmp_path))

    assert result == ["001a_first.sql", "002_second.sql"]


def test_pending_migrations_ignores_non_sql_files(tmp_path):
    (tmp_path / "001a_first.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("scratch")

    result = pending_migrations(str(tmp_path))

    assert result == ["001a_first.sql"]


def test_split_sql_statements_ignores_semicolons_in_quotes_and_comments():
    script = """
    -- explanation; still a comment
    CREATE TABLE IF NOT EXISTS demo (value STRING DEFAULT 'a;b');
    /* block; comment */
    ALTER TABLE demo ADD COLUMN IF NOT EXISTS note STRING;
    """

    statements = _split_sql_statements(script)

    assert len(statements) == 2
    assert "CREATE TABLE" in statements[0]
    assert "'a;b'" in statements[0]
    assert "ALTER TABLE" in statements[1]


def test_real_migrations_dir_resolves_to_the_repo_migrations_folder():
    from src.external.migrate import MIGRATIONS_DIR

    assert os.path.basename(MIGRATIONS_DIR) == "migrations"
    assert os.path.isdir(MIGRATIONS_DIR)


def test_pre_runner_migrations_matches_bundle_r_files_on_disk():
    """001a/001b were applied by hand before this runner existed - this
    constant is what lets apply_all() retroactively mark them as already
    applied rather than re-executing their DDL. If someone renames those
    files, this constant (and the retroactive marking) silently stops
    matching - this test exists so that renaming trips a real failure
    instead of a silent no-op."""
    from src.external.migrate import MIGRATIONS_DIR

    for filename in PRE_RUNNER_MIGRATIONS:
        assert os.path.isfile(os.path.join(MIGRATIONS_DIR, filename))


def test_all_real_migration_files_end_in_sql_and_start_with_digits():
    from src.external.migrate import MIGRATIONS_DIR

    for filename in pending_migrations(MIGRATIONS_DIR):
        assert filename[0].isdigit()
        assert filename.endswith(".sql")


def test_gate1_migration_persists_exact_version_fact_and_canonical_manifest_fields():
    from src.external.migrate import MIGRATIONS_DIR

    sql = (Path(MIGRATIONS_DIR) / "004_gate1_evidence_receipt.sql").read_text()

    for required in (
        "source_version_id",
        "verification_status",
        "effective_from",
        "claimed_rate",
        "human_approval_state",
        "evidence_manifest",
        "manifest_version",
        "findings_clause_fk",
    ):
        assert required in sql
    assert "ALTER COLUMN embedding DROP NOT NULL" in sql


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last_query = ""
        self._last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self._last_query = normalized
        self._last_params = params
        if normalized.startswith("INSERT INTO schema_migrations") and params:
            filename = params[0]
            if filename in self._conn.tracked and "ON CONFLICT" not in normalized:
                raise psycopg.errors.UniqueViolation(f"duplicate key: {filename}")
            self._conn.tracked.add(filename)
        return self

    def fetchone(self):
        if self._last_query.startswith("SELECT 1 FROM schema_migrations"):
            filename = self._last_params[0]
            return (1,) if filename in self._conn.tracked else None
        return None

    def fetchall(self):
        if "information_schema.columns" in self._last_query:
            return sorted(self._conn.live_columns)
        return [(f,) for f in self._conn.tracked]


class _FakeConnection:
    def __init__(self, already_tracked=(), live_columns=None):
        self.tracked = set(already_tracked)
        self.live_columns = (
            {
                ("tenants", "id"),
                ("carriers", "id"),
                ("recordings", "id"),
                ("recordings", "invocation"),
                ("tariff_snapshots", "id"),
                ("terminal_snapshots", "id"),
            }
            if live_columns is None
            else set(live_columns)
        )

    def cursor(self):
        return _FakeCursor(self)


def test_ensure_tracking_table_is_a_noop_when_pre_runner_migrations_already_tracked():
    conn = _FakeConnection(already_tracked=set(PRE_RUNNER_MIGRATIONS))

    _ensure_tracking_table(conn)  # must not raise

    assert conn.tracked == set(PRE_RUNNER_MIGRATIONS)


def test_ensure_tracking_table_does_not_skip_foundation_on_blank_database():
    conn = _FakeConnection(already_tracked=set(), live_columns=set())

    _ensure_tracking_table(conn)

    assert conn.tracked == set()


def test_ensure_tracking_table_survives_a_concurrent_insert_of_the_same_filename():
    """Simulates the race: another process's INSERT lands between our
    SELECT and our own INSERT for the same filename - the resulting
    UniqueViolation must be caught, not propagate and crash apply_all()."""

    class RacingCursor(_FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("INSERT INTO schema_migrations") and params:
                # Simulate a concurrent writer winning the race right here,
                # between our SELECT (already run) and this INSERT.
                self._conn.tracked.add(params[0])
                raise psycopg.errors.UniqueViolation(f"duplicate key: {params[0]}")
            return super().execute(sql, params)

    class RacingConnection(_FakeConnection):
        def cursor(self):
            return RacingCursor(self)

    conn = RacingConnection(already_tracked=set())

    _ensure_tracking_table(conn)  # must not raise despite every insert racing

    assert conn.tracked == set(PRE_RUNNER_MIGRATIONS)


def test_apply_all_executes_each_ddl_on_autocommit_before_tracking(tmp_path, monkeypatch):
    (tmp_path / "005_example.sql").write_text(
        "CREATE TABLE IF NOT EXISTS first_table (id INT PRIMARY KEY);\n"
        "CREATE TABLE IF NOT EXISTS second_table (id INT PRIMARY KEY);\n"
    )

    class Connection(_FakeConnection):
        def __init__(self):
            super().__init__(already_tracked=(), live_columns=set())
            self.executed: list[str] = []
            self.transaction_entries = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def cursor(self):
            connection = self

            class Cursor(_FakeCursor):
                def execute(self, sql, params=None):
                    normalized = " ".join(sql.split())
                    connection.executed.append(normalized)
                    return super().execute(sql, params)

            return Cursor(self)

        def transaction(self):
            connection = self

            class Transaction:
                def __enter__(self):
                    connection.transaction_entries += 1

                def __exit__(self, *exc_info):
                    return False

            return Transaction()

    conn = Connection()
    monkeypatch.setattr("src.external.migrate.connect", lambda _dsn=None: conn)

    assert apply_all(str(tmp_path)) == ["005_example.sql"]
    assert conn.tracked == {"005_example.sql"}
    assert conn.transaction_entries == 1  # bookkeeping only, never either DDL
    first_index = conn.executed.index(
        "CREATE TABLE IF NOT EXISTS first_table (id INT PRIMARY KEY);"
    )
    second_index = conn.executed.index(
        "CREATE TABLE IF NOT EXISTS second_table (id INT PRIMARY KEY);"
    )
    tracking_index = next(
        i for i, sql in enumerate(conn.executed)
        if sql.startswith("INSERT INTO schema_migrations (filename) VALUES (%s)")
    )
    assert first_index < second_index < tracking_index
    assert "ON CONFLICT (filename) DO NOTHING" in conn.executed[tracking_index]

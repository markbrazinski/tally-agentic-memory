"""Ordered .sql migration applier - no ORM, no Alembic (CLAUDE.md: raw SQL only).

Tracks applied migrations in `schema_migrations` (filename + applied_at).
Migrations run in filename-sorted order and are skipped once recorded, so
re-running this against a cluster that already has some of them applied
(e.g. 001a/001b, applied by hand before this runner existed) is safe -
this module retroactively records those two as already-applied on first
run rather than trying to re-execute their CREATE TABLE IF NOT EXISTS
statements a second time.

CockroachDB schema changes are issued one statement at a time through the
autocommit connection.  Migration SQL must therefore be idempotent: after an
interruption, already-completed statements are harmlessly replayed and the
tracking row is written only after every statement succeeds.
"""

from __future__ import annotations

import os

import psycopg

from src.external.db import connect, run_with_retry

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "migrations")

# Migrations that can predate this runner are recorded as already applied only
# when their schema markers exist. A blank recovery database must execute them.
PRE_RUNNER_MIGRATIONS = ("001a_recording_tables.sql", "001b_recordings_invocation.sql")

_PRE_RUNNER_SCHEMA_MARKERS = {
    "001a_recording_tables.sql": {
        ("tenants", "id"),
        ("carriers", "id"),
        ("recordings", "id"),
        ("tariff_snapshots", "id"),
        ("terminal_snapshots", "id"),
    },
    "001b_recordings_invocation.sql": {("recordings", "invocation")},
}


def _ensure_tracking_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename   STRING PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        cur.execute("SELECT filename FROM schema_migrations;")
        already_tracked = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public';"
        )
        live_columns = set(cur.fetchall())
        for filename in PRE_RUNNER_MIGRATIONS:
            schema_exists = _PRE_RUNNER_SCHEMA_MARKERS[filename].issubset(live_columns)
            if filename not in already_tracked and schema_exists:
                try:
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s);", (filename,)
                    )
                except psycopg.errors.UniqueViolation:
                    # A concurrent apply_all() invocation already tracked
                    # this filename between our SELECT and this INSERT -
                    # the outcome we wanted (it's tracked) already holds.
                    pass


def applied_migrations(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations;")
        return {row[0] for row in cur.fetchall()}


def pending_migrations(migrations_dir: str = MIGRATIONS_DIR) -> list[str]:
    return sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))


def _split_sql_statements(script: str) -> list[str]:
    """Split a migration without treating quoted/comment semicolons as DDL boundaries."""
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    while index < len(script):
        char = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""
        current.append(char)
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and following == "/":
                current.append(following)
                index += 1
                in_block_comment = False
        elif in_single:
            if char == "'" and following == "'":
                current.append(following)
                index += 1
            elif char == "'":
                in_single = False
        elif in_double:
            if char == '"' and following == '"':
                current.append(following)
                index += 1
            elif char == '"':
                in_double = False
        elif char == "-" and following == "-":
            current.append(following)
            index += 1
            in_line_comment = True
        elif char == "/" and following == "*":
            current.append(following)
            index += 1
            in_block_comment = True
        elif char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        index += 1
    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


def apply_all(migrations_dir: str = MIGRATIONS_DIR, *, dsn: str | None = None) -> list[str]:
    """Apply every not-yet-applied .sql file in filename order. Returns filenames applied."""
    applied_now = []
    with connect(dsn) as conn:
        _ensure_tracking_table(conn)
        already = applied_migrations(conn)
        for filename in pending_migrations(migrations_dir):
            if filename in already:
                continue
            path = os.path.join(migrations_dir, filename)
            with open(path) as f:
                sql = f.read()

            # CockroachDB schema changes must each run as an implicit transaction.
            # `connect()` is autocommit, so deliberately do not use
            # run_with_retry() here: that helper creates an explicit transaction.
            # Every statement is idempotent, making a partially interrupted file
            # safe to resume. The tracking row lands only after all DDL succeeds.
            for statement in _split_sql_statements(sql):
                with conn.cursor() as cur:
                    cur.execute(statement)

            def _track_completed(conn, filename=filename):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s) "
                        "ON CONFLICT (filename) DO NOTHING;",
                        (filename,),
                    )
                    cur.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename=%s;",
                        (filename,),
                    )
                    if cur.fetchone() is None:
                        raise RuntimeError(f"migration tracking readback failed: {filename}")

            run_with_retry(conn, _track_completed)
            applied_now.append(filename)
    return applied_now


if __name__ == "__main__":
    for filename in apply_all():
        print(f"applied: {filename}")

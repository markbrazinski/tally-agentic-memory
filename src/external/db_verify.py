"""`make db-verify`: apply pending migrations, then read back live schema state.

Per the standing CLAUDE.md lock ("a deploy isn't done when the API call
succeeds — it's done when the script reads back live state and asserts it
matches intent"), this doesn't just call apply_all() and trust it — it
re-queries information_schema afterward and asserts every table this
migration was supposed to create actually exists live, with a nonzero
column count. Exits nonzero (loud failure) on any mismatch.

EXPECTED_TABLES is DERIVED from the migration files themselves (parsed
CREATE TABLE names), not hand-maintained as a separate list - a
hand-maintained list can silently drift from what the .sql files actually
create (a renamed/added table gets forgotten in the list, and verify()
would report OK while checking a stale set). schema_migrations itself is
created by migrate.py in Python, not by any .sql file, so it's added
explicitly.
"""

from __future__ import annotations

import re
import sys

from src.external.db import connect
from src.external.migrate import MIGRATIONS_DIR, apply_all, pending_migrations

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE
)


def expected_tables(migrations_dir: str = MIGRATIONS_DIR) -> tuple[str, ...]:
    """Every table name any migration file creates, plus schema_migrations itself."""
    names: set[str] = {"schema_migrations"}
    for filename in pending_migrations(migrations_dir):
        with open(f"{migrations_dir}/{filename}") as f:
            sql = f.read()
        names.update(_CREATE_TABLE_RE.findall(sql))
    return tuple(sorted(names))


EXPECTED_TABLES = expected_tables()


def verify() -> None:
    applied = apply_all()
    for filename in applied:
        print(f"applied: {filename}")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public';"
            )
            live_tables = {row[0] for row in cur.fetchall()}

        missing = [t for t in EXPECTED_TABLES if t not in live_tables]
        if missing:
            print(f"FAIL: expected tables missing from live schema: {missing}", file=sys.stderr)
            sys.exit(1)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name = ANY(%s) "
                "GROUP BY table_name;",
                (list(EXPECTED_TABLES),),
            )
            column_counts = dict(cur.fetchall())

        empty = [t for t in EXPECTED_TABLES if column_counts.get(t, 0) == 0]
        if empty:
            print(f"FAIL: tables with zero live columns: {empty}", file=sys.stderr)
            sys.exit(1)

    print(f"OK: {len(EXPECTED_TABLES)} expected tables verified live with columns.")


if __name__ == "__main__":
    verify()

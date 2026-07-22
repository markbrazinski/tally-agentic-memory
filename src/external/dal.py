"""Data-access layer: tenant injection + query-log middleware over db.py.

Per CLAUDE.md: "src/external — adapters: CockroachDB DAL (psycopg 3, raw
SQL, retry-on-40001, tenant injection, query-log middleware)." db.py
already ships connect()/run_with_retry(); this module adds the two pieces
bundle-0.md's B0-S1 scope calls out as not yet built: a tenant context
threaded through every query, and a query_log row written for every
statement (UX Law 2 - the query log is a first-class product feature,
not incidental logging).

Design: every query goes through DAL.execute(), which (1) runs the SQL,
(2) writes one query_log row describing what just ran. The write in step
2 is itself a SQL statement - naively recursive. The recursion guard is a
thread-local/instance flag (_logging_in_progress) checked before writing
the log row: a log-write in progress never triggers a second log-write
for itself.

Known gaps, left for the session/bundle that needs them (not solved here
prematurely - see each site's own docstring for detail):
  - A DAL instance is NOT safe to share across concurrent tasks/threads;
    the recursion guard is a plain instance flag, not a lock.
  - The query itself and its query_log row are two separate autocommitted
    statements, not one transaction - a crash between them loses the log
    row silently. DAL.run_with_retry() bypasses logging entirely. Real
    transactional audit logging (needed by the Seal, TDD ss2.21) is
    B0-S3's problem to solve, not this session's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from src.external.db import connect as db_connect
from src.external.db import run_with_retry


@dataclass(frozen=True)
class Tenant:
    """Authenticated tenant context. Every DAL query is scoped to this tenant_id.

    actor identifies who/what is issuing the query for the audit trail
    (query_log.actor) - e.g. 'rachel.martinez', 'clerk', 'snapshot-job'.
    """

    tenant_id: str
    actor: str


class DAL:
    """A tenant-scoped connection wrapper. One instance per request/task.

    No caller ever passes tenant_id as a query param by hand - it's bound
    once at construction and every execute() call requires the caller to
    include a tenant_id placeholder in their WHERE/VALUES clause, which
    this class fills in from self.tenant.tenant_id. This doesn't make
    cross-tenant queries impossible at the SQL level (nothing does, short
    of row-level security), but it does make "forgot to scope this query"
    structurally awkward rather than a silent copy-paste risk: every
    execute() call's params tuple is built by starting from
    (self.tenant.tenant_id,), so a handler has to go out of its way to
    query without it.

    CONVENTION, NOT ENFORCEMENT: tenant_id is bound POSITIONALLY as the
    first parameter. The caller's SQL MUST place tenant_id as its first
    "%s" placeholder (matching every table's tenant-first PK/index
    convention). Nothing here parses the SQL to confirm this - a query
    that puts a different placeholder first (e.g. an UPDATE with SET
    columns before WHERE) will silently bind tenant_id to the wrong slot,
    with no error raised anywhere. Write queries tenant_id-first, always.

    NOT THREAD/TASK-SAFE: one DAL instance must be used by exactly one
    request/task at a time (per the class name above) - the query-log
    recursion guard (_logging_in_progress) is a plain instance flag, not
    a lock, so sharing one instance across concurrent callers can race
    and silently drop a log write. Construct a fresh DAL per request.
    """

    def __init__(self, conn: Connection, tenant: Tenant):
        self.conn = conn
        self.tenant = tenant
        self._logging_in_progress = False
        self.log_failure_count = 0
        self.last_log_error: str | None = None

    @classmethod
    def connect(cls, tenant: Tenant, dsn: str | None = None) -> DAL:
        return cls(db_connect(dsn), tenant)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> DAL:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        tag: str,
        kind: str = "sql",
        render_source: str = "live",
        audit_sql_text: str | None = None,
    ) -> list[tuple[Any, ...]]:
        """Run sql with (tenant_id, *params), fetch all rows, log the statement.

        Callers write queries with tenant_id as the FIRST bound parameter
        (matching every table's tenant-first PK/index convention) - this
        method prepends self.tenant.tenant_id, so callers never type the
        tenant_id literal themselves. ``audit_sql_text`` is a narrow redaction
        hook for statements such as CockroachDB AOST, whose timestamp cannot be
        a placeholder; it changes only the audit copy, never executed SQL.
        """
        full_params = (self.tenant.tenant_id, *params)
        started = time.monotonic()
        ok = True
        error: str | None = None
        rows: list[tuple[Any, ...]] = []
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, full_params)
                if cur.description is not None:
                    rows = cur.fetchall()
            return rows
        except Exception as exc:  # noqa: BLE001 - logged then re-raised, never swallowed
            ok = False
            error = str(exc)
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log_query(
                kind=kind,
                tag=tag,
                sql_text=audit_sql_text if audit_sql_text is not None else sql,
                elapsed_ms=elapsed_ms,
                row_count=len(rows),
                render_source=render_source,
                ok=ok,
                error=(
                    "statement failed; sensitive details redacted"
                    if error is not None and audit_sql_text is not None
                    else error
                ),
            )

    def run_with_retry(self, fn):
        """Pass-through to db.run_with_retry, bound to this DAL's connection.

        KNOWN GAP: unlike execute(), this path writes NO query_log rows for
        whatever fn(conn) does - it's a real transaction (unlike execute(),
        whose query and log-write are two separate autocommitted
        statements), but that atomicity currently comes at the cost of zero
        audit trail. Any future caller needing both atomicity AND logging
        (the Seal transaction, TDD ss2.21, is exactly this) must add that
        logging itself for now - deliberately left unsolved here rather
        than bolted on ahead of knowing that transaction's real shape.
        """
        return run_with_retry(self.conn, fn)

    def _log_query(
        self,
        *,
        kind: str,
        tag: str,
        sql_text: str,
        elapsed_ms: int,
        row_count: int,
        render_source: str,
        ok: bool,
        error: str | None,
    ) -> None:
        """Write one query_log row. Recursion-guarded: never logs itself."""
        if self._logging_in_progress:
            return
        self._logging_in_progress = True
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO query_log
                        (tenant_id, kind, tag, sql_text, elapsed_ms, row_count,
                         render_source, actor, ok, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        self.tenant.tenant_id,
                        kind,
                        tag,
                        sql_text,
                        elapsed_ms,
                        row_count,
                        render_source,
                        self.tenant.actor,
                        ok,
                        error,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - a logging failure must never mask the real query result
            # Never re-raised (the caller's real query result must still
            # return), but never silent either: log_failure_count/
            # last_log_error make a systematically broken log path
            # (e.g. query_log missing before migrations run) visible to
            # anything inspecting the DAL instance, instead of every
            # query silently producing zero audit rows forever.
            self.log_failure_count += 1
            self.last_log_error = str(exc)
        finally:
            self._logging_in_progress = False

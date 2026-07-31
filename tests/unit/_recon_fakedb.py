"""Reusable in-memory fake DB for Gate 2 reconstruction repository tests.

Zero network. Models the reconstruction spine tables as dict rows and answers
the specific statements the repository issues: lease assertion, fingerprint
existence, version allocation, and the insert set. It is faithful enough to
prove atomicity (all-or-nothing per transaction), fencing (a lost lease raises
before any write), and idempotency (a duplicated delivery replays the existing
version instead of writing a second).

Deliberately narrow: it recognizes the repository's statements by normalized SQL
prefix, exactly like the existing intake repository test harness — not a general
SQL engine (that would be a speculative rewrite; ponytail).
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.external.dal import DAL, Tenant

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None
        self._rows: list[tuple] = []
        # Real psycopg cursors expose rowcount; the repository reads it after the
        # events INSERT ... SELECT to catch a silent zero-row write (a missing
        # source artifact). Default 1 = "the statement affected its row", which is
        # what every statement in these fakes represents. Tests that need the
        # missing-artifact path set `missing_source_artifact` on the connection.
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):  # noqa: C901 - prefix dispatch, mirrors repo SQL
        n = " ".join(sql.split())
        p = params or ()
        self.conn.log.append((n, p))
        self.one = None
        self._rows = []
        self.rowcount = 1

        if n == "SELECT now();":
            self.one = (NOW,)
        elif n.startswith("SELECT state, current_attempt, lease_owner FROM workflow_tasks"):
            task = self.conn.tasks.get(p[1])
            self.one = (
                (task["state"], task["current_attempt"], task["lease_owner"])
                if task
                else None
            )
        elif n.startswith("SELECT id, version, state, event_count, days_total, days_complete FROM reconstructions"):  # noqa: E501
            key = (p[1], p[2])  # (invoice_id, fingerprint)
            existing = self.conn.recon_by_fp.get(key)
            self.one = existing
        elif n.startswith("SELECT COALESCE(max(version), 0) + 1 FROM reconstructions"):
            invoice_id = p[1]
            versions = [
                r["version"] for r in self.conn.reconstructions.values()
                if r["invoice_id"] == invoice_id
            ]
            self.one = (max(versions, default=0) + 1,)
        elif n.startswith("SELECT status_sequence FROM invoices"):
            inv = self.conn.invoices.get(p[1])
            self.one = (inv["status_sequence"],) if inv else None
        elif n.startswith("UPDATE invoices"):
            inv = self.conn.invoices.get(p[-1])
            if inv:
                inv["status_sequence"] += p[3]
                inv["state"] = p[0]
        elif n.startswith("INSERT INTO reconstructions"):
            rid = p[1]
            self.conn.reconstructions[rid] = {
                "id": rid,
                "invoice_id": p[2],
                "version": p[3],
                "fingerprint": p[5],
                "state": p[9],
                "event_count": p[10],
                "days_total": p[11],
                "days_complete": p[12],
            }
            self.conn.recon_by_fp[(p[2], p[5])] = (
                rid, p[3], p[9], p[10], p[11], p[12]
            )
        elif n.startswith("INSERT INTO reconstruction_events"):
            # INSERT ... SELECT FROM reconstruction_source_artifacts: writes zero
            # rows when the artifact is missing (the real cause of the FK crash).
            if getattr(self.conn, "missing_source_artifact", False):
                self.rowcount = 0
            else:
                self.conn.count("reconstruction_events")
        elif n.startswith("INSERT INTO reconstruction_charged_days"):
            self.conn.count("reconstruction_charged_days")
        elif n.startswith("INSERT INTO reconstruction_day_event_bindings"):
            self.conn.count("reconstruction_day_event_bindings")
        elif n.startswith("INSERT INTO reconstruction_coverage"):
            self.conn.count("reconstruction_coverage")
        elif n.startswith("INSERT INTO invoice_events"):
            self.conn.count("invoice_events")
            self.conn.events.append({"type": p[4], "state": p[10], "seq": p[3]})
        elif n.startswith("INSERT INTO event_outbox"):
            self.conn.count("event_outbox")
        elif n.startswith("INSERT INTO workflow_tasks"):
            # A follow-on task enqueue (e.g. FIND_APPLICABLE_RULE handoff).
            self.conn.count("workflow_tasks_enqueued")
        elif n.startswith("UPDATE workflow_tasks"):
            # completion or failure — fence on current_attempt + lease_owner.
            task = self.conn.tasks.get(p[-3])
            if task and task["current_attempt"] == p[-2] and task["lease_owner"] == p[-1]:
                task["state"] = "COMPLETED" if "COMPLETED" in n else p[0]
                task["lease_owner"] = None
                self.conn.count("workflow_tasks_finished")
        elif n.startswith("UPDATE workflow_task_attempts"):
            self.conn.count("attempts_finished")
        return self

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.reconstructions: dict[str, dict] = {}
        self.recon_by_fp: dict[tuple, tuple] = {}
        self.events: list[dict] = []
        self.counts: dict[str, int] = {}
        self.log: list[tuple] = []

    def count(self, table):
        self.counts[table] = self.counts.get(table, 0) + 1

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def make_dal(conn: FakeConn) -> DAL:
    return DAL(conn, Tenant(TENANT, "reconstruction-worker"))


def seed_running_task(conn: FakeConn, *, task_id="task-1", invoice_id="invoice-1",
                      attempt=1, worker="worker-1") -> None:
    conn.tasks[task_id] = {
        "state": "RUNNING",
        "current_attempt": attempt,
        "lease_owner": worker,
    }
    conn.invoices[invoice_id] = {"status_sequence": 5, "state": "READY_FOR_RECONSTRUCTION"}

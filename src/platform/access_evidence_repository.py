"""Gate 2b — gap-driven terminal-access evidence binding.

When a reconstruction withholds authority because a charged day lacks its
per-day terminal-access snapshot, a durable BIND_ACCESS_EVIDENCE task verifies
the retained-but-PENDING snapshot against its exact source and — only on pass —
binds it by projecting shipment_event_memory.source_version_state PENDING ->
VERIFIED. The snapshot already exists in retained pre-invoice memory; this never
inserts it and never rewrites its payload/timestamps/refs.

Guardrails (Delta authority-proof):
- Verify server-side against the exact retained source (Option A): the
  reconstruction read stays the sponsor MCP path; verification does not add a
  second MCP surface.
- Record the verification attempt + result DURABLY before the state transition.
- Fail closed on wrong container / wrong date / unavailable source / integrity
  failure — never bind, never fall back, never expose PENDING via the MCP view.
- On a successful bind, enqueue a fresh START_RECONSTRUCTION so the SAME fixed
  MCP query now returns the newly-VERIFIED snapshot and lifts coverage in a NEW
  immutable reconstruction revision. The earlier revision is untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

from src.core.intake import TaskType, task_input_fingerprint
from src.core.reconstruction import (
    AccessSnapshotFacts,
    verify_access_snapshot,
)
from src.external.dal import DAL
from src.platform.intake_tasks import (
    TaskLeaseLostError,
    _insert_event,
    _lock_invoice_and_advance,
)


@dataclass(frozen=True)
class AccessEvidenceLease:
    task_id: str
    invoice_id: str
    attempt: int
    worker_id: str
    knowledge_cutoff_at: object
    snapshot_public_ref: str
    expected_container_ref: str
    expected_date: date
    initiated_by: str | None
    actor_display: str


@dataclass(frozen=True)
class AccessBindingResult:
    outcome: str            # VERIFIED | REFUSED
    reason_code: str | None
    snapshot_public_ref: str
    reconstruction_requeued: bool


def complete_access_binding(
    dal: DAL, *, lease: AccessEvidenceLease
) -> AccessBindingResult:
    """Verify and (on pass) bind the retained pending access snapshot in one
    serializable transaction. Idempotent on (task, snapshot)."""
    tenant_id = dal.tenant.tenant_id

    def _complete(conn):
        with conn.cursor() as cur:
            _assert_lease(cur, tenant_id, lease)

            # Idempotency: a prior VERIFIED verdict for this (task, snapshot)
            # means the bind already happened — return it without re-verifying
            # or re-enqueuing.
            cur.execute(
                """
                SELECT outcome, reason_code FROM access_evidence_verifications
                WHERE tenant_id=%s AND task_id=%s AND snapshot_public_ref=%s;
                """,
                (tenant_id, lease.task_id, lease.snapshot_public_ref),
            )
            prior = cur.fetchone()
            if prior is not None:
                _finish_task(cur, tenant_id, lease)
                return AccessBindingResult(
                    prior[0], prior[1], lease.snapshot_public_ref, False
                )

            facts = _load_snapshot_facts(cur, tenant_id, lease.snapshot_public_ref)
            verdict = verify_access_snapshot(
                facts,
                expected_container_ref=lease.expected_container_ref,
                expected_date=lease.expected_date,
            )
            outcome = "VERIFIED" if verdict.passed else "REFUSED"

            # Durable verdict BEFORE any state transition (guardrail).
            cur.execute(
                """
                INSERT INTO access_evidence_verifications
                    (tenant_id, id, invoice_id, task_id, snapshot_public_ref,
                     container_ref, snapshot_date, outcome, reason_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
                """,
                (tenant_id, str(uuid4()), lease.invoice_id, lease.task_id,
                 lease.snapshot_public_ref, facts.container_ref,
                 facts.snapshot_date, outcome, verdict.reason_code),
            )

            if not verdict.passed:
                # Fail closed: no binding, no re-reconstruction, no fallback.
                _finish_task(cur, tenant_id, lease)
                return AccessBindingResult(
                    outcome, verdict.reason_code, lease.snapshot_public_ref, False
                )

            # Bind: the ONLY mutation to the retained snapshot is its state.
            cur.execute(
                """
                UPDATE shipment_event_memory
                SET source_version_state='VERIFIED'
                WHERE tenant_id=%s AND public_ref=%s
                  AND source_version_state='PENDING';
                """,
                (tenant_id, lease.snapshot_public_ref),
            )

            cur.execute("SELECT now();")
            now = cur.fetchone()[0]
            sequence = _lock_invoice_and_advance(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status="RECONSTRUCTING", status="RECONSTRUCTING",
                increment=1, occurred_at=now,
            )
            _insert_event(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                sequence=sequence, event_type="reconstruction.source_bound",
                occurred_at=now, task="BIND_ACCESS_EVIDENCE",
                tool_display_name="CockroachDB Managed MCP",
                state="COMPLETED", aggregate_status="RECONSTRUCTING",
                summary=(
                    f"Terminal-access snapshot verified and bound for "
                    f"{lease.expected_date.isoformat()}"
                ),
                initiated_by=lease.initiated_by, actor_display=lease.actor_display,
                input_refs=[{"type": "access_snapshot",
                             "id": lease.snapshot_public_ref}],
                produced_refs=[{"type": "access_binding",
                                "id": lease.snapshot_public_ref}],
                output_count=1,
            )
            _emit_reconstruction_task(cur, tenant_id=tenant_id, lease=lease, now=now)
            _finish_task(cur, tenant_id, lease)
            return AccessBindingResult(
                outcome, None, lease.snapshot_public_ref, True
            )

    return dal.run_with_retry(_complete)


def _load_snapshot_facts(cur, tenant_id, public_ref) -> AccessSnapshotFacts:
    """Read the retained snapshot's immutable identity + integrity facts,
    server-side. source verified == the exact source row is VERIFIED; the
    snapshot's own state being PENDING is what we are about to resolve."""
    cur.execute(
        """
        SELECT m.public_ref, m.container_ref, m.effective_from,
               m.normalized_facts, m.event_type,
               COALESCE(a.verification_state, 'UNAVAILABLE')
        FROM shipment_event_memory m
        LEFT JOIN reconstruction_source_artifacts a
          ON a.tenant_id = m.tenant_id AND a.public_ref = m.source_public_ref
        WHERE m.tenant_id=%s AND m.public_ref=%s;
        """,
        (tenant_id, public_ref),
    )
    row = cur.fetchone()
    if row is None:
        raise AccessSnapshotMissingError(public_ref)
    facts = row[3] or {}
    return AccessSnapshotFacts(
        public_ref=row[0],
        container_ref=row[1],
        snapshot_date=row[2],
        exact_source_verified=(row[5] == "VERIFIED"),
        access_status=str(facts.get("access_status", "")),
        gate_status=str(facts.get("gate_status", "")),
        blocking_hold=str(facts.get("blocking_hold", "")),
    )


def _emit_reconstruction_task(cur, *, tenant_id, lease, now) -> None:
    """Enqueue a fresh START_RECONSTRUCTION so the worker re-runs the fixed MCP
    query — now returning the newly-VERIFIED snapshot — into a NEW revision.
    A distinct fingerprint (the binding event) keeps it separate from revision 1."""
    refs = [{"type": "access_binding", "id": lease.snapshot_public_ref}]
    fingerprint = task_input_fingerprint(
        task_type=TaskType.START_RECONSTRUCTION, input_refs=refs
    )
    cur.execute(
        """
        INSERT INTO workflow_tasks
            (tenant_id, id, invoice_id, task_type, task_version, state,
             initiated_by, actor_display, knowledge_cutoff_at, input_fingerprint,
             input_object_refs, public_summary)
        VALUES (%s, %s, %s, 'START_RECONSTRUCTION', 1, 'PENDING', %s, %s, %s, %s, %s,
                'Re-sourcing charged days after evidence binding')
        ON CONFLICT (tenant_id, invoice_id, task_type, task_version, input_fingerprint)
        DO NOTHING;
        """,
        (tenant_id, str(uuid4()), lease.invoice_id, lease.initiated_by,
         lease.actor_display, lease.knowledge_cutoff_at, fingerprint,
         json.dumps(refs)),
    )


def _assert_lease(cur, tenant_id, lease: AccessEvidenceLease) -> None:
    cur.execute(
        """
        SELECT state, current_attempt, lease_owner FROM workflow_tasks
        WHERE tenant_id=%s AND id=%s FOR UPDATE;
        """,
        (tenant_id, lease.task_id),
    )
    row = cur.fetchone()
    if row is None or row[0] != "RUNNING" or row[2] != lease.worker_id:
        raise TaskLeaseLostError(lease.task_id)


def _finish_task(cur, tenant_id, lease: AccessEvidenceLease) -> None:
    cur.execute(
        """
        UPDATE workflow_tasks SET state='COMPLETED', completed_at=now(),
            lease_owner=NULL, lease_expires_at=NULL, updated_at=now()
        WHERE tenant_id=%s AND id=%s AND lease_owner=%s;
        """,
        (tenant_id, lease.task_id, lease.worker_id),
    )


def claim_next_access_evidence_task(
    dal: DAL, *, worker_id: str, lease_seconds: int = 90
) -> AccessEvidenceLease | None:
    """Lease one runnable BIND_ACCESS_EVIDENCE task. Same SELECT ... FOR UPDATE
    SKIP LOCKED fencing as the other workers. The snapshot ref + expected
    container/date travel in the task's input_object_refs (set at enqueue)."""
    tenant_id = dal.tenant.tenant_id

    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.invoice_id, w.current_attempt, w.knowledge_cutoff_at,
                       w.initiated_by, w.actor_display, w.input_object_refs
                FROM workflow_tasks w
                WHERE w.tenant_id=%s
                  AND w.task_type='BIND_ACCESS_EVIDENCE'
                  AND (
                    w.state='PENDING'
                    OR (w.state='RETRY_WAIT'
                        AND (w.not_before IS NULL OR w.not_before <= now()))
                    OR (w.state IN ('LEASED','RUNNING') AND w.lease_expires_at < now())
                  )
                ORDER BY w.created_at, w.id
                LIMIT 1
                FOR UPDATE OF w SKIP LOCKED;
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task_id = str(row[0])
            attempt = int(row[2]) + 1
            refs = row[6] if isinstance(row[6], list) else json.loads(row[6])
            binding = next(
                (r for r in refs if r.get("type") == "access_binding_request"), {}
            )
            cur.execute("SELECT now();")
            started_at = cur.fetchone()[0]
            lease_expires_at = started_at + timedelta(seconds=lease_seconds)
            cur.execute(
                """
                UPDATE workflow_tasks
                SET state='RUNNING', current_attempt=%s, lease_owner=%s,
                    lease_expires_at=%s, started_at=COALESCE(started_at, %s),
                    updated_at=%s
                WHERE tenant_id=%s AND id=%s;
                """,
                (attempt, worker_id, lease_expires_at, started_at, started_at,
                 tenant_id, task_id),
            )
            cur.execute(
                """
                INSERT INTO workflow_task_attempts
                    (tenant_id, task_id, attempt, state, lease_owner,
                     lease_expires_at, started_at)
                VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s);
                """,
                (tenant_id, task_id, attempt, worker_id, lease_expires_at, started_at),
            )
            return AccessEvidenceLease(
                task_id=task_id,
                invoice_id=str(row[1]),
                attempt=attempt,
                worker_id=worker_id,
                knowledge_cutoff_at=row[3],
                snapshot_public_ref=binding.get("snapshot_public_ref", ""),
                expected_container_ref=binding.get("container_ref", ""),
                expected_date=date.fromisoformat(binding["snapshot_date"])
                if binding.get("snapshot_date") else date.min,
                initiated_by=row[4],
                actor_display=str(row[5]),
            )

    return dal.run_with_retry(_claim)


def release_access_evidence(dal: DAL, *, invoice_id: str) -> bool:
    """Controlled release: flip this invoice's HELD BIND_ACCESS_EVIDENCE task to
    PENDING so the worker can claim it. Returns True if a held task was released,
    False if none was held (idempotent — a second call is a no-op)."""
    tenant_id = dal.tenant.tenant_id

    def _release(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET state='PENDING', updated_at=now()
                WHERE tenant_id=%s AND invoice_id=%s
                  AND task_type='BIND_ACCESS_EVIDENCE' AND state='HELD';
                """,
                (tenant_id, invoice_id),
            )
            return cur.rowcount > 0

    return dal.run_with_retry(_release)


class AccessSnapshotMissingError(RuntimeError):
    """The referenced snapshot row is absent — a real gap, never invented."""

"""Durable Gate 4 judgment: frozen recommendation + per-day judgments.

Leases a JUDGE_DAYS task, reads the persisted charged days + applicable rule,
computes the deterministic recommendation in pure Python (no model arithmetic),
and freezes one immutable recommendation version with one explainable judgment
row per charged day. Idempotent on the judgment input fingerprint; a duplicated
delivery replays the frozen version. Reuses the task/event spine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from src.core.judgment import (
    DayInput,
    Recommendation,
    recommendation_fingerprint,
    resolve_recommendation,
)
from src.external.dal import DAL
from src.platform.applicable_rule_repository import _insert_rule_event
from src.platform.intake_tasks import MAX_AUTOMATIC_ATTEMPTS, TaskLeaseLostError
from src.platform.reconstruction_repository import _lock_invoice_and_advance


@dataclass(frozen=True)
class JudgmentTaskLease:
    task_id: str
    invoice_id: str
    reconstruction_id: str
    attempt: int
    worker_id: str
    input_fingerprint: str
    initiated_by: str | None
    actor_display: str


@dataclass(frozen=True)
class JudgmentCompletion:
    recommendation_id: str
    version: int
    recommendation_type: str
    disputed_amount_minor: int
    supported_amount_minor: int
    days_total: int


def claim_next_judgment_task(
    dal: DAL, *, worker_id: str, lease_seconds: int = 90
) -> JudgmentTaskLease | None:
    tenant_id = dal.tenant.tenant_id

    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.invoice_id, w.current_attempt, w.initiated_by,
                       w.actor_display, w.input_fingerprint, w.input_object_refs
                FROM workflow_tasks w
                WHERE w.tenant_id=%s AND w.task_type='JUDGE_DAYS'
                  AND (w.state='PENDING'
                    OR (w.state='RETRY_WAIT'
                        AND (w.not_before IS NULL OR w.not_before <= now()))
                    OR (w.state IN ('LEASED','RUNNING') AND w.lease_expires_at < now()))
                ORDER BY w.created_at, w.id LIMIT 1 FOR UPDATE OF w SKIP LOCKED;
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task_id, invoice_id = str(row[0]), str(row[1])
            attempt = int(row[2]) + 1
            refs = row[6] if isinstance(row[6], list) else json.loads(row[6])
            recon_ref = next((r for r in refs if r.get("type") == "reconstruction"), {})
            cur.execute("SELECT now();")
            started_at = cur.fetchone()[0]
            lease_expires_at = started_at + timedelta(seconds=lease_seconds)
            cur.execute(
                """
                UPDATE workflow_tasks SET state='RUNNING', current_attempt=%s,
                    lease_owner=%s, lease_expires_at=%s,
                    started_at=COALESCE(started_at,%s), updated_at=%s
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
                VALUES (%s,%s,%s,'RUNNING',%s,%s,%s);
                """,
                (tenant_id, task_id, attempt, worker_id, lease_expires_at, started_at),
            )
            return JudgmentTaskLease(
                task_id=task_id, invoice_id=invoice_id,
                reconstruction_id=str(recon_ref.get("id")), attempt=attempt,
                worker_id=worker_id, input_fingerprint=row[5],
                initiated_by=str(row[3]) if row[3] else None, actor_display=row[4],
            )

    return dal.run_with_retry(_claim)


def load_day_inputs(dal: DAL, *, reconstruction_id: str) -> list[DayInput]:
    """Read the persisted charged days into deterministic judgment inputs."""
    tenant_id = dal.tenant.tenant_id
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT charge_date, invoice_rate_minor, applicable_rate_minor, currency,
                   coverage_state, chargeability
            FROM reconstruction_charged_days
            WHERE tenant_id=%s AND reconstruction_id=%s ORDER BY charge_date;
            """,
            (tenant_id, reconstruction_id),
        )
        return [
            DayInput(
                charge_date=r[0], invoice_rate_minor=int(r[1]),
                applicable_rate_minor=int(r[2]) if r[2] is not None else None,
                currency=r[3], coverage_state=r[4],
                chargeable=(r[5] == "CHARGEABLE"),
            )
            for r in cur.fetchall()
        ]


def complete_judgment(
    dal: DAL, *, lease: JudgmentTaskLease, days: list[DayInput]
) -> JudgmentCompletion:
    tenant_id = dal.tenant.tenant_id
    recommendation = resolve_recommendation(days)
    fingerprint = recommendation_fingerprint(days)

    def _complete(conn):
        with conn.cursor() as cur:
            _assert_lease(cur, tenant_id, lease)
            cur.execute(
                """
                SELECT id, version, recommendation_type, disputed_amount_minor,
                       supported_amount_minor, days_total
                FROM recommendations
                WHERE tenant_id=%s AND reconstruction_id=%s AND input_fingerprint=%s;
                """,
                (tenant_id, lease.reconstruction_id, fingerprint),
            )
            existing = cur.fetchone()
            if existing is not None:
                _finish_task(cur, tenant_id, lease)
                return JudgmentCompletion(
                    str(existing[0]), int(existing[1]), existing[2],
                    int(existing[3]), int(existing[4]), int(existing[5]),
                )

            cur.execute("SELECT now();")
            now = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COALESCE(max(version),0)+1 FROM recommendations
                WHERE tenant_id=%s AND reconstruction_id=%s;
                """,
                (tenant_id, lease.reconstruction_id),
            )
            version = int(cur.fetchone()[0])
            recommendation_id = str(uuid4())
            rule_id = _rule_id(cur, tenant_id, lease.reconstruction_id)
            _insert_recommendation(cur, tenant_id, recommendation_id, version, lease,
                                   rule_id, fingerprint, recommendation)
            _insert_judgments(cur, tenant_id, recommendation_id, lease, rule_id,
                              recommendation)
            _finish_task(cur, tenant_id, lease)

            sequence = _lock_invoice_and_advance(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status="READY_FOR_REVIEW", status="READY_FOR_REVIEW",
                increment=1, occurred_at=now,
            )
            _insert_rule_event(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                sequence=sequence, event_type="decision.recommendation_ready",
                occurred_at=now, state="COMPLETED", aggregate_status="READY_FOR_REVIEW",
                summary=recommendation.summary, initiated_by=lease.initiated_by,
                actor_display=lease.actor_display,
                input_refs=[{"type": "reconstruction", "id": lease.reconstruction_id,
                             "version": 1}],
                produced_refs=[{"type": "recommendation", "id": recommendation_id,
                                "version": version}],
                output_count=recommendation.days_total,
            )
            return JudgmentCompletion(
                recommendation_id, version, recommendation.recommendation_type.value,
                recommendation.disputed_amount_minor,
                recommendation.supported_amount_minor, recommendation.days_total,
            )

    return dal.run_with_retry(_complete)


def _insert_recommendation(cur, tenant_id, rec_id, version, lease, rule_id,
                           fingerprint, rec: Recommendation) -> None:
    cur.execute(
        """
        INSERT INTO recommendations
            (tenant_id, id, invoice_id, reconstruction_id, applicable_rule_id,
             version, input_fingerprint, recommendation_type, disputed_amount_minor,
             supported_amount_minor, claimed_amount_minor, currency, days_total,
             days_covered, evidence_coverage, state, digest, public_summary)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'FROZEN',%s,%s)
        ON CONFLICT (tenant_id, reconstruction_id, input_fingerprint) DO NOTHING;
        """,
        (tenant_id, rec_id, lease.invoice_id, lease.reconstruction_id, rule_id,
         version, fingerprint, rec.recommendation_type.value,
         rec.disputed_amount_minor, rec.supported_amount_minor,
         rec.claimed_amount_minor, rec.currency, rec.days_total, rec.days_covered,
         rec.evidence_coverage, rec.digest, rec.summary),
    )


def _insert_judgments(cur, tenant_id, rec_id, lease, rule_id, rec: Recommendation) -> None:
    for j in rec.judgments:
        cur.execute(
            """
            INSERT INTO charged_day_judgments
                (tenant_id, id, invoice_id, reconstruction_id, recommendation_id,
                 charged_day_id, charge_date, invoice_rate_minor,
                 applicable_rate_minor, discrepancy_minor, currency, outcome,
                 coverage_state, applicable_rule_id, explanation)
            SELECT %s,%s,%s,%s,%s, d.id, %s,%s,%s,%s,%s,%s, d.coverage_state, %s,%s
            FROM reconstruction_charged_days d
            WHERE d.tenant_id=%s AND d.reconstruction_id=%s AND d.charge_date=%s;
            """,
            (tenant_id, str(uuid4()), lease.invoice_id, lease.reconstruction_id,
             rec_id, j.charge_date, j.invoice_rate_minor, j.applicable_rate_minor,
             j.discrepancy_minor, j.currency, j.outcome.value, rule_id,
             j.explanation, tenant_id, lease.reconstruction_id, j.charge_date),
        )


def fail_judgment(dal: DAL, *, lease: JudgmentTaskLease, error_code: str) -> str:
    tenant_id = dal.tenant.tenant_id

    def _fail(conn):
        with conn.cursor() as cur:
            _assert_lease(cur, tenant_id, lease)
            cur.execute("SELECT now();")
            now = cur.fetchone()[0]
            will_retry = lease.attempt < MAX_AUTOMATIC_ATTEMPTS
            state = "RETRY_WAIT" if will_retry else "BLOCKED"
            not_before = now + timedelta(seconds=2 ** lease.attempt) if will_retry else None
            cur.execute(
                """
                UPDATE workflow_tasks SET state=%s, not_before=%s, lease_owner=NULL,
                    lease_expires_at=NULL, private_error_code=%s, updated_at=%s
                WHERE tenant_id=%s AND id=%s AND current_attempt=%s AND lease_owner=%s;
                """,
                (state, not_before, error_code, now, tenant_id, lease.task_id,
                 lease.attempt, lease.worker_id),
            )
            cur.execute(
                """
                UPDATE workflow_task_attempts SET state=%s, completed_at=%s,
                    private_error_code=%s
                WHERE tenant_id=%s AND task_id=%s AND attempt=%s AND lease_owner=%s;
                """,
                (state, now, error_code, tenant_id, lease.task_id, lease.attempt,
                 lease.worker_id),
            )
            return state

    return dal.run_with_retry(_fail)


def _rule_id(cur, tenant_id, reconstruction_id) -> str | None:
    cur.execute(
        """
        SELECT id FROM applicable_rules
        WHERE tenant_id=%s AND reconstruction_id=%s AND validation_state='VERIFIED';
        """,
        (tenant_id, reconstruction_id),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _finish_task(cur, tenant_id, lease: JudgmentTaskLease) -> None:
    cur.execute(
        """
        UPDATE workflow_tasks SET state='COMPLETED', completed_at=now(),
            lease_owner=NULL, lease_expires_at=NULL,
            public_summary='Recommendation frozen', updated_at=now()
        WHERE tenant_id=%s AND id=%s AND current_attempt=%s AND lease_owner=%s;
        """,
        (tenant_id, lease.task_id, lease.attempt, lease.worker_id),
    )
    cur.execute(
        """
        UPDATE workflow_task_attempts SET state='COMPLETED', completed_at=now()
        WHERE tenant_id=%s AND task_id=%s AND attempt=%s AND lease_owner=%s;
        """,
        (tenant_id, lease.task_id, lease.attempt, lease.worker_id),
    )


def _assert_lease(cur, tenant_id, lease: JudgmentTaskLease) -> None:
    cur.execute(
        "SELECT state, current_attempt, lease_owner FROM workflow_tasks "
        "WHERE tenant_id=%s AND id=%s FOR UPDATE;",
        (tenant_id, lease.task_id),
    )
    row = cur.fetchone()
    if (row is None or row[0] != "RUNNING" or int(row[1]) != lease.attempt
            or row[2] != lease.worker_id):
        raise TaskLeaseLostError("TASK_LEASE_LOST")

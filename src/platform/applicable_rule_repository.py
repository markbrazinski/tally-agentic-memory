"""Durable transactions for Gate 3 applicable-rule retrieval + validation.

Persists vector RETRIEVAL (run + ranked candidates) separately from
deterministic APPLICABILITY (the accepted applicable_rule). Reuses the intake
task spine for leasing/fencing and the reconstruction event/outbox helpers. No
embedded-clause or fixture fallback: an empty/unavailable vector result or a
no-candidate-passes decision fails closed to NEEDS_EVIDENCE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from src.core.applicable_rule import (
    ApplicabilityDecision,
    CandidateValidation,
    RuleCandidate,
    RuleValidationState,
)
from src.core.intake import TaskType, task_input_fingerprint
from src.external.dal import DAL
from src.platform.intake_tasks import MAX_AUTOMATIC_ATTEMPTS, TaskLeaseLostError
from src.platform.reconstruction_repository import _lock_invoice_and_advance


def _emit_judgment_task(cur, *, tenant_id, lease, reconstruction_id) -> None:
    """Create the durable JUDGE_DAYS task for Gate 4 (idempotent)."""
    refs = [{"type": "reconstruction", "id": reconstruction_id, "version": 1}]
    fingerprint = task_input_fingerprint(
        task_type=TaskType.JUDGE_DAYS, input_refs=refs
    )
    cur.execute(
        """
        INSERT INTO workflow_tasks
            (tenant_id, id, invoice_id, task_type, task_version, state,
             initiated_by, actor_display, knowledge_cutoff_at, input_fingerprint,
             input_object_refs, public_summary)
        VALUES (%s, %s, %s, 'JUDGE_DAYS', 1, 'PENDING', %s, %s, %s, %s, %s,
                'Waiting to compute the deterministic judgment')
        ON CONFLICT (tenant_id, invoice_id, task_type, task_version, input_fingerprint)
        DO NOTHING;
        """,
        (tenant_id, str(uuid4()), lease.invoice_id, lease.initiated_by,
         lease.actor_display, lease.knowledge_cutoff_at, fingerprint,
         json.dumps(refs)),
    )


@dataclass(frozen=True)
class RuleTaskLease:
    task_id: str
    invoice_id: str
    reconstruction_id: str
    attempt: int
    worker_id: str
    knowledge_cutoff_at: datetime
    input_fingerprint: str
    carrier_id: str
    scope_code: str
    charge_dates: tuple[str, ...]
    invoice_currency: str
    initiated_by: str | None
    actor_display: str


@dataclass(frozen=True)
class RuleCompletion:
    retrieval_run_id: str
    applicable_rule_id: str | None
    state: str
    candidate_count: int
    accepted_rate_minor: int | None


def claim_next_rule_task(
    dal: DAL, *, worker_id: str, lease_seconds: int = 90
) -> RuleTaskLease | None:
    """Lease one FIND_APPLICABLE_RULE task and gather its reconstruction inputs."""
    tenant_id = dal.tenant.tenant_id

    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.invoice_id, w.current_attempt, w.knowledge_cutoff_at,
                       w.initiated_by, w.actor_display, w.input_fingerprint,
                       w.input_object_refs
                FROM workflow_tasks w
                WHERE w.tenant_id=%s AND w.task_type='FIND_APPLICABLE_RULE'
                  AND (
                    w.state='PENDING'
                    OR (w.state='RETRY_WAIT'
                        AND (w.not_before IS NULL OR w.not_before <= now()))
                    OR (w.state IN ('LEASED','RUNNING') AND w.lease_expires_at < now())
                  )
                ORDER BY w.created_at, w.id
                LIMIT 1 FOR UPDATE OF w SKIP LOCKED;
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task_id, invoice_id = str(row[0]), str(row[1])
            attempt = int(row[2]) + 1
            refs = row[7] if isinstance(row[7], list) else json.loads(row[7])
            recon_ref = next((r for r in refs if r.get("type") == "reconstruction"), {})
            reconstruction_id = str(recon_ref.get("id"))
            inputs = _load_rule_inputs(cur, tenant_id, invoice_id, reconstruction_id)

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
            sequence = _lock_invoice_and_advance(
                cur, tenant_id=tenant_id, invoice_id=invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION",
                aggregate_status="RECONSTRUCTING", status="RECONSTRUCTING",
                increment=1, occurred_at=started_at,
            )
            _insert_rule_event(
                cur, tenant_id=tenant_id, invoice_id=invoice_id, sequence=sequence,
                event_type="evidence.rule_search_started", occurred_at=started_at,
                state="RUNNING", aggregate_status="RECONSTRUCTING",
                summary="Searching recorded tariffs via Distributed Vector Indexing",
                initiated_by=str(row[4]) if row[4] else None, actor_display=row[5],
                input_refs=refs,
            )
            return RuleTaskLease(
                task_id=task_id, invoice_id=invoice_id,
                reconstruction_id=reconstruction_id, attempt=attempt,
                worker_id=worker_id, knowledge_cutoff_at=row[3],
                input_fingerprint=row[6], carrier_id=inputs["carrier_id"],
                scope_code=inputs["scope_code"], charge_dates=inputs["charge_dates"],
                invoice_currency=inputs["currency"],
                initiated_by=str(row[4]) if row[4] else None, actor_display=row[5],
            )

    return dal.run_with_retry(_claim)


def _load_rule_inputs(cur, tenant_id, invoice_id, reconstruction_id) -> dict[str, Any]:
    cur.execute(
        """
        SELECT charge_date, currency FROM reconstruction_charged_days
        WHERE tenant_id=%s AND reconstruction_id=%s ORDER BY charge_date;
        """,
        (tenant_id, reconstruction_id),
    )
    rows = cur.fetchall()
    charge_dates = tuple(r[0].isoformat() for r in rows)
    currency = rows[0][1] if rows else "USD"
    cur.execute(
        "SELECT carrier_id FROM invoices WHERE tenant_id=%s AND id=%s;",
        (tenant_id, invoice_id),
    )
    carrier_row = cur.fetchone()
    carrier_id = str(carrier_row[0]) if carrier_row else ""
    return {
        "carrier_id": carrier_id,
        "scope_code": "DEMURRAGE:USOAK:DRY",
        "charge_dates": charge_dates,
        "currency": currency,
    }


def complete_rule(
    dal: DAL,
    *,
    lease: RuleTaskLease,
    query_text: str,
    query_fingerprint: str,
    embedding_model: str,
    embedding_input_sha256: str,
    vector_index_name: str,
    candidates: list[RuleCandidate],
    decision: ApplicabilityDecision,
) -> RuleCompletion:
    """Persist retrieval run + candidates, then the applicability decision.

    Idempotent on the retrieval query fingerprint. Retrieval and applicability are
    separate rows: candidates record their vector rank/distance; the applicable
    rule exists only if the decision is VERIFIED.
    """
    tenant_id = dal.tenant.tenant_id

    def _complete(conn):
        with conn.cursor() as cur:
            _assert_rule_lease(cur, tenant_id, lease)
            cur.execute(
                """
                SELECT id FROM rule_retrieval_runs
                WHERE tenant_id=%s AND reconstruction_id=%s AND query_fingerprint=%s;
                """,
                (tenant_id, lease.reconstruction_id, query_fingerprint),
            )
            existing = cur.fetchone()
            if existing is not None:
                _finish_task(cur, tenant_id, lease)
                return _load_completion(cur, tenant_id, str(existing[0]))

            cur.execute("SELECT now();")
            now = cur.fetchone()[0]
            run_id = str(uuid4())
            run_state = "COMPLETED" if candidates else "EMPTY"
            cur.execute(
                """
                INSERT INTO rule_retrieval_runs
                    (tenant_id, id, invoice_id, reconstruction_id, query_fingerprint,
                     query_text_private, vector_index_name, embedding_model,
                     embedding_input_sha256, state, candidate_count, completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                """,
                (tenant_id, run_id, lease.invoice_id, lease.reconstruction_id,
                 query_fingerprint, query_text, vector_index_name, embedding_model,
                 embedding_input_sha256, run_state, len(candidates), now),
            )
            validations_by_ref = {
                v.candidate.public_ref: v for v in decision.candidate_validations
            }
            for candidate in candidates:
                v = validations_by_ref.get(candidate.public_ref)
                cand_state = v.state.value if v else "RETRIEVED"
                rejection = v.rejection_code if v else None
                cur.execute(
                    """
                    INSERT INTO rule_candidates
                        (tenant_id, id, retrieval_run_id, tariff_clause_id,
                         clause_public_ref, rank, distance, candidate_state,
                         rejection_code)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
                    """,
                    (tenant_id, str(uuid4()), run_id, candidate.clause_id,
                     candidate.public_ref, candidate.rank, candidate.distance,
                     cand_state, rejection),
                )

            applicable_rule_id = None
            accepted_rate_minor = None
            if decision.state is RuleValidationState.VERIFIED and decision.accepted:
                applicable_rule_id, accepted_rate_minor = _insert_applicable_rule(
                    cur, tenant_id, lease, run_id, decision.accepted, now
                )
                aggregate, event_type, ev_state = (
                    "RECONSTRUCTING", "evidence.rule_verified", "COMPLETED"
                )
                summary = "Applicable tariff verified: exact rate, date, and scope"
            else:
                aggregate, ev_state = "NEEDS_EVIDENCE", "BLOCKED"
                event_type = "evidence.rule_conflict" if (
                    decision.state is RuleValidationState.CONFLICTED
                ) else "evidence.rule_not_applicable"
                summary = (
                    "Conflicting tariffs; evidence required"
                    if decision.state is RuleValidationState.CONFLICTED
                    else "No applicable tariff verified; evidence required"
                )

            _finish_task(cur, tenant_id, lease)
            # Hand off to Gate 4 only when a rule was verified.
            if applicable_rule_id is not None:
                _emit_judgment_task(
                    cur, tenant_id=tenant_id, lease=lease,
                    reconstruction_id=lease.reconstruction_id,
                )
            sequence = _lock_invoice_and_advance(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION", aggregate_status=aggregate,
                status=aggregate, increment=1, occurred_at=now,
            )
            produced = (
                [{"type": "applicable_rule", "id": applicable_rule_id, "version": 1}]
                if applicable_rule_id else []
            )
            _insert_rule_event(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                sequence=sequence, event_type=event_type, occurred_at=now,
                state=ev_state, aggregate_status=aggregate, summary=summary,
                initiated_by=lease.initiated_by, actor_display=lease.actor_display,
                input_refs=[{"type": "rule_retrieval_run", "id": run_id, "version": 1}],
                produced_refs=produced,
                public_error=None if applicable_rule_id else {"code": decision.public_error},
            )
            return RuleCompletion(
                retrieval_run_id=run_id,
                applicable_rule_id=applicable_rule_id,
                state=decision.state.value,
                candidate_count=len(candidates),
                accepted_rate_minor=accepted_rate_minor,
            )

    return dal.run_with_retry(_complete)


def _insert_applicable_rule(
    cur, tenant_id, lease, run_id, accepted: CandidateValidation, now
) -> tuple[str, int]:
    c = accepted.candidate
    rule_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO applicable_rules
            (tenant_id, id, invoice_id, reconstruction_id, retrieval_run_id,
             tariff_clause_id, public_ref, clause_ref, display_excerpt, rate_minor,
             currency, unit, effective_from, effective_to, scope_code,
             source_locator_private, source_version_state, validation_state,
             validation_results, validated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'VERIFIED',
                'VERIFIED',%s,%s)
        ON CONFLICT (tenant_id, reconstruction_id) DO NOTHING;
        """,
        (tenant_id, rule_id, lease.invoice_id, lease.reconstruction_id, run_id,
         c.clause_id, c.public_ref, c.clause_ref, c.display_excerpt,
         accepted.rate_minor, (c.rate_currency or "USD"), (c.rate_unit or "CALENDAR_DAY"),
         c.effective_from, c.effective_to, c.scope_code, c.source_locator,
         json.dumps(accepted.results), now),
    )
    # Bind the rule to every charged day and stamp applicable_rate_minor.
    cur.execute(
        """
        UPDATE reconstruction_charged_days
        SET applicable_rate_minor=%s
        WHERE tenant_id=%s AND reconstruction_id=%s;
        """,
        (accepted.rate_minor, tenant_id, lease.reconstruction_id),
    )
    cur.execute(
        """
        INSERT INTO charged_day_rule_bindings (tenant_id, charged_day_id, applicable_rule_id)
        SELECT tenant_id, id, %s FROM reconstruction_charged_days
        WHERE tenant_id=%s AND reconstruction_id=%s
        ON CONFLICT DO NOTHING;
        """,
        (rule_id, tenant_id, lease.reconstruction_id),
    )
    return rule_id, accepted.rate_minor


def fail_rule(
    dal: DAL, *, lease: RuleTaskLease, error_code: str, retryable: bool
) -> str:
    """Fail closed: no applicable rule, invoice NEEDS_EVIDENCE. No fallback clause."""
    tenant_id = dal.tenant.tenant_id

    def _fail(conn):
        with conn.cursor() as cur:
            _assert_rule_lease(cur, tenant_id, lease)
            cur.execute("SELECT now();")
            now = cur.fetchone()[0]
            will_retry = retryable and lease.attempt < MAX_AUTOMATIC_ATTEMPTS
            task_state = "RETRY_WAIT" if will_retry else "BLOCKED"
            not_before = now + timedelta(seconds=2 ** lease.attempt) if will_retry else None
            cur.execute(
                """
                UPDATE workflow_tasks SET state=%s, not_before=%s, lease_owner=NULL,
                    lease_expires_at=NULL, private_error_code=%s, public_summary=%s,
                    updated_at=%s
                WHERE tenant_id=%s AND id=%s AND current_attempt=%s AND lease_owner=%s;
                """,
                (task_state, not_before, error_code,
                 "Rule search will retry" if will_retry else "Rule search is blocked",
                 now, tenant_id, lease.task_id, lease.attempt, lease.worker_id),
            )
            cur.execute(
                """
                UPDATE workflow_task_attempts SET state=%s, completed_at=%s,
                    private_error_code=%s
                WHERE tenant_id=%s AND task_id=%s AND attempt=%s AND lease_owner=%s;
                """,
                (task_state, now, error_code, tenant_id, lease.task_id, lease.attempt,
                 lease.worker_id),
            )
            aggregate = "RECONSTRUCTING" if will_retry else "NEEDS_EVIDENCE"
            sequence = _lock_invoice_and_advance(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                intake_state="READY_FOR_RECONSTRUCTION", aggregate_status=aggregate,
                status=aggregate, increment=1, occurred_at=now,
            )
            _insert_rule_event(
                cur, tenant_id=tenant_id, invoice_id=lease.invoice_id,
                sequence=sequence,
                event_type=("evidence.rule_search_retry_scheduled" if will_retry
                            else "evidence.rule_search_failed"),
                occurred_at=now, state=task_state, aggregate_status=aggregate,
                summary=("Tariff search will retry" if will_retry
                         else "Tariff search unavailable. No substitute clause is used."),
                initiated_by=lease.initiated_by, actor_display=lease.actor_display,
                input_refs=[{"type": "workflow_task", "id": lease.task_id, "version": 1}],
                public_error={"code": _public_error(error_code)},
            )
            return task_state

    return dal.run_with_retry(_fail)


def _public_error(code: str) -> str:
    if code.startswith("VECTOR_"):
        return "VECTOR_RETRIEVAL_UNAVAILABLE"
    return "NO_APPLICABLE_RULE"


def _load_completion(cur, tenant_id, run_id) -> RuleCompletion:
    cur.execute(
        "SELECT candidate_count FROM rule_retrieval_runs WHERE tenant_id=%s AND id=%s;",
        (tenant_id, run_id),
    )
    count = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT id, rate_minor, validation_state FROM applicable_rules
        WHERE tenant_id=%s AND retrieval_run_id=%s;
        """,
        (tenant_id, run_id),
    )
    rule = cur.fetchone()
    return RuleCompletion(
        retrieval_run_id=run_id,
        applicable_rule_id=str(rule[0]) if rule else None,
        state=rule[2] if rule else "REJECTED",
        candidate_count=count,
        accepted_rate_minor=int(rule[1]) if rule else None,
    )


def _finish_task(cur, tenant_id, lease: RuleTaskLease) -> None:
    cur.execute(
        """
        UPDATE workflow_tasks SET state='COMPLETED', completed_at=now(),
            lease_owner=NULL, lease_expires_at=NULL,
            public_summary='Applicable rule evaluated', updated_at=now()
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


def _assert_rule_lease(cur, tenant_id, lease: RuleTaskLease) -> None:
    cur.execute(
        "SELECT state, current_attempt, lease_owner FROM workflow_tasks "
        "WHERE tenant_id=%s AND id=%s FOR UPDATE;",
        (tenant_id, lease.task_id),
    )
    row = cur.fetchone()
    if (row is None or row[0] != "RUNNING" or int(row[1]) != lease.attempt
            or row[2] != lease.worker_id):
        raise TaskLeaseLostError("TASK_LEASE_LOST")


# Gate 3 events use the EVIDENCE_AGENT role + vector-indexing tool, so this is a
# dedicated inserter rather than the reconstruction one (which hard-codes
# RECONSTRUCTION_AGENT / Managed MCP).
def _insert_rule_event(cur, *, tenant_id, invoice_id, sequence, event_type,
                       occurred_at, state, aggregate_status, summary, initiated_by,
                       actor_display, input_refs, produced_refs=None,
                       output_count=None, public_error=None) -> None:
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_events
            (tenant_id, id, invoice_id, sequence, event_type, schema_version,
             occurred_at, role, task, tool_display_name, state, aggregate_status,
             summary, initiated_by, actor_display, input_object_refs,
             produced_object_refs, output_count, public_error)
        VALUES (%s,%s,%s,%s,%s,1,%s,'EVIDENCE_AGENT','FIND_APPLICABLE_RULE',
                'CockroachDB Distributed Vector Indexing',%s,%s,%s,%s,%s,%s,%s,%s,%s);
        """,
        (tenant_id, event_id, invoice_id, sequence, event_type, occurred_at, state,
         aggregate_status, summary, initiated_by, actor_display,
         json.dumps(input_refs), json.dumps(produced_refs or []), output_count,
         json.dumps(public_error) if public_error else None),
    )
    cur.execute(
        "INSERT INTO event_outbox (tenant_id, invoice_id, event_id, state, available_at) "
        "VALUES (%s,%s,%s,'PENDING',%s);",
        (tenant_id, invoice_id, event_id, occurred_at),
    )

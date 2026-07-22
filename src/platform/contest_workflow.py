"""Minimal later-contest workflow used by the Gate 3 runtime demo.

Recording a contest is input handling, not a seeded decision.  The transaction
locks the already-sealed case, records the carrier response, moves FILED to
CONTESTED, and writes the application audit line atomically.  Replaying the
same contest ID is idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from src.external.dal import DAL


class ContestWorkflowError(RuntimeError):
    pass


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ContestWorkflowError(f"{field}_invalid") from exc


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContestWorkflowError("received_at_invalid") from exc
    if parsed.tzinfo is None:
        raise ContestWorkflowError("received_at_unzoned")
    return parsed.astimezone(UTC)


def record_later_contest(
    dal: DAL,
    *,
    contest_id: str,
    case_id: str,
    received_at: str,
    sender: str,
    claim_text: str,
    claimed_rate: str,
) -> dict[str, object]:
    """Persist one contest against a sealed case and advance its current state."""
    tenant_id = dal.tenant.tenant_id
    contest = _uuid(contest_id, field="contest_id")
    case = _uuid(case_id, field="case_id")
    received = _utc_timestamp(received_at)
    try:
        rate = Decimal(claimed_rate)
    except (InvalidOperation, ValueError) as exc:
        raise ContestWorkflowError("claimed_rate_invalid") from exc
    if not sender.strip() or not claim_text.strip() or not rate.is_finite() or rate < 0:
        raise ContestWorkflowError("contest_input_invalid")

    def _commit(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT carrier_id, state, evidence_hash, evidence_manifest,
                       sealed_by, sealed_at_display, sealed_txn_ts
                FROM cases
                WHERE tenant_id=%s AND id=%s
                FOR UPDATE;
                """,
                (tenant_id, case),
            )
            case_row = cur.fetchone()
            if case_row is None:
                raise ContestWorkflowError("sealed_case_not_found")
            carrier_id, state, evidence_hash, manifest, sealed_by, sealed_at, sealed_txn = case_row
            if state not in {"FILED", "CONTESTED"} or not all(
                (evidence_hash, manifest, sealed_by, sealed_at, sealed_txn)
            ):
                raise ContestWorkflowError("case_not_sealed_for_contest")

            cur.execute(
                """
                SELECT case_id, carrier_id, received_at, sender, claim_text, claimed_rate
                FROM contests WHERE tenant_id=%s AND id=%s;
                """,
                (tenant_id, contest),
            )
            existing = cur.fetchone()
            already_recorded = existing is not None
            if existing is not None:
                expected = (
                    UUID(case),
                    carrier_id,
                    received,
                    sender,
                    claim_text,
                    rate,
                )
                if tuple(existing) != expected:
                    raise ContestWorkflowError("contest_id_conflict")
            else:
                cur.execute(
                    """
                    INSERT INTO contests
                        (tenant_id, id, case_id, carrier_id, received_at,
                         sender, claim_text, claimed_rate, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN');
                    """,
                    (
                        tenant_id,
                        contest,
                        case,
                        carrier_id,
                        received,
                        sender,
                        claim_text,
                        rate,
                    ),
                )
            if state == "FILED":
                cur.execute(
                    """
                    UPDATE cases SET state='CONTESTED', updated_at=now()
                    WHERE tenant_id=%s AND id=%s AND state='FILED';
                    """,
                    (tenant_id, case),
                )
            cur.execute(
                """
                INSERT INTO query_log
                    (tenant_id, kind, tag, sql_text, actor, ok)
                VALUES (%s, 'system', 'contest.record', %s, %s, true);
                """,
                (
                    tenant_id,
                    "COMMIT (contest input + case state transition)",
                    dal.tenant.actor,
                ),
            )
        return {
            "contest_id": contest,
            "case_id": case,
            "state": "CONTESTED",
            "already_recorded": already_recorded,
        }

    return dal.run_with_retry(_commit)

"""Gate 5 — human approval + atomic decision seal.

Binds one human approval to one exact immutable recommendation version, then in
one atomic SERIALIZABLE transaction seals the recommendation, its seven day
judgments, the applicable rule, the reconstruction version, the claim set, the
exact invoice source, the approver authorization, and timestamps/revision.

Idempotency (approval key), optimistic concurrency (expected version + digest),
stale-recommendation rejection, safe repeated approval replay, and concurrent
conflict handling (FOR UPDATE + SERIALIZABLE). Sealed records are never edited in
place — a correction is a new recommendation version + new seal. Reuses the
repo's canonical digest primitives and writes its own in-transaction audit row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.core.seal_manifest import (
    SealInputs,
    approval_request_hash,
    build_seal_manifest,
    is_stale,
    seal_digest,
)
from src.external.dal import DAL


class ApprovalError(ValueError):
    pass


class StaleRecommendationError(ApprovalError):
    pass


class RecommendationNotApprovableError(ApprovalError):
    pass


class ApprovalConflictError(ApprovalError):
    pass


@dataclass(frozen=True)
class SealResult:
    approval_id: str
    seal_id: str
    revision: int
    seal_digest: str
    recommendation_type: str
    already_sealed: bool


def approve_and_seal(
    dal: DAL,
    *,
    recommendation_id: str,
    expected_version: int,
    expected_digest: str,
    idempotency_key: str,
    approver_user_id: str | None,
    approver_display: str,
    approver_kind: str = "SYNTHETIC_DEMO",
    decision: str = "APPROVE",
) -> SealResult:
    tenant_id = dal.tenant.tenant_id
    request_hash = approval_request_hash(
        recommendation_id=recommendation_id, recommendation_version=expected_version,
        recommendation_digest=expected_digest, decision=decision,
    )

    def _commit(conn):
        with conn.cursor() as cur:
            # 1. Lock the recommendation row (concurrency gate).
            cur.execute(
                """
                SELECT id, reconstruction_id, invoice_id, version, digest, state,
                       recommendation_type, applicable_rule_id, disputed_amount_minor,
                       supported_amount_minor, claimed_amount_minor, currency
                FROM recommendations
                WHERE tenant_id=%s AND id=%s FOR UPDATE;
                """,
                (tenant_id, recommendation_id),
            )
            rec = cur.fetchone()
            if rec is None:
                raise RecommendationNotApprovableError("RECOMMENDATION_NOT_FOUND")
            current_version, current_digest, state = int(rec[3]), rec[4], rec[5]
            reconstruction_id, invoice_id = str(rec[1]), str(rec[2])

            # 2. Idempotent replay: same key + same request → return existing.
            cur.execute(
                """
                SELECT id, state, request_hash, response_snapshot
                FROM approvals WHERE tenant_id=%s AND idempotency_key=%s FOR UPDATE;
                """,
                (tenant_id, idempotency_key),
            )
            existing_approval = cur.fetchone()
            if existing_approval is not None:
                if existing_approval[2] != request_hash:
                    raise ApprovalConflictError("IDEMPOTENCY_CONFLICT")
                snapshot = existing_approval[3]
                snapshot = snapshot if isinstance(snapshot, dict) else json.loads(snapshot)
                return SealResult(
                    approval_id=str(existing_approval[0]), seal_id=snapshot["seal_id"],
                    revision=int(snapshot["revision"]),
                    seal_digest=snapshot["seal_digest"],
                    recommendation_type=snapshot["recommendation_type"],
                    already_sealed=True,
                )

            # 3. Stale/optimistic concurrency + frozen-state checks.
            if is_stale(expected_version=expected_version, current_version=current_version,
                        expected_digest=expected_digest, current_digest=current_digest):
                raise StaleRecommendationError("RECOMMENDATION_STALE")
            if state != "FROZEN":
                raise RecommendationNotApprovableError("RECOMMENDATION_NOT_FROZEN")

            # 4. Gather the exact bound inputs.
            claim_set_version, source_ref = _invoice_bindings(cur, tenant_id, invoice_id)
            recon_version = _reconstruction_version(cur, tenant_id, reconstruction_id)
            rule_digest = _rule_digest(cur, tenant_id, rec[7]) if rec[7] else None
            day_digests = _day_judgment_digests(cur, tenant_id, recommendation_id)
            revision = _next_revision(cur, tenant_id, invoice_id)

            inputs = SealInputs(
                invoice_id=invoice_id, reconstruction_id=reconstruction_id,
                reconstruction_version=recon_version,
                recommendation_id=recommendation_id, recommendation_version=current_version,
                recommendation_type=rec[6], recommendation_digest=current_digest,
                disputed_amount_minor=int(rec[8]), supported_amount_minor=int(rec[9]),
                claimed_amount_minor=int(rec[10]), currency=rec[11],
                applicable_rule_id=str(rec[7]) if rec[7] else None,
                applicable_rule_digest=rule_digest,
                claim_set_version=claim_set_version, invoice_source_ref=source_ref,
                day_judgment_digests=day_digests, approver_display=approver_display,
                approver_kind=approver_kind,
            )
            manifest = build_seal_manifest(inputs, revision=revision)
            digest = seal_digest(manifest)

            # 5. Record approval + seal atomically.
            approval_id, seal_id = str(uuid4()), str(uuid4())
            snapshot = {
                "seal_id": seal_id, "revision": revision, "seal_digest": digest,
                "recommendation_type": rec[6],
            }
            cur.execute(
                """
                INSERT INTO approvals
                    (tenant_id, id, invoice_id, reconstruction_id, recommendation_id,
                     recommendation_version, recommendation_digest, idempotency_key,
                     request_hash, approver_user_id, approver_display, approver_kind,
                     decision, state, response_snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'APPROVED',%s);
                """,
                (tenant_id, approval_id, invoice_id, reconstruction_id,
                 recommendation_id, current_version, current_digest, idempotency_key,
                 request_hash, approver_user_id, approver_display, approver_kind,
                 decision, json.dumps(snapshot)),
            )
            cur.execute(
                """
                INSERT INTO decision_seals
                    (tenant_id, id, invoice_id, reconstruction_id, recommendation_id,
                     recommendation_version, applicable_rule_id, claim_set_version,
                     approval_id, approver_display, revision, seal_digest,
                     bound_object_refs, invoice_source_ref_private, public_summary,
                     sealed_txn_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        cluster_logical_timestamp())
                ON CONFLICT (tenant_id, recommendation_id) DO NOTHING;
                """,
                (tenant_id, seal_id, invoice_id, reconstruction_id, recommendation_id,
                 current_version, (str(rec[7]) if rec[7] else None), claim_set_version,
                 approval_id, approver_display, revision, digest,
                 json.dumps(_bound_refs(inputs)), source_ref,
                 f"Decision sealed: {rec[6]} · revision {revision}"),
            )
            # 6. Advance invoice + emit public event + in-transaction audit row.
            _advance_and_emit(cur, tenant_id, invoice_id, recommendation_id, seal_id,
                              revision, rec[6], approver_display)
            cur.execute(
                """
                INSERT INTO query_log (tenant_id, kind, tag, sql_text, actor, ok)
                VALUES (%s, 'audit', 'seal', %s, %s, true);
                """,
                (tenant_id, f"APPROVE & SEAL · rec {recommendation_id} · "
                            f"rev {revision} · by {approver_display}", approver_display),
            )
            return SealResult(
                approval_id=approval_id, seal_id=seal_id, revision=revision,
                seal_digest=digest, recommendation_type=rec[6], already_sealed=False,
            )

    return dal.run_with_retry(_commit)


def _invoice_bindings(cur, tenant_id, invoice_id) -> tuple[int, str]:
    cur.execute(
        "SELECT active_claim_set_version FROM invoices WHERE tenant_id=%s AND id=%s;",
        (tenant_id, invoice_id),
    )
    row = cur.fetchone()
    claim_set_version = int(row[0]) if row and row[0] is not None else 1
    cur.execute(
        """
        SELECT s3_object_key_private, s3_version_id_private FROM invoice_sources
        WHERE tenant_id=%s AND invoice_id=%s AND source_type='INVOICE_PDF' LIMIT 1;
        """,
        (tenant_id, invoice_id),
    )
    src = cur.fetchone()
    source_ref = f"{src[0]}@{src[1]}" if src else f"unbound-source:{invoice_id}"
    return claim_set_version, source_ref


def _reconstruction_version(cur, tenant_id, reconstruction_id) -> int:
    cur.execute(
        "SELECT version FROM reconstructions WHERE tenant_id=%s AND id=%s;",
        (tenant_id, reconstruction_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 1


def _rule_digest(cur, tenant_id, rule_id) -> str | None:
    cur.execute(
        """
        SELECT public_ref, rate_minor, currency, effective_from, effective_to
        FROM applicable_rules WHERE tenant_id=%s AND id=%s;
        """,
        (tenant_id, rule_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return prefixed_sha256(canonical_json_bytes({
        "ref": row[0], "rate": int(row[1]), "cur": row[2],
        "from": row[3].isoformat(), "to": row[4].isoformat() if row[4] else None,
    }))


def _day_judgment_digests(cur, tenant_id, recommendation_id) -> tuple[str, ...]:
    cur.execute(
        """
        SELECT charge_date, invoice_rate_minor, applicable_rate_minor,
               discrepancy_minor, outcome
        FROM charged_day_judgments
        WHERE tenant_id=%s AND recommendation_id=%s ORDER BY charge_date;
        """,
        (tenant_id, recommendation_id),
    )
    digests = []
    for r in cur.fetchall():
        digests.append(prefixed_sha256(canonical_json_bytes({
            "d": r[0].isoformat(), "inv": int(r[1]),
            "app": int(r[2]) if r[2] is not None else None,
            "disc": int(r[3]), "o": r[4],
        })))
    return tuple(digests)


def _next_revision(cur, tenant_id, invoice_id) -> int:
    cur.execute(
        "SELECT COALESCE(max(revision),0)+1 FROM decision_seals "
        "WHERE tenant_id=%s AND invoice_id=%s;",
        (tenant_id, invoice_id),
    )
    return int(cur.fetchone()[0])


def _bound_refs(inputs: SealInputs) -> list[dict]:
    refs = [
        {"type": "recommendation", "id": inputs.recommendation_id,
         "version": inputs.recommendation_version},
        {"type": "reconstruction", "id": inputs.reconstruction_id,
         "version": inputs.reconstruction_version},
        {"type": "claim_set", "version": inputs.claim_set_version},
    ]
    if inputs.applicable_rule_id:
        refs.append({"type": "applicable_rule", "id": inputs.applicable_rule_id,
                     "version": 1})
    return refs


def _advance_and_emit(cur, tenant_id, invoice_id, recommendation_id, seal_id,
                      revision, rec_type, approver_display) -> None:
    from src.platform.reconstruction_repository import _lock_invoice_and_advance

    outcome_status = {
        "DISPUTE": "DISPUTED",
        "APPROVE_FOR_PAYMENT": "APPROVED_FOR_PAYMENT",
    }.get(rec_type, "READY_TO_SEND")
    cur.execute("SELECT now();")
    now = cur.fetchone()[0]
    sequence = _lock_invoice_and_advance(
        cur, tenant_id=tenant_id, invoice_id=invoice_id,
        intake_state="READY_FOR_RECONSTRUCTION", aggregate_status=outcome_status,
        status=outcome_status, increment=1, occurred_at=now,
    )
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_events
            (tenant_id, id, invoice_id, sequence, event_type, schema_version,
             occurred_at, role, task, tool_display_name, state, aggregate_status,
             summary, actor_display, input_object_refs, produced_object_refs)
        VALUES (%s,%s,%s,%s,'decision.sealed',1,%s,'DECISION_ENGINE','SEAL_DECISION',
                'CockroachDB transaction','COMPLETED',%s,%s,%s,%s,%s);
        """,
        (tenant_id, event_id, invoice_id, sequence, now, outcome_status,
         f"Decision sealed and {outcome_status.lower().replace('_',' ')}",
         approver_display,
         json.dumps([{"type": "recommendation", "id": recommendation_id, "version": 1}]),
         json.dumps([{"type": "decision_seal", "id": seal_id, "version": revision}])),
    )
    cur.execute(
        "INSERT INTO event_outbox (tenant_id, invoice_id, event_id, state, available_at) "
        "VALUES (%s,%s,%s,'PENDING',%s);",
        (tenant_id, invoice_id, event_id, now),
    )

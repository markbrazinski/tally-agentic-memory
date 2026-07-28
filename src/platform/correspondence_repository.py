"""Gate 6 — sealed-record drafting + gated controlled send.

Drafts correspondence ONLY from the sealed decision fact pack (locked financial/
identifier fields validated), then on a second human authorization runs fresh
MCP / vector-binding / exact-source / no-fallback / locked-field gate checks. All
must pass, or the send is blocked and the controlled provider is never called.
One idempotent send attempt records the controlled provider acknowledgement; a
retry never duplicates delivery.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from src.core.correspondence import (
    GateResult,
    GateState,
    SealedFactPack,
    build_subject,
    evaluate_send_gates,
    locked_fields,
    locked_fields_digest,
    validate_draft_locked_fields,
)
from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.external.controlled_mail import (
    RECIPIENT_CLASS,
    ControlledSendError,
    DemonstrationInboxProvider,
)
from src.external.correspondence_bedrock import (
    DeterministicDraftGenerator,
    DraftGeneratorProtocol,
)
from src.external.dal import DAL


class CorrespondenceError(ValueError):
    pass


class DraftNotFoundError(CorrespondenceError):
    pass


class SendConflictError(CorrespondenceError):
    pass


# A gate check returns (state, detail). Injected so the caller wires the real
# fresh MCP / vector / source checks (and tests can force a failure).
GateCheck = Callable[[], GateResult]


@dataclass(frozen=True)
class DraftResult:
    draft_id: str
    seal_digest: str
    validation_state: str


@dataclass(frozen=True)
class SendResult:
    send_attempt_id: str
    send_state: str
    gate_state: str
    provider_message_id: str | None
    blocked_reason: str | None
    duplicate: bool


def load_sealed_fact_pack(dal: DAL, *, decision_seal_id: str) -> SealedFactPack:
    """Read the sealed decision into a fact pack — the ONLY draft input."""
    tenant_id = dal.tenant.tenant_id
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.invoice_id, s.seal_digest, r.recommendation_type,
                   r.disputed_amount_minor, r.supported_amount_minor, r.currency,
                   ar.clause_ref, i.invoice_no
            FROM decision_seals s
            JOIN recommendations r
              ON r.tenant_id=s.tenant_id AND r.id=s.recommendation_id
            JOIN invoices i ON i.tenant_id=s.tenant_id AND i.id=s.invoice_id
            LEFT JOIN applicable_rules ar
              ON ar.tenant_id=s.tenant_id AND ar.id=s.applicable_rule_id
            WHERE s.tenant_id=%s AND s.id=%s;
            """,
            (tenant_id, decision_seal_id),
        )
        row = cur.fetchone()
        if row is None:
            raise DraftNotFoundError("SEAL_NOT_FOUND")
        invoice_id = str(row[0])
        cur.execute(
            """
            SELECT normalized_value FROM extracted_claims c
            JOIN claim_sets cs ON cs.tenant_id=c.tenant_id AND cs.id=c.claim_set_id
            WHERE c.tenant_id=%s AND cs.invoice_id=%s AND c.field_name=%s LIMIT 1;
            """,
            (tenant_id, invoice_id, "container_number"),
        )
        container_row = cur.fetchone()
        container = _norm(container_row[0]) if container_row else "UNKNOWN"
        period = _charge_period(cur, tenant_id, decision_seal_id)
    return SealedFactPack(
        invoice_id=invoice_id, decision_seal_id=decision_seal_id, seal_digest=row[1],
        recommendation_type=row[2], disputed_amount_minor=int(row[3]),
        supported_amount_minor=int(row[4]), currency=row[5],
        charged_period_start=period[0], charged_period_end=period[1],
        container_ref=str(container), invoice_no=row[7] or "INV",
        rule_ref=row[6] or "UNKNOWN",
    )


def draft_from_sealed(
    dal: DAL, *, decision_seal_id: str, body_prose: str | None = None,
    draft_generator: DraftGeneratorProtocol | None = None,
) -> DraftResult:
    """Create the bounded draft from the sealed fact pack (idempotent per seal).

    The locked fields are copied from the seal; the prose is the only free text.
    When ``body_prose`` is not supplied, it is generated from the sealed fact pack
    by ``draft_generator`` (default: the deterministic no-network generator; the
    platform wires the Bedrock generator). The model writes prose only — the
    locked fields below are always re-derived from the seal, never the prose.
    """
    tenant_id = dal.tenant.tenant_id
    pack = load_sealed_fact_pack(dal, decision_seal_id=decision_seal_id)
    if body_prose is None:
        generator = draft_generator or DeterministicDraftGenerator()
        body_prose = generator.draft_body(pack)
    fields = locked_fields(pack)
    fields_digest = locked_fields_digest(pack)
    validation = validate_draft_locked_fields(pack, fields)
    subject = build_subject(pack)

    def _draft(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM correspondence_drafts
                WHERE tenant_id=%s AND decision_seal_id=%s;
                """,
                (tenant_id, decision_seal_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                return DraftResult(str(existing[0]), pack.seal_digest, "VALIDATED")
            draft_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO correspondence_drafts
                    (tenant_id, id, invoice_id, decision_seal_id, seal_digest,
                     recipient_class, subject, body_prose, locked_fields,
                     locked_fields_digest, validation_state, state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DRAFT_READY');
                """,
                (tenant_id, draft_id, pack.invoice_id, decision_seal_id,
                 pack.seal_digest, RECIPIENT_CLASS, subject, body_prose,
                 json.dumps(fields), fields_digest,
                 "VALIDATED" if validation.ok else "INVALID"),
            )
            return DraftResult(draft_id, pack.seal_digest,
                               "VALIDATED" if validation.ok else "INVALID")

    return dal.run_with_retry(_draft)


def approve_and_send(
    dal: DAL,
    *,
    draft_id: str,
    idempotency_key: str,
    second_approver_display: str,
    gate_checks: dict[str, GateCheck],
    provider: DemonstrationInboxProvider,
) -> SendResult:
    """Second authorization → fresh gates → one idempotent controlled send.

    Every gate in SEND_GATES is evaluated fresh here (SECOND_AUTHORIZATION and
    LOCKED_FIELDS are computed; the rest come from injected fresh checks). If any
    fails, the send is blocked and the provider is never called.
    """
    tenant_id = dal.tenant.tenant_id

    def _send(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.invoice_id, d.decision_seal_id, d.seal_digest,
                       d.subject, d.body_prose, d.locked_fields, d.locked_fields_digest,
                       d.validation_state
                FROM correspondence_drafts d
                WHERE tenant_id=%s AND id=%s FOR UPDATE;
                """,
                (tenant_id, draft_id),
            )
            draft = cur.fetchone()
            if draft is None:
                raise DraftNotFoundError("DRAFT_NOT_FOUND")
            request_hash = prefixed_sha256(canonical_json_bytes({
                "draft": draft_id, "seal_digest": draft[3],
            }))

            # Idempotent replay.
            cur.execute(
                """
                SELECT id, send_state, gate_state, provider_message_id,
                       blocked_reason, request_hash
                FROM send_attempts WHERE tenant_id=%s AND idempotency_key=%s FOR UPDATE;
                """,
                (tenant_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[5] != request_hash:
                    raise SendConflictError("IDEMPOTENCY_CONFLICT")
                return SendResult(str(existing[0]), existing[1], existing[2],
                                  existing[3], existing[4], True)

            send_attempt_id = str(uuid4())
            provider_key = prefixed_sha256(canonical_json_bytes({
                "attempt": send_attempt_id, "seal": draft[3],
            })).removeprefix("sha256:")

            # Evaluate every gate fresh.
            results: dict[str, GateResult] = {
                "SECOND_AUTHORIZATION": GateResult(
                    "SECOND_AUTHORIZATION",
                    GateState.VERIFIED if second_approver_display else GateState.FAILED,
                    None),
                "LOCKED_FIELDS": GateResult(
                    "LOCKED_FIELDS",
                    GateState.VERIFIED if draft[8] == "VALIDATED" else GateState.FAILED,
                    None),
            }
            for code, check in gate_checks.items():
                results[code] = check()
            decision = evaluate_send_gates(results)

            # Persist the send attempt + per-gate runs (always, for audit).
            cur.execute(
                """
                INSERT INTO send_attempts
                    (tenant_id, id, invoice_id, draft_id, decision_seal_id,
                     idempotency_key, request_hash, second_approver_display,
                     gate_state, send_state, provider_idempotency_key,
                     recipient_class, blocked_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                """,
                (tenant_id, send_attempt_id, draft[1], draft_id, draft[2],
                 idempotency_key, request_hash, second_approver_display,
                 "PASSED" if decision.permitted else "BLOCKED",
                 "PENDING" if decision.permitted else "SEND_BLOCKED", provider_key,
                 RECIPIENT_CLASS, decision.blocked_reason),
            )
            for result in decision.gate_results:
                cur.execute(
                    """
                    INSERT INTO send_gate_runs
                        (tenant_id, id, send_attempt_id, gate_code, gate_state, detail)
                    VALUES (%s,%s,%s,%s,%s,%s);
                    """,
                    (tenant_id, str(uuid4()), send_attempt_id, result.gate_code,
                     result.state.value, result.detail),
                )

            if not decision.permitted:
                _emit_send_event(cur, tenant_id, draft[1], "correspondence.send_blocked",
                                 "BLOCKED", second_approver_display,
                                 {"code": decision.blocked_reason})
                return SendResult(send_attempt_id, "SEND_BLOCKED", "BLOCKED", None,
                                  decision.blocked_reason, False)

            # All gates passed — call the controlled provider (idempotent).
            try:
                receipt = provider.send(
                    provider_idempotency_key=provider_key, subject=draft[4],
                    body=draft[5], recipient_class=RECIPIENT_CLASS,
                )
            except ControlledSendError as exc:
                cur.execute(
                    """
                    UPDATE send_attempts SET send_state='SEND_FAILED_RETRYABLE',
                        blocked_reason=%s WHERE tenant_id=%s AND id=%s;
                    """,
                    (str(exc), tenant_id, send_attempt_id),
                )
                _emit_send_event(cur, tenant_id, draft[1], "correspondence.send_failed",
                                 "FAILED", second_approver_display, {"code": str(exc)})
                return SendResult(send_attempt_id, "SEND_FAILED_RETRYABLE", "PASSED",
                                  None, str(exc), False)

            cur.execute(
                """
                UPDATE send_attempts SET send_state='SENT', provider_message_id=%s,
                    acknowledged_at=now(), response_snapshot=%s
                WHERE tenant_id=%s AND id=%s;
                """,
                (receipt.provider_message_id,
                 json.dumps({"recipient_class": receipt.recipient_class,
                             "disclaimer": receipt.disclaimer}),
                 tenant_id, send_attempt_id),
            )
            _emit_send_event(cur, tenant_id, draft[1], "correspondence.sent", "COMPLETED",
                             second_approver_display,
                             None, message_id=receipt.provider_message_id)
            return SendResult(send_attempt_id, "SENT", "PASSED",
                              receipt.provider_message_id, None, receipt.duplicate)

    return dal.run_with_retry(_send)


def _emit_send_event(cur, tenant_id, invoice_id, event_type, state, actor,
                     public_error, *, message_id=None):
    from src.platform.reconstruction_repository import _lock_invoice_and_advance

    cur.execute("SELECT now();")
    now = cur.fetchone()[0]
    if event_type == "correspondence.sent":
        # A successful send preserves the sealed outcome (DISPUTED /
        # APPROVED_FOR_PAYMENT) — sending does not regress the invoice state.
        cur.execute("SELECT status FROM invoices WHERE tenant_id=%s AND id=%s;",
                    (tenant_id, invoice_id))
        current = cur.fetchone()
        aggregate = current[0] if current else "READY_TO_SEND"
    else:
        aggregate = "SEND_FAILED"
    sequence = _lock_invoice_and_advance(
        cur, tenant_id=tenant_id, invoice_id=invoice_id,
        intake_state="READY_FOR_RECONSTRUCTION", aggregate_status=aggregate,
        status=aggregate, increment=1, occurred_at=now,
    )
    produced = ([{"type": "send_receipt", "id": message_id, "version": 1}]
                if message_id else [])
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_events
            (tenant_id, id, invoice_id, sequence, event_type, schema_version,
             occurred_at, role, task, tool_display_name, state, aggregate_status,
             summary, actor_display, input_object_refs, produced_object_refs,
             public_error)
        VALUES (%s,%s,%s,%s,%s,1,%s,'CORRESPONDENCE_AGENT','SEND_CORRESPONDENCE',
                'Controlled mail provider',%s,%s,%s,%s,%s,%s,%s);
        """,
        (tenant_id, event_id, invoice_id, sequence, event_type, now, state, aggregate,
         _summary(event_type), actor, json.dumps([]), json.dumps(produced),
         json.dumps(public_error) if public_error else None),
    )
    cur.execute(
        "INSERT INTO event_outbox (tenant_id, invoice_id, event_id, state, available_at) "
        "VALUES (%s,%s,%s,'PENDING',%s);",
        (tenant_id, invoice_id, event_id, now),
    )


def _summary(event_type: str) -> str:
    return {
        "correspondence.sent": "Adjustment request sent to the controlled demonstration inbox",
        "correspondence.send_blocked": "Send blocked. Required memory or evidence check failed.",
        "correspondence.send_failed": "Controlled provider did not acknowledge; will retry.",
    }.get(event_type, event_type)


def _charge_period(cur, tenant_id, decision_seal_id) -> tuple[str, str]:
    cur.execute(
        """
        SELECT min(charge_date), max(charge_date)
        FROM charged_day_judgments j
        JOIN decision_seals s ON s.tenant_id=j.tenant_id
          AND s.recommendation_id=j.recommendation_id
        WHERE j.tenant_id=%s AND s.id=%s;
        """,
        (tenant_id, decision_seal_id),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return ("", "")
    return (row[0].isoformat(), row[1].isoformat())


def _norm(value) -> str:
    return value if isinstance(value, str) else json.loads(value)

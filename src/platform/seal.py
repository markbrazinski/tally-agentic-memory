"""The seal transaction (TDD §2.21-B): the one irreversible action in the product.

One atomic transaction via DAL.run_with_retry: lock the case row, hash all
evidence, flip state to FILED, record who sealed it and CockroachDB's own
logical clock (the eventual AOST replay target, TDD §6/§3.8), insert a
ledger event, and write the audit line INTO THE SAME TRANSACTION - this is
the real, non-deferred answer to the gap src/external/dal.py's
run_with_retry docstring flagged back in B0-S1/S2 ("any future caller
needing both atomicity AND logging... must add that logging itself").
Idempotent: a second Approve validates the already-FILED receipt without
rewriting product state; it appends only the disclosure audit row.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from src.core.receipt import canonical_json_bytes, prefixed_sha256
from src.external.dal import DAL

FRESH_SEALABLE_STATES = ("ANALYZED",)
IDEMPOTENT_SEALED_STATES = ("FILED",)
BLOCKED_SEAL_STATES = ("CONTESTED", "RESOLVED")


class CaseNotFoundError(Exception):
    pass


class CaseNotSealableError(Exception):
    """Raised when the case is in a state the seal can never apply to
    (CONTESTED/RESOLVED) - the caller maps this to 409."""


class EmptyEvidenceError(Exception):
    """Raised before any seal writes when a case has no evidence rows."""


class EvidenceIntegrityError(Exception):
    """Raised when stored evidence no longer matches its canonical hash."""


class SealedCaseIntegrityError(Exception):
    """Raised when an allegedly sealed case has incomplete or altered state."""


class ApprovalRecordError(Exception):
    """Raised when sealing cannot record exactly one human approval row."""


def _evidence_hash(manifest: dict) -> str:
    """Hash canonical versioned JSON, never ambiguous concatenated fields."""
    return prefixed_sha256(canonical_json_bytes(manifest))


def _json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _content_hash(content: object) -> str:
    """Canonical bare SHA-256 stored on ``case_evidence.content_sha256``."""
    return prefixed_sha256(canonical_json_bytes(content)).removeprefix("sha256:")


def _stored_content_hash_matches(stored: object, computed: str) -> bool:
    return isinstance(stored, str) and stored in {computed, f"sha256:{computed}"}


def _timestamp_text(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("approval timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


_RESERVED_MANIFEST_EVIDENCE_KEYS = {
    "content",
    "content_sha256",
    "evidence_id",
    "kind",
    "source_id",
    "source_table",
}


def _validated_evidence_content(content: object, content_sha256: object) -> dict:
    parsed_content = _json_value(content)
    if not isinstance(parsed_content, dict):
        raise EvidenceIntegrityError("case evidence content must be a JSON object")
    reserved = _RESERVED_MANIFEST_EVIDENCE_KEYS.intersection(parsed_content)
    if reserved:
        raise EvidenceIntegrityError(
            "case evidence content uses reserved manifest keys: " + ", ".join(sorted(reserved))
        )
    computed = _content_hash(parsed_content)
    if not _stored_content_hash_matches(content_sha256, computed):
        raise EvidenceIntegrityError("case evidence canonical content hash mismatch")
    return parsed_content


def _build_manifest(
    *,
    tenant_id: str,
    case_id: str,
    invoice_id: str,
    finding_id: str,
    evidence_rows: list[tuple],
    approved_by: str,
    approved_at: str,
) -> dict:
    evidence = []
    recommendation = None
    calculation = None
    for (
        evidence_id,
        kind,
        source_table,
        source_id,
        content,
        content_sha256,
        _sealed,
    ) in evidence_rows:
        parsed_content = _validated_evidence_content(content, content_sha256)
        canonical_content_sha256 = _content_hash(parsed_content)
        if recommendation is None and isinstance(parsed_content, dict):
            recommendation = parsed_content.get("recommendation")
            if recommendation is None and isinstance(parsed_content.get("calculation"), dict):
                recommendation = parsed_content["calculation"].get("recommendation")
        if calculation is None and isinstance(parsed_content, dict):
            candidate = parsed_content.get("calculation")
            if isinstance(candidate, dict):
                calculation = candidate
        flattened = dict(parsed_content) if isinstance(parsed_content, dict) else {}
        flattened.update(
            {
                "evidence_id": str(evidence_id),
                "kind": kind,
                "source_table": source_table,
                "source_id": str(source_id),
                "content_sha256": canonical_content_sha256,
                # Preserve the exact JSON value over which content_sha256 was
                # recomputed. Direct fields remain for the v1 verifier shape.
                "content": parsed_content,
            }
        )
        evidence.append(flattened)
    return {
        "manifest_version": 1,
        "case_id": str(case_id),
        "tenant_id": str(tenant_id),
        "invoice_id": str(invoice_id),
        "finding_id": str(finding_id),
        "evidence": evidence,
        "calculation": calculation,
        "recommendation": recommendation,
        "approved_by": str(approved_by),
        "approved_at": approved_at,
    }


def _validate_existing_seal(
    *,
    state: str,
    tenant_id: str,
    case_id: str,
    invoice_id: object,
    finding_id: object,
    sealed_txn_ts: object,
    evidence_hash: object,
    sealed_by: object,
    sealed_at_display: object,
    manifest_version: object,
    evidence_manifest: object,
    evidence_rows: list[tuple],
    approval_state: object,
) -> dict:
    if state not in IDEMPOTENT_SEALED_STATES:
        raise CaseNotSealableError(state)
    manifest = _json_value(evidence_manifest) if evidence_manifest else None
    if not isinstance(manifest, dict):
        raise SealedCaseIntegrityError("sealed case has no evidence manifest")
    if not evidence_rows or not all(bool(row[6]) for row in evidence_rows):
        raise SealedCaseIntegrityError("sealed case has missing or unsealed evidence")
    if sealed_txn_ts is None or sealed_by is None or sealed_at_display is None:
        raise SealedCaseIntegrityError("sealed case has incomplete seal metadata")
    if manifest_version != 1 or manifest.get("manifest_version") != 1:
        raise SealedCaseIntegrityError("sealed case has an unsupported manifest version")
    if approval_state != "APPROVED":
        raise SealedCaseIntegrityError("sealed case has no recorded human approval")

    expected_manifest = _build_manifest(
        tenant_id=tenant_id,
        case_id=case_id,
        invoice_id=str(invoice_id),
        finding_id=str(finding_id),
        evidence_rows=evidence_rows,
        approved_by=str(sealed_by),
        approved_at=_timestamp_text(sealed_at_display),
    )
    if canonical_json_bytes(manifest) != canonical_json_bytes(expected_manifest):
        raise SealedCaseIntegrityError("sealed manifest does not match stored evidence")
    computed_hash = _evidence_hash(manifest)
    if (
        not isinstance(evidence_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_hash) is None
        or evidence_hash != computed_hash
    ):
        raise SealedCaseIntegrityError("sealed manifest hash is invalid")
    return manifest


def seal_case(dal: DAL, *, case_id: str, sealed_by_user_id: str, sealed_at_display: str) -> dict:
    """Executes the seal transaction. Returns a dict matching
    contract/fixtures/POST_cases_id_approve.json's shape, with an
    additional `already_sealed` key (False on a fresh seal, True on the
    idempotent double-press path - both are valid 200 responses per
    TDD §3.2, the caller doesn't need a different status code).

    Raises CaseNotFoundError (caller maps to 404) or
    CaseNotSealableError (caller maps to 409).
    """
    tenant_id = dal.tenant.tenant_id
    actor = dal.tenant.actor
    approved_by = str(sealed_by_user_id)
    approved_at = _timestamp_text(sealed_at_display)

    def _commit(conn):
        with conn.cursor() as cur:
            # Lock the case row for the duration of the transaction -
            # CockroachDB's SERIALIZABLE isolation + this FOR UPDATE
            # together are what make a concurrent double-press safe: the
            # second transaction's SELECT blocks until the first commits,
            # then sees the already-FILED state and takes the idempotent
            # path below, never a second set of writes.
            cur.execute(
                "SELECT state, sealed_txn_ts, evidence_hash, invoice_id, finding_id, "
                "carrier_id, draft_dispute, evidence_manifest, sealed_by, "
                "sealed_at_display, manifest_version FROM cases "
                "WHERE tenant_id=%s AND id=%s FOR UPDATE;",
                (tenant_id, case_id),
            )
            row = cur.fetchone()
            if row is None:
                raise CaseNotFoundError(case_id)
            (
                state,
                existing_sealed_txn_ts,
                existing_evidence_hash,
                invoice_id,
                finding_id,
                _existing_carrier_id,
                _existing_draft_dispute,
                existing_manifest,
                existing_sealed_by,
                existing_sealed_at_display,
                existing_manifest_version,
            ) = row

            if state in BLOCKED_SEAL_STATES:
                raise CaseNotSealableError(state)

            if state in IDEMPOTENT_SEALED_STATES:
                cur.execute(
                    "SELECT id, kind, source_table, source_id, content, content_sha256, sealed "
                    "FROM case_evidence WHERE tenant_id=%s AND case_id=%s ORDER BY id;",
                    (tenant_id, case_id),
                )
                existing_evidence_rows = cur.fetchall()
                cur.execute(
                    "SELECT human_approval_state FROM findings "
                    "WHERE tenant_id=%s AND id=%s;",
                    (tenant_id, finding_id),
                )
                approval_row = cur.fetchone()
                parsed_existing_manifest = _validate_existing_seal(
                    state=state,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    invoice_id=invoice_id,
                    finding_id=finding_id,
                    sealed_txn_ts=existing_sealed_txn_ts,
                    evidence_hash=existing_evidence_hash,
                    sealed_by=existing_sealed_by,
                    sealed_at_display=existing_sealed_at_display,
                    manifest_version=existing_manifest_version,
                    evidence_manifest=existing_manifest,
                    evidence_rows=existing_evidence_rows,
                    approval_state=approval_row[0] if approval_row else None,
                )
                cur.execute(
                    """
                    INSERT INTO query_log (tenant_id, kind, tag, sql_text, actor, ok)
                    VALUES (%s, 'audit', 'seal', %s, %s, true);
                    """,
                    (
                        tenant_id,
                        f"already sealed · txn {existing_sealed_txn_ts}",
                        actor,
                    ),
                )
                return {
                    "already_sealed": True,
                    "state": state,
                    "sealed_txn_ts": str(existing_sealed_txn_ts),
                    "evidence_hash": existing_evidence_hash,
                    "evidence_count": len((parsed_existing_manifest or {}).get("evidence", [])),
                    "evidence_manifest": parsed_existing_manifest,
                    "sealed_by": str(existing_sealed_by),
                    "sealed_at_display": _timestamp_text(existing_sealed_at_display),
                    "manifest_version": existing_manifest_version,
                    "commit_line": None,
                }

            if state not in FRESH_SEALABLE_STATES:
                raise CaseNotSealableError(state)

            cur.execute(
                "SELECT id, kind, source_table, source_id, content, content_sha256, sealed "
                "FROM case_evidence WHERE tenant_id=%s AND case_id=%s "
                "ORDER BY id;",
                (tenant_id, case_id),
            )
            evidence_rows = cur.fetchall()
            if not evidence_rows:
                raise EmptyEvidenceError(case_id)
            if any(bool(row[6]) for row in evidence_rows):
                raise EvidenceIntegrityError("unsealed case contains sealed evidence")
            manifest = _build_manifest(
                tenant_id=tenant_id,
                case_id=case_id,
                invoice_id=invoice_id,
                finding_id=finding_id,
                evidence_rows=evidence_rows,
                approved_by=approved_by,
                approved_at=approved_at,
            )
            evidence_hash = _evidence_hash(manifest)
            evidence_count = len(evidence_rows)

            cur.execute(
                "UPDATE case_evidence SET sealed=true WHERE tenant_id=%s AND case_id=%s;",
                (tenant_id, case_id),
            )

            cur.execute(
                """
                UPDATE cases SET state='FILED', sealed_at_display=%s,
                                 sealed_txn_ts=cluster_logical_timestamp(),
                                 sealed_by=%s, evidence_hash=%s,
                                 manifest_version=1, evidence_manifest=%s,
                                 updated_at=now()
                WHERE tenant_id=%s AND id=%s
                RETURNING sealed_txn_ts, carrier_id, draft_dispute, sealed_by,
                          sealed_at_display, manifest_version;
                """,
                (
                    approved_at,
                    approved_by,
                    evidence_hash,
                    json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str),
                    tenant_id,
                    case_id,
                ),
            )
            (
                sealed_txn_ts,
                carrier_id,
                draft_dispute,
                stored_sealed_by,
                stored_sealed_at_display,
                stored_manifest_version,
            ) = cur.fetchone()

            cur.execute(
                "UPDATE findings SET human_approval_state='APPROVED' "
                "WHERE tenant_id=%s AND id=%s RETURNING id;",
                (tenant_id, finding_id),
            )
            approval_rows = cur.fetchall()
            if len(approval_rows) != 1:
                raise ApprovalRecordError("seal must update exactly one finding approval")

            cur.execute(
                """
                INSERT INTO ledger_events
                    (tenant_id, case_id, carrier_id, kind, occurred_on, details)
                VALUES (%s, %s, %s, 'FILED', now()::DATE, %s);
                """,
                (
                    tenant_id,
                    case_id,
                    carrier_id,
                    json.dumps(
                        {
                            "approved_by": str(stored_sealed_by),
                            "approved_at": _timestamp_text(stored_sealed_at_display),
                            "recommendation": manifest["recommendation"],
                            "evidence_hash": evidence_hash,
                        },
                        sort_keys=True,
                    ),
                ),
            )

            audit_line = (
                f"APPROVE & FILE · case {case_id} · by {actor} · txn {sealed_txn_ts}"
            )
            cur.execute(
                """
                INSERT INTO query_log (tenant_id, kind, tag, sql_text, actor, ok)
                VALUES (%s, 'audit', 'seal', %s, %s, true);
                """,
                (tenant_id, audit_line, actor),
            )

        commit_line = (
            f"1 finding · {evidence_count} evidence rows · 1 draft — single transaction"
        )
        return {
            "already_sealed": False,
            "state": "FILED",
            "sealed_at_display": _timestamp_text(stored_sealed_at_display),
            "sealed_txn_ts": str(sealed_txn_ts),
            "evidence_hash": evidence_hash,
            "evidence_count": evidence_count,
            "evidence_manifest": manifest,
            "sealed_by": str(stored_sealed_by),
            "manifest_version": stored_manifest_version,
            "commit_line": commit_line,
        }

    return dal.run_with_retry(_commit)

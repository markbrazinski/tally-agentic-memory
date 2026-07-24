"""Pure canonical seal-manifest construction and digest for Gate 5.

The seal digest is a deterministic hash over the exact bound inputs: the
recommendation (type + amounts + version + digest), the seven day judgments, the
applicable rule version, the reconstruction version, the claim-set version, the
exact invoice-source reference, and the approver authorization. Same inputs →
same digest; any drift changes it. Zero external I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.receipt import canonical_json_bytes, prefixed_sha256, sha256_hex


@dataclass(frozen=True)
class SealInputs:
    invoice_id: str
    reconstruction_id: str
    reconstruction_version: int
    recommendation_id: str
    recommendation_version: int
    recommendation_type: str
    recommendation_digest: str
    disputed_amount_minor: int
    supported_amount_minor: int
    claimed_amount_minor: int
    currency: str
    applicable_rule_id: str | None
    applicable_rule_digest: str | None
    claim_set_version: int
    invoice_source_ref: str
    day_judgment_digests: tuple[str, ...]  # per-day, order-significant
    approver_display: str
    approver_kind: str  # AUTHENTICATED | SYNTHETIC_DEMO


def build_seal_manifest(inputs: SealInputs, *, revision: int) -> dict:
    """Canonical, public-safe-shaped manifest bound at seal time.

    The invoice_source_ref is bound but hashed, not exposed in public
    projections. Every version-bearing input is included so the digest pins the
    exact decision lineage.
    """
    return {
        "schema_version": 1,
        "revision": revision,
        "invoice_id": inputs.invoice_id,
        "reconstruction": {
            "id": inputs.reconstruction_id,
            "version": inputs.reconstruction_version,
        },
        "recommendation": {
            "id": inputs.recommendation_id,
            "version": inputs.recommendation_version,
            "type": inputs.recommendation_type,
            "digest": inputs.recommendation_digest,
            "disputed_amount_minor": inputs.disputed_amount_minor,
            "supported_amount_minor": inputs.supported_amount_minor,
            "claimed_amount_minor": inputs.claimed_amount_minor,
            "currency": inputs.currency,
        },
        "applicable_rule": {
            "id": inputs.applicable_rule_id,
            "digest": inputs.applicable_rule_digest,
        },
        "claim_set_version": inputs.claim_set_version,
        "invoice_source_binding_sha256": _sha(inputs.invoice_source_ref),
        "day_judgments": list(inputs.day_judgment_digests),
        "approver": {
            "display": inputs.approver_display,
            "kind": inputs.approver_kind,
        },
    }


def seal_digest(manifest: dict) -> str:
    """Deterministic seal digest over the canonical manifest.

    Reuses the repo's canonical serialization + prefixed-sha256 primitives so the
    Gate 5 seal digest is computed the same way as every other sealed record.
    """
    return prefixed_sha256(canonical_json_bytes(manifest))


def approval_request_hash(
    *, recommendation_id: str, recommendation_version: int, recommendation_digest: str,
    decision: str,
) -> str:
    """Fingerprint of an approval request — a different payload under one
    idempotency key must conflict."""
    return sha256_hex(canonical_json_bytes({
        "rec": recommendation_id,
        "v": recommendation_version,
        "digest": recommendation_digest,
        "decision": decision,
    }))


def is_stale(
    *, expected_version: int, current_version: int,
    expected_digest: str, current_digest: str,
) -> bool:
    """Approval targets a stale recommendation if the version or digest moved."""
    return expected_version != current_version or expected_digest != current_digest


def _sha(value: str) -> str:
    return sha256_hex(value.encode())

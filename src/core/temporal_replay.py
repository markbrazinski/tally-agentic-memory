"""Pure validation and projection for exact-seal CockroachDB replay.

The current and historical query rows cross an untrusted storage boundary.
This module validates their identities and canonical evidence bindings before
projecting the frozen ``GET /cases/{case_id}/replay`` response facts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from src.core.receipt import canonical_json_bytes, prefixed_sha256

HLC_LITERAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,18})\.[0-9]{10}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SEALED_CASE_STATES = frozenset({"FILED", "CONTESTED", "RESOLVED"})


class TemporalReplayValidationError(ValueError):
    """Replay rows cannot be safely interpreted as one sealed case."""


def canonical_hlc_literal(value: Any) -> str:
    """Return Cockroach's canonical decimal HLC form or reject it.

    CockroachDB does not support a bound placeholder in ``AS OF SYSTEM
    TIME``.  The platform layer may embed only a value accepted here: an
    unsigned wall-time integer and exactly ten logical-counter digits.  Signs,
    whitespace, exponent notation, quotes, and SQL punctuation are rejected.
    """
    text = value if isinstance(value, str) else str(value)
    if HLC_LITERAL_RE.fullmatch(text) is None:
        raise TemporalReplayValidationError("sealed_txn_ts is not a canonical HLC")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise TemporalReplayValidationError("sealed_txn_ts is not decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise TemporalReplayValidationError("sealed_txn_ts must be positive and finite")
    return text


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TemporalReplayValidationError(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise TemporalReplayValidationError(f"{field} must be a JSON object")
    return dict(value)


def _uuid(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TemporalReplayValidationError(f"{field} must be a UUID") from exc


def _required_text(value: Any, *, field: str) -> str:
    if value is None or not str(value).strip():
        raise TemporalReplayValidationError(f"{field} is required")
    return str(value)


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TemporalReplayValidationError(f"{field} must be decimal") from exc
    if not parsed.is_finite():
        raise TemporalReplayValidationError(f"{field} must be finite")
    return parsed


def _sha256(value: Any, *, field: str) -> str:
    text = _required_text(value, field=field).removeprefix("sha256:")
    if SHA256_RE.fullmatch(text) is None:
        raise TemporalReplayValidationError(f"{field} must be a SHA-256")
    return text


def _stored_hash_matches(stored: Any, computed_prefixed: str) -> bool:
    return isinstance(stored, str) and stored in {
        computed_prefixed,
        computed_prefixed.removeprefix("sha256:"),
    }


@dataclass(frozen=True)
class TemporalReplayView:
    tenant_id: str
    case_id: str
    state: str
    sealed_txn_ts: str
    tariff_rate: Decimal
    version_label: str
    stored_evidence_hash: str
    recomputed_evidence_hash: str
    sealed_rate: Decimal
    sealed_content_sha256: str
    clause_sha256: str
    source_sha256: str
    current_bindings_intact: bool


def validate_replay_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_tenant_id: str,
    expected_case_id: str,
    expected_hlc: str | None = None,
    historical: bool,
) -> TemporalReplayView:
    """Validate one current or exact-AOST case/evidence result set.

    Historical rows are authoritative only when every seal and evidence
    binding is intact and the case is ``FILED``.  Current rows may truthfully
    report a later mismatch through ``current_bindings_intact``; malformed or
    incomplete rows still fail closed.
    """
    if not rows:
        raise TemporalReplayValidationError("replay query returned no case")
    normalized = [dict(row) for row in rows]
    first = normalized[0]
    tenant_id = _uuid(first.get("tenant_id"), field="tenant_id")
    case_id = _uuid(first.get("case_id"), field="case_id")
    if tenant_id != _uuid(expected_tenant_id, field="expected_tenant_id"):
        raise TemporalReplayValidationError("returned tenant does not match trusted context")
    if case_id != _uuid(expected_case_id, field="expected_case_id"):
        raise TemporalReplayValidationError("returned case does not match request")

    state = _required_text(first.get("case_state"), field="case_state")
    if state not in SEALED_CASE_STATES:
        raise TemporalReplayValidationError("case has no filed seal")
    if historical and state != "FILED":
        raise TemporalReplayValidationError("seal-time case state is not FILED")
    sealed_txn_ts = canonical_hlc_literal(first.get("sealed_txn_ts"))
    if expected_hlc is not None and sealed_txn_ts != canonical_hlc_literal(expected_hlc):
        raise TemporalReplayValidationError("historical HLC does not match current seal anchor")
    if first.get("manifest_version") != 1:
        raise TemporalReplayValidationError("unsupported manifest version")

    manifest = _mapping(first.get("evidence_manifest"), field="evidence_manifest")
    if manifest.get("manifest_version") != 1:
        raise TemporalReplayValidationError("manifest version mismatch")
    if _uuid(manifest.get("tenant_id"), field="manifest tenant_id") != tenant_id:
        raise TemporalReplayValidationError("manifest tenant mismatch")
    if _uuid(manifest.get("case_id"), field="manifest case_id") != case_id:
        raise TemporalReplayValidationError("manifest case mismatch")
    recomputed_manifest_hash = prefixed_sha256(canonical_json_bytes(manifest))
    stored_evidence_hash = _required_text(
        first.get("evidence_hash"), field="evidence_hash"
    )

    manifest_items_value = manifest.get("evidence")
    if not isinstance(manifest_items_value, list) or not manifest_items_value:
        raise TemporalReplayValidationError("manifest has no evidence")
    manifest_items: dict[str, dict[str, Any]] = {}
    for value in manifest_items_value:
        item = _mapping(value, field="manifest evidence")
        evidence_id = _uuid(item.get("evidence_id"), field="manifest evidence_id")
        if evidence_id in manifest_items:
            raise TemporalReplayValidationError("manifest contains duplicate evidence IDs")
        manifest_items[evidence_id] = item

    returned_ids: set[str] = set()
    all_current_bindings_intact = stored_evidence_hash == recomputed_manifest_hash
    tariff_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in normalized:
        for field in (
            "tenant_id",
            "case_id",
            "case_state",
            "sealed_txn_ts",
            "evidence_hash",
            "evidence_manifest",
            "manifest_version",
        ):
            if canonical_json_bytes(row.get(field)) != canonical_json_bytes(first.get(field)):
                raise TemporalReplayValidationError(f"inconsistent case field: {field}")
        evidence_id = _uuid(row.get("evidence_id"), field="evidence_id")
        if evidence_id in returned_ids:
            raise TemporalReplayValidationError("duplicate returned evidence ID")
        returned_ids.add(evidence_id)
        item = manifest_items.get(evidence_id)
        if item is None:
            raise TemporalReplayValidationError("returned evidence is absent from manifest")
        content = _mapping(row.get("evidence_content"), field="evidence_content")
        computed_content_hash = prefixed_sha256(canonical_json_bytes(content))
        row_binding_intact = (
            row.get("evidence_sealed") is True
            and _stored_hash_matches(row.get("content_sha256"), computed_content_hash)
            and canonical_json_bytes(item.get("content")) == canonical_json_bytes(content)
            and str(item.get("content_sha256"))
            in {computed_content_hash, computed_content_hash.removeprefix("sha256:")}
            and str(item.get("kind")) == str(row.get("evidence_kind"))
            and str(item.get("source_table")) == str(row.get("source_table"))
            and str(item.get("source_id")) == str(row.get("source_id"))
        )
        all_current_bindings_intact = all_current_bindings_intact and row_binding_intact
        if row.get("source_table") == "tariff_clauses":
            tariff_candidates.append((row, content))

    if returned_ids != set(manifest_items):
        raise TemporalReplayValidationError("returned evidence set does not match manifest")
    if len(tariff_candidates) != 1:
        raise TemporalReplayValidationError("replay requires exactly one tariff evidence row")

    tariff_row, tariff_content = tariff_candidates[0]
    source_id = _uuid(tariff_row.get("source_id"), field="source_id")
    clause_id = _uuid(tariff_row.get("clause_id"), field="clause_id")
    snapshot_id = _uuid(tariff_row.get("snapshot_id"), field="snapshot_id")
    content_clause_id = _uuid(tariff_content.get("clause_id"), field="content clause_id")
    content_capture_id = _uuid(
        tariff_content.get("capture_id"), field="content capture_id"
    )
    source_binding_intact = (
        source_id == clause_id == content_clause_id and snapshot_id == content_capture_id
    )
    all_current_bindings_intact = all_current_bindings_intact and source_binding_intact

    tariff_rate = _decimal(tariff_row.get("tariff_rate"), field="tariff_rate")
    sealed_rate = _decimal(tariff_content.get("rate_amount"), field="sealed rate")
    version_label = _required_text(
        tariff_row.get("version_label"), field="version_label"
    )
    clause_sha256 = _sha256(tariff_row.get("clause_sha256"), field="clause_sha256")
    source_sha256 = _sha256(
        tariff_row.get("snapshot_source_sha256"), field="snapshot_source_sha256"
    )
    sealed_clause_sha256 = _sha256(
        tariff_content.get("clause_sha256"), field="sealed clause_sha256"
    )
    sealed_source_sha256 = _sha256(
        tariff_content.get("source_sha256"), field="sealed source_sha256"
    )
    all_current_bindings_intact = all_current_bindings_intact and all(
        (
            tariff_rate == sealed_rate,
            clause_sha256 == sealed_clause_sha256,
            source_sha256 == sealed_source_sha256,
        )
    )
    sealed_content_sha256 = prefixed_sha256(canonical_json_bytes(tariff_content))

    if historical and not all_current_bindings_intact:
        raise TemporalReplayValidationError("seal-time evidence bindings are invalid")
    return TemporalReplayView(
        tenant_id=tenant_id,
        case_id=case_id,
        state=state,
        sealed_txn_ts=sealed_txn_ts,
        tariff_rate=tariff_rate,
        version_label=version_label,
        stored_evidence_hash=stored_evidence_hash,
        recomputed_evidence_hash=recomputed_manifest_hash,
        sealed_rate=sealed_rate,
        sealed_content_sha256=sealed_content_sha256,
        clause_sha256=clause_sha256,
        source_sha256=source_sha256,
        current_bindings_intact=all_current_bindings_intact,
    )


def build_replay_response(
    *, historical: TemporalReplayView, current: TemporalReplayView, queries: list[str]
) -> dict[str, Any]:
    if historical.tenant_id != current.tenant_id or historical.case_id != current.case_id:
        raise TemporalReplayValidationError("current and historical case identity mismatch")
    if historical.sealed_txn_ts != current.sealed_txn_ts:
        raise TemporalReplayValidationError("current and historical seal HLC mismatch")
    match = bool(
        current.current_bindings_intact
        and historical.tariff_rate == current.tariff_rate == current.sealed_rate
        and historical.version_label == current.version_label
        and historical.stored_evidence_hash == current.recomputed_evidence_hash
        and historical.sealed_content_sha256 == current.sealed_content_sha256
        and historical.clause_sha256 == current.clause_sha256
        and historical.source_sha256 == current.source_sha256
    )
    return {
        "then": {
            "as_of": historical.sealed_txn_ts,
            "state": historical.state,
            "tariff_rate": float(historical.tariff_rate),
            "version_label": historical.version_label,
            "evidence_hash": historical.stored_evidence_hash,
            "source": "AS OF SYSTEM TIME",
        },
        "now": {
            "state": current.state,
            "tariff_rate": float(current.tariff_rate),
            "version_label": current.version_label,
            "evidence_hash_recomputed": current.recomputed_evidence_hash,
            "source": "current read",
        },
        "tamper_check": {"match": match},
        "sealed_copy": {
            "rate": float(current.sealed_rate),
            "content_sha256": current.sealed_content_sha256,
            "source": "case_evidence (sealed evidence copy)",
        },
        "retention": {
            "ttl_seconds": 7_776_000,
            "ttl_days": 90,
            "target_queryable": True,
            "language": (
                "Versioned S3 retains the dated source artifact. Within CockroachDB’s "
                "configured MVCC window, Tally can also replay the transactional case "
                "state at filing."
            ),
        },
        "queries": queries,
    }

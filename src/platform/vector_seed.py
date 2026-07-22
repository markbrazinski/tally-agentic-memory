"""Idempotently seed public synthetic Gate 2 clauses with exact-source bindings.

This platform module accepts already-fetched retained objects.  It never fetches
or uploads source objects, and it never logs bucket, key, or version values.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from src.core.receipt import TariffExtraction, verify_tariff_extraction
from src.external.titan_embeddings import embedding_sha256
from src.external.versioned_source import RetainedObject
from src.platform.vector_receipt_pipeline import compose_clause_embedding_input

VECTOR_DIMENSIONS = 1024


class SeedEvidenceConflictError(RuntimeError):
    """An idempotency key resolved to incompatible synthetic evidence."""


class SeedInputError(ValueError):
    """The public fixture or its exact retained binding is incomplete."""


@dataclass(frozen=True)
class SeedClauseSpec:
    capture_alias: str
    clause_ref: str
    document_family: str
    source_text: str
    clause_text: str
    effective_from: date
    effective_to: date | None
    equipment_type: str
    route_code: str
    service_context: str
    rate_amount: Decimal
    rate_currency: str
    rate_unit: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SeedClauseSpec":
        required = {
            "capture_alias",
            "clause_ref",
            "document_family",
            "source_text",
            "clause_text",
            "effective_from",
            "effective_to",
            "equipment_type",
            "route_code",
            "service_context",
            "rate_amount",
            "rate_currency",
            "rate_unit",
        }
        missing = sorted(required - set(value))
        if missing:
            raise SeedInputError(f"missing seed clause fields: {', '.join(missing)}")
        try:
            result = cls(
                capture_alias=str(value["capture_alias"]),
                clause_ref=str(value["clause_ref"]),
                document_family=str(value["document_family"]),
                source_text=str(value["source_text"]),
                clause_text=str(value["clause_text"]),
                effective_from=date.fromisoformat(str(value["effective_from"])),
                effective_to=(
                    date.fromisoformat(str(value["effective_to"]))
                    if value["effective_to"] is not None
                    else None
                ),
                equipment_type=str(value["equipment_type"]),
                route_code=str(value["route_code"]),
                service_context=str(value["service_context"]),
                rate_amount=Decimal(str(value["rate_amount"])),
                rate_currency=str(value["rate_currency"]),
                rate_unit=str(value["rate_unit"]),
            )
        except (InvalidOperation, ValueError) as exc:
            raise SeedInputError("invalid seed clause specification") from exc
        if not all(
            field.strip()
            for field in (
                result.capture_alias,
                result.clause_ref,
                result.document_family,
                result.source_text,
                result.clause_text,
                result.equipment_type,
                result.route_code,
                result.service_context,
                result.rate_currency,
                result.rate_unit,
            )
        ):
            raise SeedInputError("seed clause text and identity fields are required")
        if result.effective_to is not None and result.effective_to < result.effective_from:
            raise SeedInputError("seed clause effective interval is invalid")
        return result


@dataclass(frozen=True)
class EmbeddedClause:
    values: tuple[float, ...]
    input_sha256: str
    embedding_sha256: str


class TitanEmbedder(Protocol):
    def embed(self, text: str) -> EmbeddedClause: ...


class RetryDAL(Protocol):
    def run_with_retry(self, fn: Callable[[object], object]) -> object: ...


@dataclass(frozen=True)
class SeedResult:
    snapshot_ids_by_alias: dict[str, str]
    clause_ids_by_alias: dict[str, str]
    snapshots_inserted: int
    snapshots_reused: int
    clauses_inserted: int
    clauses_reused: int


@dataclass(frozen=True)
class _PreparedClause:
    spec: SeedClauseSpec
    retained: RetainedObject
    source_sha256: str
    clause_sha256: str
    embedding: EmbeddedClause
    vector_literal: str
    verification_status: str
    verification_reason: str


def _validate_vector(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) != VECTOR_DIMENSIONS:
        raise SeedInputError(f"Titan vector must contain {VECTOR_DIMENSIONS} dimensions")
    vector = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in vector):
        raise SeedInputError("Titan vector must contain finite values")
    return vector


def _vector_literal(values: Sequence[float]) -> str:
    return json.dumps(_validate_vector(values), separators=(",", ":"), allow_nan=False)


def _prepare(
    specs: Sequence[SeedClauseSpec],
    *,
    retained_by_alias: Mapping[str, RetainedObject],
    operator_version_ids: Mapping[str, str],
    embedder: TitanEmbedder,
) -> tuple[_PreparedClause, ...]:
    aliases = [spec.capture_alias for spec in specs]
    if len(aliases) != len(set(aliases)):
        raise SeedInputError("one seed clause is required per capture alias")
    prepared: list[_PreparedClause] = []
    for spec in specs:
        retained = retained_by_alias.get(spec.capture_alias)
        expected_version = operator_version_ids.get(spec.capture_alias)
        if (
            retained is None
            or not expected_version
            or retained.version_id != expected_version
        ):
            raise SeedInputError(
                "exact retained source version does not match operator binding"
            )
        if retained.body != spec.source_text.encode("utf-8"):
            raise SeedInputError(
                "retained source body does not exactly match public synthetic text"
            )
        embedding_text = compose_clause_embedding_input(
            document_family=spec.document_family,
            equipment=spec.equipment_type,
            route=spec.route_code,
            service=spec.service_context,
            clause_text=spec.clause_text,
        )
        embedding = embedder.embed(embedding_text)
        vector_literal = _vector_literal(embedding.values)
        if not all(
            isinstance(value, str) and len(value) == 64
            for value in (embedding.input_sha256, embedding.embedding_sha256)
        ):
            raise SeedInputError("Titan embedding provenance hashes are invalid")
        if embedding_sha256(embedding.values) != embedding.embedding_sha256:
            raise SeedInputError("Titan embedding values do not match embedding hash")
        verification = verify_tariff_extraction(
            retained.body,
            TariffExtraction(
                rate_amount=spec.rate_amount,
                rate_currency=spec.rate_currency,
                rate_unit=spec.rate_unit,
                rate_text=spec.clause_text,
                effective_from=spec.effective_from,
                effective_to=spec.effective_to,
                clause_text=spec.clause_text,
                source_locator=spec.clause_ref,
                confidence=Decimal("1.0000"),
            ),
        )
        prepared.append(
            _PreparedClause(
                spec=spec,
                retained=retained,
                source_sha256=hashlib.sha256(retained.body).hexdigest(),
                clause_sha256=hashlib.sha256(spec.clause_text.encode("utf-8")).hexdigest(),
                embedding=embedding,
                vector_literal=vector_literal,
                verification_status=("VERIFIED" if verification.eligible else "REJECTED"),
                verification_reason=verification.reason,
            )
        )
    return tuple(prepared)


def _assert_equal(label: str, actual: tuple[object, ...], expected: tuple[object, ...]) -> None:
    if actual != expected:
        mismatches = [
            str(position)
            for position, (actual_value, expected_value) in enumerate(
                zip(actual, expected, strict=False)
            )
            if actual_value != expected_value
        ]
        if len(actual) != len(expected):
            mismatches.append("length")
        raise SeedEvidenceConflictError(
            f"reused {label} conflicts with synthetic evidence at fields "
            f"{','.join(mismatches)}"
        )


def _assert_reused_vector(stored: object, expected_hash: str) -> None:
    if not isinstance(stored, str):
        raise SeedEvidenceConflictError("reused clause conflicts with synthetic evidence")
    try:
        restored = tuple(float(value) for value in json.loads(stored))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SeedEvidenceConflictError("reused clause conflicts with synthetic evidence") from exc
    try:
        stored_hash = embedding_sha256(restored)
    except ValueError as exc:
        raise SeedEvidenceConflictError("reused clause conflicts with synthetic evidence") from exc
    if stored_hash != expected_hash:
        raise SeedEvidenceConflictError("reused clause conflicts with synthetic evidence")


def seed_synthetic_clauses(
    dal: RetryDAL,
    *,
    specs: Sequence[SeedClauseSpec],
    retained_by_alias: Mapping[str, RetainedObject],
    operator_version_ids: Mapping[str, str],
    carrier_id: str,
    lane: str,
    embedding_model: str,
    embedder: TitanEmbedder,
) -> SeedResult:
    """Seed one exact retained public clause per capture alias transactionally."""
    if not carrier_id.strip() or not lane.strip() or not embedding_model.strip():
        raise SeedInputError("carrier_id, lane, and embedding_model are required")
    tenant = getattr(dal, "tenant", None)
    tenant_id = getattr(tenant, "tenant_id", None)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise SeedInputError("DAL must provide an authenticated tenant_id")
    prepared = _prepare(
        specs,
        retained_by_alias=retained_by_alias,
        operator_version_ids=operator_version_ids,
        embedder=embedder,
    )

    def _seed(conn: object) -> SeedResult:
        snapshot_ids: dict[str, str] = {}
        clause_ids: dict[str, str] = {}
        snapshots_inserted = snapshots_reused = clauses_inserted = clauses_reused = 0
        with conn.cursor() as cur:  # type: ignore[union-attr]
            for item in prepared:
                spec = item.spec
                retained = item.retained
                source_url = "retained-object://exact-version"
                cur.execute(
                    """
                    INSERT INTO tariff_snapshots
                        (tenant_id, carrier_id, lane, version_label, effective_date,
                         captured_at, source_url, s3_key, source_version_id,
                         source_byte_size, doc_sha256, doc_text, headline_rate)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, carrier_id, lane, captured_at) DO NOTHING
                    RETURNING id;
                    """,
                    (
                        tenant_id,
                        carrier_id,
                        lane,
                        spec.effective_from.isoformat(),
                        spec.effective_from,
                        retained.observed_at,
                        source_url,
                        retained.key,
                        retained.version_id,
                        retained.size,
                        item.source_sha256,
                        spec.source_text,
                        spec.rate_amount,
                    ),
                )
                snapshot_row = cur.fetchone()
                if snapshot_row is None:
                    cur.execute(
                        """
                        SELECT id, carrier_id, lane, version_label, effective_date, captured_at,
                               source_url, s3_key, source_version_id, source_byte_size,
                               doc_sha256, doc_text, headline_rate
                        FROM tariff_snapshots
                        WHERE tenant_id=%s AND carrier_id=%s AND lane=%s AND captured_at=%s;
                        """,
                        (tenant_id, carrier_id, lane, retained.observed_at),
                    )
                    snapshot_row = cur.fetchone()
                    if snapshot_row is None:
                        raise SeedEvidenceConflictError("missing reused synthetic snapshot")
                    _assert_equal(
                        "snapshot",
                        (str(snapshot_row[1]), *snapshot_row[2:]),
                        (
                            carrier_id,
                            lane,
                            spec.effective_from.isoformat(),
                            spec.effective_from,
                            retained.observed_at,
                            source_url,
                            retained.key,
                            retained.version_id,
                            retained.size,
                            item.source_sha256,
                            spec.source_text,
                            spec.rate_amount,
                        ),
                    )
                    snapshots_reused += 1
                else:
                    snapshots_inserted += 1
                snapshot_id = str(snapshot_row[0])
                snapshot_ids[spec.capture_alias] = snapshot_id
                cur.execute(
                    """
                    INSERT INTO tariff_clauses
                        (tenant_id, carrier_id, snapshot_id, clause_ref, clause_kind, clause_text,
                         rate_amount, rate_currency, rate_unit, free_time_basis, sha256,
                         effective_from, effective_to, source_locator, confidence,
                         verification_status, verification_reason, equipment_type, route_code,
                         service_context, document_family, embedding_model,
                         embedding_input_sha256, embedding_sha256, embedding)
                    VALUES (%s, %s, %s, %s, 'rate', %s, %s, %s, %s, NULL, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s::VECTOR)
                    ON CONFLICT (tenant_id, snapshot_id, sha256) DO NOTHING
                    RETURNING id;
                    """,
                    (
                        tenant_id,
                        carrier_id,
                        snapshot_id,
                        spec.clause_ref,
                        spec.clause_text,
                        spec.rate_amount,
                        spec.rate_currency,
                        spec.rate_unit,
                        item.clause_sha256,
                        spec.effective_from,
                        spec.effective_to,
                        spec.clause_ref,
                        Decimal("1.0000"),
                        item.verification_status,
                        item.verification_reason,
                        spec.equipment_type,
                        spec.route_code,
                        spec.service_context,
                        spec.document_family,
                        embedding_model,
                        item.embedding.input_sha256,
                        item.embedding.embedding_sha256,
                        item.vector_literal,
                    ),
                )
                clause_row = cur.fetchone()
                if clause_row is None:
                    cur.execute(
                        """
                        SELECT id, carrier_id, clause_ref, clause_text, rate_amount,
                               rate_currency, rate_unit, sha256, effective_from, effective_to,
                               source_locator, confidence, verification_status,
                               verification_reason, equipment_type, route_code, service_context,
                               document_family, embedding_model, embedding_input_sha256,
                               embedding_sha256, embedding::STRING
                        FROM tariff_clauses
                        WHERE tenant_id=%s AND snapshot_id=%s AND sha256=%s;
                        """,
                        (tenant_id, snapshot_id, item.clause_sha256),
                    )
                    clause_row = cur.fetchone()
                    if clause_row is None:
                        raise SeedEvidenceConflictError("missing reused synthetic clause")
                    _assert_equal(
                        "clause",
                        (str(clause_row[1]), *clause_row[2:-1]),
                        (
                            carrier_id,
                            spec.clause_ref,
                            spec.clause_text,
                            spec.rate_amount,
                            spec.rate_currency,
                            spec.rate_unit,
                            item.clause_sha256,
                            spec.effective_from,
                            spec.effective_to,
                            spec.clause_ref,
                            Decimal("1.0000"),
                            item.verification_status,
                            item.verification_reason,
                            spec.equipment_type,
                            spec.route_code,
                            spec.service_context,
                            spec.document_family,
                            embedding_model,
                            item.embedding.input_sha256,
                            item.embedding.embedding_sha256,
                        ),
                    )
                    _assert_reused_vector(clause_row[-1], item.embedding.embedding_sha256)
                    clauses_reused += 1
                else:
                    clauses_inserted += 1
                clause_ids[spec.capture_alias] = str(clause_row[0])
        return SeedResult(
            snapshot_ids_by_alias=snapshot_ids,
            clause_ids_by_alias=clause_ids,
            snapshots_inserted=snapshots_inserted,
            snapshots_reused=snapshots_reused,
            clauses_inserted=clauses_inserted,
            clauses_reused=clauses_reused,
        )

    return dal.run_with_retry(_seed)  # type: ignore[return-value]

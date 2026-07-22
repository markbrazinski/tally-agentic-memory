"""CockroachDB vector retrieval for Gate 2 tariff-clause candidates.

This adapter only ranks candidates. It searches one authenticated tenant and
carrier across retained tariff capture versions; snapshot, effective interval,
equipment, route, and service applicability remain deterministic post-filters.
No retrieval result is an authoritative tariff fact or a verdict.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

EMBEDDING_DIMENSION = 1024
MAX_RESULT_LIMIT = 25
_CANDIDATE_MULTIPLIER = 4
_VECTOR_INDEX_NAME = "tariff_clause_embedding_search_idx"


class VectorSearchError(ValueError):
    """Raised before a vector query with an unsafe or unsupported shape runs."""


class VectorSearchDAL(Protocol):
    """The narrow tenant-injecting DAL surface used by this read-only adapter."""

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] = (),
        *,
        tag: str,
        kind: str = "sql",
        render_source: str = "live",
    ) -> list[tuple[object, ...]]: ...


@dataclass(frozen=True)
class ClauseCarrierScope:
    """Carrier identity; DAL injects the authenticated tenant identity."""

    carrier_id: str

    def __post_init__(self) -> None:
        _require_identifier("carrier_id", self.carrier_id)


@dataclass(frozen=True)
class ClauseVectorHit:
    """A ranked candidate plus provenance needed for deterministic post-filtering."""

    clause_id: str
    snapshot_id: str
    carrier_id: str
    source_version_id: str | None
    source_sha256: str
    source_key: str
    clause_sha256: str
    snapshot_captured_at: datetime
    clause_ref: str
    clause_text: str
    rate_amount: Decimal | None
    rate_currency: str | None
    rate_unit: str | None
    source_locator: str | None
    confidence: Decimal | None
    effective_from: date | None
    effective_to: date | None
    equipment_type: str | None
    route_code: str | None
    service_context: str | None
    document_family: str | None
    verification_status: str
    embedding_model: str | None
    embedding_input_sha256: str | None
    embedding_sha256: str | None
    l2_distance: float
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class VectorIndexExplain:
    """Redacted plan proof for the exact runtime vector query."""

    plan_lines: tuple[str, ...]
    uses_named_vector_index: bool


class CockroachClauseVectorSearch:
    """Tenant-scoped, read-only C-SPANN retrieval over one carrier's clauses."""

    def __init__(self, dal: VectorSearchDAL):
        self._dal = dal

    def search(
        self,
        *,
        scope: ClauseCarrierScope,
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ClauseVectorHit]:
        """Return nearest verified candidates across a carrier's capture versions.

        The first CTE is the vector-index acceleration boundary. It has only
        the index's exact tenant/carrier prefix predicates and forces the named
        CockroachDB vector index, so small synthetic datasets still exercise
        the product index path. Verification and NULL-vector checks occur in
        the outer query, alongside the snapshot provenance join. Callers apply
        date/equipment/route/service policy to these candidates afterwards.
        """
        sql, params = _build_vector_search_query(scope, query_embedding, limit)
        rows = self._dal.execute(
            sql,
            params,
            tag="gate2.tariff_clause_vector_search",
            kind="vector_search",
        )
        return [_row_to_hit(row) for row in rows]

    def search_brute_force(
        self,
        *,
        scope: ClauseCarrierScope,
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ClauseVectorHit]:
        """Run the same ranking contract through the primary index as an exact oracle."""
        sql, params = _build_vector_search_query(
            scope, query_embedding, limit, index_name="primary"
        )
        rows = self._dal.execute(
            sql,
            params,
            tag="gate2.tariff_clause_vector_search.brute_force",
            kind="vector_search_brute_force",
        )
        return [_row_to_hit(row) for row in rows]

    def explain_index_use(
        self,
        *,
        scope: ClauseCarrierScope,
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> VectorIndexExplain:
        """Explain the exact search statement without exposing its bound values."""
        sql, params = _build_vector_search_query(scope, query_embedding, limit)
        rows = self._dal.execute(
            f"EXPLAIN {sql}",
            params,
            tag="gate2.tariff_clause_vector_search.explain",
            kind="vector_search_explain",
        )
        plan_lines: list[str] = []
        for row in rows:
            if len(row) != 1 or not isinstance(row[0], str):
                raise VectorSearchError("vector search explain returned an unexpected row shape")
            plan_lines.append(row[0])
        return VectorIndexExplain(
            plan_lines=tuple(plan_lines),
            uses_named_vector_index=any(_VECTOR_INDEX_NAME in line for line in plan_lines),
        )


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise VectorSearchError(f"{name} must be a non-empty identifier")


def _build_vector_search_query(
    scope: ClauseCarrierScope,
    query_embedding: Sequence[float],
    limit: int,
    *,
    index_name: str = _VECTOR_INDEX_NAME,
) -> tuple[str, tuple[object, ...]]:
    vector_literal = _vector_literal(query_embedding)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_RESULT_LIMIT
    ):
        raise VectorSearchError(f"limit must be an integer from 1 to {MAX_RESULT_LIMIT}")
    candidate_limit = limit * _CANDIDATE_MULTIPLIER
    if index_name not in {_VECTOR_INDEX_NAME, "primary"}:
        raise VectorSearchError("unsupported internal index selection")
    return (
        f"""
        WITH vector_candidates AS (
            SELECT c.tenant_id, c.id, c.snapshot_id, c.carrier_id, c.clause_ref,
                   c.clause_text, c.rate_amount, c.rate_currency, c.rate_unit,
                   c.source_locator, c.sha256, c.confidence, c.effective_from,
                   c.effective_to, c.equipment_type, c.route_code, c.service_context,
                   c.document_family, c.verification_status, c.embedding_model,
                   c.embedding_input_sha256, c.embedding_sha256, c.embedding
            FROM tariff_clauses@{index_name} AS c
            WHERE c.tenant_id=%s
              AND c.carrier_id=%s
            ORDER BY c.embedding <-> %s::VECTOR
            LIMIT %s
        )
        SELECT candidate.id::STRING, candidate.snapshot_id::STRING,
               candidate.carrier_id::STRING,
               snapshot.source_version_id, snapshot.doc_sha256, snapshot.s3_key,
               candidate.sha256, snapshot.captured_at, candidate.clause_ref,
               candidate.clause_text, candidate.rate_amount, candidate.rate_currency,
               candidate.rate_unit, candidate.source_locator, candidate.confidence,
               candidate.effective_from, candidate.effective_to, candidate.equipment_type,
               candidate.route_code, candidate.service_context, candidate.document_family,
               candidate.verification_status, candidate.embedding_model,
               candidate.embedding_input_sha256, candidate.embedding_sha256,
               candidate.embedding <-> %s::VECTOR AS l2_distance,
               candidate.embedding::STRING AS stored_embedding
        FROM vector_candidates AS candidate
        JOIN tariff_snapshots AS snapshot
          ON snapshot.tenant_id=candidate.tenant_id
         AND snapshot.id=candidate.snapshot_id
        WHERE candidate.verification_status='VERIFIED'
          AND candidate.embedding IS NOT NULL
        ORDER BY candidate.embedding <-> %s::VECTOR
        LIMIT %s;
        """,
        (
            scope.carrier_id,
            vector_literal,
            candidate_limit,
            vector_literal,
            vector_literal,
            limit,
        ),
    )


def _vector_literal(query_embedding: Sequence[float]) -> str:
    if isinstance(query_embedding, (str, bytes)) or len(query_embedding) != EMBEDDING_DIMENSION:
        raise VectorSearchError(f"query_embedding must contain {EMBEDDING_DIMENSION} finite values")
    values: list[float] = []
    for value in query_embedding:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise VectorSearchError(
                f"query_embedding must contain {EMBEDDING_DIMENSION} finite values"
            )
        values.append(float(value))
    return json.dumps(values, separators=(",", ":"), allow_nan=False)


def _stored_embedding(value: object) -> tuple[float, ...]:
    """Parse CockroachDB's ``VECTOR::STRING`` output without accepting bad data."""
    if not isinstance(value, str):
        raise VectorSearchError("vector search returned a non-string stored embedding")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise VectorSearchError("vector search returned an invalid stored embedding") from exc
    if not isinstance(parsed, list) or len(parsed) != EMBEDDING_DIMENSION:
        raise VectorSearchError(
            f"stored embedding must contain {EMBEDDING_DIMENSION} finite values"
        )
    embedding: list[float] = []
    for item in parsed:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            raise VectorSearchError(
                f"stored embedding must contain {EMBEDDING_DIMENSION} finite values"
            )
        embedding.append(float(item))
    return tuple(embedding)


def _row_to_hit(row: tuple[object, ...]) -> ClauseVectorHit:
    if len(row) != 27:
        raise VectorSearchError("vector search returned an unexpected row shape")
    (
        clause_id,
        snapshot_id,
        carrier_id,
        source_version_id,
        source_sha256,
        source_key,
        clause_sha256,
        snapshot_captured_at,
        clause_ref,
        clause_text,
        rate_amount,
        rate_currency,
        rate_unit,
        source_locator,
        confidence,
        effective_from,
        effective_to,
        equipment_type,
        route_code,
        service_context,
        document_family,
        verification_status,
        embedding_model,
        embedding_input_sha256,
        embedding_sha256,
        l2_distance,
        stored_embedding,
    ) = row
    required_strings = (
        clause_id,
        snapshot_id,
        carrier_id,
        source_sha256,
        source_key,
        clause_sha256,
        clause_ref,
        clause_text,
        verification_status,
    )
    if not all(isinstance(value, str) and value for value in required_strings):
        raise VectorSearchError("vector search returned an incomplete clause identity")
    if source_version_id is not None and not isinstance(source_version_id, str):
        raise VectorSearchError("vector search returned an invalid source_version_id")
    if not isinstance(snapshot_captured_at, datetime):
        raise VectorSearchError("vector search returned an invalid snapshot capture time")
    if rate_amount is not None and not isinstance(rate_amount, Decimal):
        raise VectorSearchError("vector search returned a non-decimal rate_amount")
    if confidence is not None and not isinstance(confidence, Decimal):
        raise VectorSearchError("vector search returned a non-decimal confidence")
    if effective_from is not None and not isinstance(effective_from, date):
        raise VectorSearchError("vector search returned an invalid effective_from")
    if effective_to is not None and not isinstance(effective_to, date):
        raise VectorSearchError("vector search returned an invalid effective_to")
    optional_strings = (
        rate_currency,
        rate_unit,
        source_locator,
        equipment_type,
        route_code,
        service_context,
        document_family,
        embedding_model,
        embedding_input_sha256,
        embedding_sha256,
    )
    if not all(value is None or isinstance(value, str) for value in optional_strings):
        raise VectorSearchError("vector search returned invalid optional clause fields")
    if isinstance(l2_distance, bool) or not isinstance(l2_distance, (int, float)):
        raise VectorSearchError("vector search returned an invalid L2 distance")
    if not math.isfinite(float(l2_distance)):
        raise VectorSearchError("vector search returned an invalid L2 distance")
    embedding = _stored_embedding(stored_embedding)
    return ClauseVectorHit(
        clause_id=clause_id,
        snapshot_id=snapshot_id,
        carrier_id=carrier_id,
        source_version_id=source_version_id,
        source_sha256=source_sha256,
        source_key=source_key,
        clause_sha256=clause_sha256,
        snapshot_captured_at=snapshot_captured_at,
        clause_ref=clause_ref,
        clause_text=clause_text,
        rate_amount=rate_amount,
        rate_currency=rate_currency,
        rate_unit=rate_unit,
        source_locator=source_locator,
        confidence=confidence,
        effective_from=effective_from,
        effective_to=effective_to,
        equipment_type=equipment_type,
        route_code=route_code,
        service_context=service_context,
        document_family=document_family,
        verification_status=verification_status,
        embedding_model=embedding_model,
        embedding_input_sha256=embedding_input_sha256,
        embedding_sha256=embedding_sha256,
        l2_distance=float(l2_distance),
        embedding=embedding,
    )

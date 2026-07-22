"""Pure Gate 2 contract for deterministic, evidence-bound clause retrieval.

The vector distance is only a ranking hint.  A candidate is usable only after
tenant/carrier/document-family scope, charge date, equipment, route, service,
and exact retained-source postfilters pass.  No function performs database,
object-store, or embedding-provider I/O.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

TEMPORAL_APPLIES = "applies"
TEMPORAL_NOT_YET_EFFECTIVE = "not_yet_effective"
TEMPORAL_EXPIRED = "expired"

EXACT_SOURCE_VERIFIED = "verified"
EXACT_SOURCE_MISSING = "missing"
EXACT_SOURCE_NOT_UTF8 = "not_utf8"
EXACT_SOURCE_HASH_MISMATCH = "hash_mismatch"
EXACT_SOURCE_CLAUSE_HASH_MISMATCH = "clause_hash_mismatch"
EXACT_SOURCE_CLAUSE_ABSENT = "clause_absent"
EXACT_SOURCE_EFFECTIVE_DATE_ABSENT = "effective_date_absent"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_vector(value: Sequence[object]) -> tuple[float, ...] | None:
    """Return a finite unit vector, or ``None`` for a corrupt embedding."""
    try:
        vector = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    squared_length = sum(part * part for part in vector)
    if (
        not vector
        or not all(math.isfinite(part) for part in vector)
        or math.isclose(squared_length, 0.0)
    ):
        return None
    length = math.sqrt(squared_length)
    return tuple(part / length for part in vector)


def normalized_l2_distance(left: Sequence[object], right: Sequence[object]) -> float | None:
    """Live-index-compatible normalized-vector L2 distance; lower is nearer."""
    left_vector = _normalized_vector(left)
    right_vector = _normalized_vector(right)
    if left_vector is None or right_vector is None or len(left_vector) != len(right_vector):
        return None
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left_vector, right_vector, strict=True)))


@dataclass(frozen=True)
class ClauseDocument:
    """A clause, its business context, and its immutable retained-source identity."""

    clause_id: str
    tenant_id: str
    carrier_id: str
    document_family: str
    capture_id: str
    source_version_id: str
    source_sha256: str
    clause_sha256: str
    effective_from: date
    effective_to: date | None
    equipment: str
    route: str
    service: str
    rate_amount: Decimal
    rate_currency: str
    rate_unit: str
    clause_text: str
    embedding: tuple[object, ...]
    embedding_integrity_verified: bool
    source_bytes: bytes | None = None

    def __post_init__(self) -> None:
        required = (
            self.clause_id,
            self.tenant_id,
            self.carrier_id,
            self.document_family,
            self.capture_id,
            self.source_version_id,
            self.source_sha256,
            self.clause_sha256,
            self.equipment,
            self.route,
            self.service,
            self.rate_currency,
            self.rate_unit,
            self.clause_text,
        )
        if not all(value.strip() for value in required):
            raise ValueError("clause identity, context, and source fields are required")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if self.rate_amount < 0:
            raise ValueError("rate_amount must not be negative")
        if not isinstance(self.embedding_integrity_verified, bool):
            raise ValueError("embedding_integrity_verified must be a boolean")


@dataclass(frozen=True)
class RetrievalRequest:
    """The required scope and context for one charge-date clause lookup."""

    tenant_id: str
    carrier_id: str
    document_family: str
    charge_date: date
    equipment: str
    route: str
    service: str
    query_embedding: tuple[object, ...]

    @property
    def has_scope_prefix(self) -> bool:
        return all(
            value.strip() for value in (self.tenant_id, self.carrier_id, self.document_family)
        )

    @property
    def has_charge_context(self) -> bool:
        return all(value.strip() for value in (self.equipment, self.route, self.service))


@dataclass(frozen=True)
class CandidateEvaluation:
    """An auditable ranked candidate and every deterministic postfilter result."""

    clause_id: str
    tenant_id: str
    carrier_id: str
    document_family: str
    capture_id: str
    source_version_id: str
    equipment: str
    route: str
    service: str
    rate_amount: Decimal
    rate_currency: str
    rate_unit: str
    embedding_integrity_verified: bool
    normalized_l2_distance: float | None
    temporal_status: str
    exact_source_status: str
    selected: bool
    rejection_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "clause_id": self.clause_id,
            "tenant_id": self.tenant_id,
            "carrier_id": self.carrier_id,
            "document_family": self.document_family,
            "capture_id": self.capture_id,
            "source_version_id": self.source_version_id,
            "equipment": self.equipment,
            "route": self.route,
            "service": self.service,
            "rate_amount": format(self.rate_amount, ".2f"),
            "rate_currency": self.rate_currency,
            "rate_unit": self.rate_unit,
            "embedding_integrity_verified": self.embedding_integrity_verified,
            "normalized_l2_distance": self.normalized_l2_distance,
            "temporal_status": self.temporal_status,
            "exact_source_status": self.exact_source_status,
            "selected": self.selected,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[CandidateEvaluation, ...]
    selected: CandidateEvaluation | None
    abstention_reasons: tuple[str, ...]

    @property
    def abstained(self) -> bool:
        return self.selected is None


@dataclass(frozen=True)
class EvaluationQuery:
    """Committed fixture expectation, not a production-quality metric claim."""

    query_id: str
    request: RetrievalRequest
    expected_clause_id: str | None


@dataclass(frozen=True)
class EvaluationSummary:
    """Small synthetic-fixture counts; do not interpret them as production accuracy."""

    query_count: int
    raw_top1_expected_count: int
    raw_top_k_expected_count: int
    selection_expected_count: int
    expected_abstention_count: int


def temporal_status(document: ClauseDocument, charge_date: date) -> str:
    if charge_date < document.effective_from:
        return TEMPORAL_NOT_YET_EFFECTIVE
    if document.effective_to is not None and charge_date > document.effective_to:
        return TEMPORAL_EXPIRED
    return TEMPORAL_APPLIES


def exact_source_status(document: ClauseDocument) -> str:
    """Verify the clause against the supplied retained bytes and stated digest."""
    if document.source_bytes is None:
        return EXACT_SOURCE_MISSING
    if hashlib.sha256(document.clause_text.encode("utf-8")).hexdigest() != document.clause_sha256:
        return EXACT_SOURCE_CLAUSE_HASH_MISMATCH
    if hashlib.sha256(document.source_bytes).hexdigest() != document.source_sha256:
        return EXACT_SOURCE_HASH_MISMATCH
    try:
        source_text = document.source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return EXACT_SOURCE_NOT_UTF8
    if _normalize_text(document.clause_text) not in _normalize_text(source_text):
        return EXACT_SOURCE_CLAUSE_ABSENT
    required_dates = [document.effective_from.isoformat()]
    if document.effective_to is not None:
        required_dates.append(document.effective_to.isoformat())
    if any(required_date not in source_text for required_date in required_dates):
        return EXACT_SOURCE_EFFECTIVE_DATE_ABSENT
    return EXACT_SOURCE_VERIFIED


def _evaluate(document: ClauseDocument, request: RetrievalRequest) -> CandidateEvaluation:
    distance = normalized_l2_distance(request.query_embedding, document.embedding)
    temporal = temporal_status(document, request.charge_date)
    source = exact_source_status(document)
    reasons: list[str] = []
    if distance is None:
        reasons.append("invalid_embedding")
    if not document.embedding_integrity_verified:
        reasons.append("embedding_hash_mismatch")
    if document.equipment != request.equipment:
        reasons.append("equipment_mismatch")
    if document.route != request.route:
        reasons.append("route_mismatch")
    if document.service != request.service:
        reasons.append("service_mismatch")
    if temporal != TEMPORAL_APPLIES:
        reasons.append(f"temporal_{temporal}")
    if source != EXACT_SOURCE_VERIFIED:
        reasons.append(f"exact_source_{source}")
    return CandidateEvaluation(
        clause_id=document.clause_id,
        tenant_id=document.tenant_id,
        carrier_id=document.carrier_id,
        document_family=document.document_family,
        capture_id=document.capture_id,
        source_version_id=document.source_version_id,
        equipment=document.equipment,
        route=document.route,
        service=document.service,
        rate_amount=document.rate_amount,
        rate_currency=document.rate_currency,
        rate_unit=document.rate_unit,
        embedding_integrity_verified=document.embedding_integrity_verified,
        normalized_l2_distance=distance,
        temporal_status=temporal,
        exact_source_status=source,
        selected=not reasons,
        rejection_reasons=tuple(reasons),
    )


def _ranked_evaluations(
    documents: Iterable[ClauseDocument], request: RetrievalRequest
) -> tuple[CandidateEvaluation, ...]:
    """Apply scope before ranking, then use stable normalized-L2 ordering."""
    in_scope = (
        document
        for document in documents
        if document.tenant_id == request.tenant_id
        and document.carrier_id == request.carrier_id
        and document.document_family == request.document_family
    )
    evaluations = [_evaluate(document, request) for document in in_scope]
    return tuple(
        sorted(
            evaluations,
            key=lambda candidate: (
                candidate.normalized_l2_distance is None,
                (
                    candidate.normalized_l2_distance
                    if candidate.normalized_l2_distance is not None
                    else math.inf
                ),
                candidate.clause_id,
                candidate.capture_id,
                candidate.source_version_id,
            ),
        )
    )


def _result_from(documents: Iterable[ClauseDocument], request: RetrievalRequest) -> RetrievalResult:
    if not request.has_scope_prefix:
        return RetrievalResult((), None, ("missing_tenant_carrier_or_document_family_scope",))
    if not request.has_charge_context:
        return RetrievalResult((), None, ("missing_charge_context",))
    candidates = _ranked_evaluations(documents, request)
    selected = next((candidate for candidate in candidates if candidate.selected), None)
    if selected is not None:
        reasons: tuple[str, ...] = ()
    elif not candidates:
        reasons = ("no_scoped_candidates",)
    else:
        reasons = ("no_candidate_passed_postfilters",)
    return RetrievalResult(candidates=candidates, selected=selected, abstention_reasons=reasons)


def retrieve_brute_force(
    documents: Iterable[ClauseDocument], request: RetrievalRequest
) -> RetrievalResult:
    """Reference implementation for checking an index's fixed-fixture behavior."""
    return _result_from(documents, request)


def evaluate_fixture(
    index: "ClauseIndex", queries: Iterable[EvaluationQuery], *, top_k: int
) -> EvaluationSummary:
    """Evaluate committed fixture expectations without asserting production accuracy."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    query_count = raw_top1_count = raw_top_k_count = selection_count = abstention_count = 0
    for query in queries:
        query_count += 1
        result = index.retrieve(query.request)
        selected_id = result.selected.clause_id if result.selected else None
        if query.expected_clause_id is None:
            abstention_count += int(result.abstained)
            continue
        selection_count += int(selected_id == query.expected_clause_id)
        raw_top1_count += int(
            bool(result.candidates) and result.candidates[0].clause_id == query.expected_clause_id
        )
        raw_top_k_count += int(
            query.expected_clause_id
            in {candidate.clause_id for candidate in result.candidates[:top_k]}
        )
    return EvaluationSummary(
        query_count,
        raw_top1_count,
        raw_top_k_count,
        selection_count,
        abstention_count,
    )


@dataclass(frozen=True)
class ClauseIndex:
    """Small immutable scope partition used as the pure indexed contract."""

    by_scope: dict[tuple[str, str, str], tuple[ClauseDocument, ...]]

    @classmethod
    def build(cls, documents: Iterable[ClauseDocument]) -> "ClauseIndex":
        grouped: dict[tuple[str, str, str], list[ClauseDocument]] = {}
        for document in documents:
            key = (document.tenant_id, document.carrier_id, document.document_family)
            grouped.setdefault(key, []).append(document)
        return cls({scope: tuple(values) for scope, values in grouped.items()})

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        if not request.has_scope_prefix:
            return _result_from((), request)
        scope = (request.tenant_id, request.carrier_id, request.document_family)
        return _result_from(self.by_scope.get(scope, ()), request)

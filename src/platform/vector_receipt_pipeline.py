"""Gate 2 read-only tariff retrieval bound to exact retained source versions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Protocol

from src.core.receipt import TariffExtraction, TariffVerification, verify_tariff_extraction
from src.core.vector_retrieval import (
    ClauseDocument,
    RetrievalRequest,
    RetrievalResult,
    retrieve_brute_force,
)
from src.external.gate2_vector_search import (
    ClauseCarrierScope,
    ClauseVectorHit,
    CockroachClauseVectorSearch,
)
from src.external.titan_embeddings import (
    MODEL_ID,
    TitanEmbedding,
    TitanTextEmbeddingsV2,
    embedding_input_sha256,
    embedding_sha256,
)
from src.external.versioned_source import RetainedObject


class TenantDAL(Protocol):
    tenant: object


class Embeddings(Protocol):
    def embed(self, text: str) -> TitanEmbedding: ...


class ClauseSearch(Protocol):
    def search(
        self, *, scope: ClauseCarrierScope, query_embedding: tuple[float, ...], limit: int
    ) -> list[ClauseVectorHit]: ...


class VersionedSource(Protocol):
    def get_exact(self, *, bucket: str, key: str, version_id: str) -> RetainedObject: ...


def _required(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    return value


def compose_clause_embedding_input(
    *, document_family: str, equipment: str, route: str, service: str, clause_text: str
) -> str:
    """Produce the stable clause text representation used for seeding and lookup."""
    return "\n".join(
        (
            f"document_family: {_required('document_family', document_family)}",
            f"equipment: {_required('equipment', equipment)}",
            f"route: {_required('route', route)}",
            f"service: {_required('service', service)}",
            f"tariff_clause: {_required('clause_text', clause_text)}",
        )
    )


def compose_query_embedding_input(
    *, document_family: str, equipment: str, route: str, service: str, invoice_context: str
) -> str:
    """Produce the stable query representation with the same context labels as clauses."""
    return "\n".join(
        (
            f"document_family: {_required('document_family', document_family)}",
            f"equipment: {_required('equipment', equipment)}",
            f"route: {_required('route', route)}",
            f"service: {_required('service', service)}",
            f"tariff_clause: {_required('invoice_context', invoice_context)}",
        )
    )


@dataclass(frozen=True)
class VectorReceiptRequest:
    carrier_id: str
    document_family: str
    bucket: str
    charge_date: date
    equipment: str
    route: str
    service: str
    invoice_context: str
    top_k: int = 5

    def validate(self, tenant_id: str) -> None:
        for name, value in (
            ("tenant_id", tenant_id),
            ("carrier_id", self.carrier_id),
            ("document_family", self.document_family),
            ("bucket", self.bucket),
            ("equipment", self.equipment),
            ("route", self.route),
            ("service", self.service),
            ("invoice_context", self.invoice_context),
        ):
            _required(name, value)
        if not isinstance(self.charge_date, date):
            raise ValueError("charge_date is required")
        if not isinstance(self.top_k, int) or isinstance(self.top_k, bool) or self.top_k < 1:
            raise ValueError("top_k must be positive")


@dataclass(frozen=True)
class VectorReceiptResult:
    """Public evaluations plus private evidence objects for the selected clause only."""

    retrieval: RetrievalResult
    selected_hit: ClauseVectorHit | None = None
    selected_source: RetainedObject | None = None
    extraction: TariffExtraction | None = None
    verification: TariffVerification | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.retrieval.candidates],
            "selected_clause_id": (
                self.retrieval.selected.clause_id if self.retrieval.selected else None
            ),
            "abstention_reasons": list(self.retrieval.abstention_reasons),
        }


class VectorReceiptPipeline:
    """Read-only candidate retrieval; it never files findings or writes evidence."""

    def __init__(
        self,
        dal: TenantDAL,
        *,
        embeddings: Embeddings | None = None,
        search: ClauseSearch | None = None,
        source: VersionedSource,
    ) -> None:
        self._dal = dal
        self._embeddings = embeddings or TitanTextEmbeddingsV2()
        self._search = search or CockroachClauseVectorSearch(dal)  # type: ignore[arg-type]
        self._source = source

    def retrieve(self, request: VectorReceiptRequest) -> VectorReceiptResult:
        tenant_id = getattr(getattr(self._dal, "tenant", None), "tenant_id", None)
        request.validate(tenant_id if isinstance(tenant_id, str) else "")
        query_input = compose_query_embedding_input(
            document_family=request.document_family,
            equipment=request.equipment,
            route=request.route,
            service=request.service,
            invoice_context=request.invoice_context,
        )
        query_embedding = self._embeddings.embed(query_input).values
        retrieval_request = RetrievalRequest(
            tenant_id=tenant_id,
            carrier_id=request.carrier_id,
            document_family=request.document_family,
            charge_date=request.charge_date,
            equipment=request.equipment,
            route=request.route,
            service=request.service,
            query_embedding=query_embedding,
        )
        hits = self._search.search(
            scope=ClauseCarrierScope(carrier_id=request.carrier_id),
            query_embedding=query_embedding,
            limit=request.top_k,
        )
        documents: list[ClauseDocument] = []
        hit_by_clause: dict[str, ClauseVectorHit] = {}
        source_by_clause: dict[str, RetainedObject] = {}
        for hit in hits:
            retained = self._load_exact(request.bucket, hit)
            if retained is not None:
                source_by_clause[hit.clause_id] = retained
            documents.append(self._document(tenant_id, hit, retained))
            hit_by_clause[hit.clause_id] = hit
        retrieval = retrieve_brute_force(documents, retrieval_request)
        selected = retrieval.selected
        if selected is None:
            return VectorReceiptResult(retrieval)
        hit = hit_by_clause[selected.clause_id]
        source = source_by_clause.get(selected.clause_id)
        if source is None:
            return VectorReceiptResult(
                self._abstain_selected(retrieval, "exact_source_unavailable")
            )
        extraction = self._extraction(hit)
        verification = verify_tariff_extraction(source.body, extraction)
        if (
            not verification.eligible
            or verification.source_sha256 != hit.source_sha256
            or verification.clause_sha256 != hit.clause_sha256
        ):
            return VectorReceiptResult(
                self._abstain_selected(retrieval, "tariff_verification_failed")
            )
        return VectorReceiptResult(retrieval, hit, source, extraction, verification)

    def _load_exact(
        self, bucket: str, hit: ClauseVectorHit
    ) -> RetainedObject | None:
        if not hit.source_version_id:
            return None
        try:
            retained = self._source.get_exact(
                bucket=bucket, key=hit.source_key, version_id=hit.source_version_id
            )
            if (
                retained.bucket != bucket
                or retained.key != hit.source_key
                or retained.version_id != hit.source_version_id
            ):
                return None
            return retained
        except Exception:  # Exact version/read failure must cause retrieval abstention.
            return None

    @staticmethod
    def _document(
        tenant_id: str, hit: ClauseVectorHit, source: RetainedObject | None
    ) -> ClauseDocument:
        try:
            expected_input_hash = embedding_input_sha256(
                compose_clause_embedding_input(
                    document_family=hit.document_family or "",
                    equipment=hit.equipment_type or "",
                    route=hit.route_code or "",
                    service=hit.service_context or "",
                    clause_text=hit.clause_text,
                )
            )
            integrity_verified = (
                VectorReceiptPipeline._metadata_complete(hit)
                and hit.embedding_model == MODEL_ID
                and hit.embedding_input_sha256 == expected_input_hash
                and hit.embedding_sha256 is not None
                and embedding_sha256(hit.embedding) == hit.embedding_sha256
            )
        except ValueError:
            integrity_verified = False
        return ClauseDocument(
            clause_id=hit.clause_id,
            tenant_id=tenant_id,
            carrier_id=hit.carrier_id,
            document_family=hit.document_family or "missing-document-family",
            capture_id=hit.snapshot_id,
            source_version_id=hit.source_version_id or "missing-source-version",
            source_sha256=hit.source_sha256,
            clause_sha256=hit.clause_sha256,
            effective_from=hit.effective_from or date.max,
            effective_to=hit.effective_to,
            equipment=hit.equipment_type or "missing-equipment",
            route=hit.route_code or "missing-route",
            service=hit.service_context or "missing-service",
            rate_amount=hit.rate_amount or Decimal("0"),
            rate_currency=hit.rate_currency or "missing-currency",
            rate_unit=hit.rate_unit or "missing-unit",
            clause_text=hit.clause_text,
            embedding=hit.embedding,
            embedding_integrity_verified=integrity_verified,
            source_bytes=source.body if source else None,
        )

    @staticmethod
    def _metadata_complete(hit: ClauseVectorHit) -> bool:
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                hit.source_version_id,
                hit.document_family,
                hit.equipment_type,
                hit.route_code,
                hit.service_context,
                hit.rate_currency,
                hit.rate_unit,
                hit.source_locator,
                hit.embedding_model,
                hit.embedding_input_sha256,
                hit.embedding_sha256,
            )
        ) and all(
            isinstance(value, Decimal)
            for value in (hit.rate_amount, hit.confidence)
        ) and hit.effective_from is not None

    @staticmethod
    def _extraction(hit: ClauseVectorHit) -> TariffExtraction:
        if not VectorReceiptPipeline._metadata_complete(hit):
            raise ValueError("selected vector clause metadata is incomplete")
        assert hit.rate_amount is not None
        assert hit.confidence is not None
        assert hit.effective_from is not None
        return TariffExtraction(
            rate_amount=hit.rate_amount,
            rate_currency=hit.rate_currency or "",
            rate_unit=hit.rate_unit or "",
            rate_text=hit.clause_text,
            effective_from=hit.effective_from,
            effective_to=hit.effective_to,
            clause_text=hit.clause_text,
            source_locator=hit.source_locator or hit.clause_ref,
            confidence=hit.confidence,
        )

    @staticmethod
    def _abstain_selected(retrieval: RetrievalResult, reason: str) -> RetrievalResult:
        selected = retrieval.selected
        if selected is None:
            return retrieval
        rejected = replace(
            selected,
            selected=False,
            rejection_reasons=(*selected.rejection_reasons, reason),
        )
        candidates = tuple(rejected if item == selected else item for item in retrieval.candidates)
        return RetrievalResult(candidates, None, (reason,))

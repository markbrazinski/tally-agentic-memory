"""Product-path Gate 2 retrieval tests using only public-safe fakes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.external.gate2_vector_search import ClauseVectorHit
from src.external.titan_embeddings import (
    DIMENSIONS,
    MODEL_ID,
    TitanEmbedding,
    embedding_input_sha256,
    embedding_sha256,
)
from src.external.versioned_source import RetainedObject
from src.platform.vector_receipt_pipeline import (
    VectorReceiptPipeline,
    VectorReceiptRequest,
    compose_clause_embedding_input,
    compose_query_embedding_input,
)

TENANT = "tenant-synthetic"
CARRIER = "carrier-synthetic"
BUCKET = "synthetic-bucket"


def _vector() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (DIMENSIONS - 1)


class FakeEmbeddings:
    def __init__(self):
        self.inputs: list[str] = []

    def embed(self, text: str) -> TitanEmbedding:
        self.inputs.append(text)
        vector = _vector()
        return TitanEmbedding(vector, "input-hash", embedding_sha256(vector))


class FakeSearch:
    def __init__(self, hits: list[ClauseVectorHit]):
        self.hits = hits
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


class FakeSource:
    def __init__(self, objects: dict[tuple[str, str], RetainedObject]):
        self.objects = objects
        self.calls: list[tuple[str, str, str]] = []

    def get_exact(self, *, bucket: str, key: str, version_id: str) -> RetainedObject:
        self.calls.append((bucket, key, version_id))
        return self.objects[(key, version_id)]


def _clause(amount: str, effective_from: date) -> str:
    return (
        f"CHASSIS HARBOR-ELM import detention rate is ${amount}/day. "
        f"Effective {effective_from.isoformat()}."
    )


def _hit(*, clause_id: str, amount: str, effective_from: date, version: str) -> ClauseVectorHit:
    text = _clause(amount, effective_from)
    vector = _vector()
    return ClauseVectorHit(
        clause_id=clause_id,
        snapshot_id=f"snapshot-{clause_id}",
        carrier_id=CARRIER,
        source_version_id=version,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_key=f"synthetic/{clause_id}.txt",
        clause_sha256=hashlib.sha256(text.encode()).hexdigest(),
        snapshot_captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        clause_ref="item-1",
        clause_text=text,
        rate_amount=Decimal(amount),
        rate_currency="USD",
        rate_unit="per_day",
        source_locator="item-1",
        confidence=Decimal("0.99"),
        effective_from=effective_from,
        effective_to=None,
        equipment_type="CHASSIS",
        route_code="HARBOR-ELM",
        service_context="import",
        document_family="detention-tariff",
        verification_status="VERIFIED",
        embedding_model=MODEL_ID,
        embedding_input_sha256=embedding_input_sha256(
            compose_clause_embedding_input(
                document_family="detention-tariff",
                equipment="CHASSIS",
                route="HARBOR-ELM",
                service="import",
                clause_text=text,
            )
        ),
        embedding_sha256=embedding_sha256(vector),
        l2_distance=0.0,
        embedding=vector,
    )


def _request(**changes) -> VectorReceiptRequest:
    values = dict(
        carrier_id=CARRIER,
        document_family="detention-tariff",
        bucket=BUCKET,
        charge_date=date(2026, 3, 15),
        equipment="CHASSIS",
        route="HARBOR-ELM",
        service="import",
        invoice_context="Invoice claims detention at $350/day.",
        top_k=5,
    )
    values.update(changes)
    return VectorReceiptRequest(**values)


def _pipeline(hits: list[ClauseVectorHit], source: FakeSource, embeddings=None):
    return VectorReceiptPipeline(
        SimpleNamespace(tenant=SimpleNamespace(tenant_id=TENANT)),
        embeddings=embeddings or FakeEmbeddings(),
        search=FakeSearch(hits),
        source=source,
    )


def _objects(hits: list[ClauseVectorHit]) -> FakeSource:
    return FakeSource({
        (hit.source_key, hit.source_version_id): RetainedObject(
            bucket=BUCKET,
            key=hit.source_key,
            version_id=hit.source_version_id or "",
            body=hit.clause_text.encode(),
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for hit in hits
        if hit.source_version_id
    })


def test_product_path_selects_250_rejects_later_350_and_reopens_exact_versions():
    early = _hit(
        clause_id="clause-250", amount="250", effective_from=date(2026, 1, 1), version="v1"
    )
    later = _hit(
        clause_id="clause-350", amount="350", effective_from=date(2026, 4, 1), version="v2"
    )
    source = _objects([early, later])
    result = _pipeline([early, later], source).retrieve(_request())

    assert result.retrieval.selected is not None
    assert result.retrieval.selected.clause_id == "clause-250"
    assert "temporal_not_yet_effective" in result.as_dict()["candidates"][1]["rejection_reasons"]
    assert source.calls == [
        (BUCKET, early.source_key, "v1"),
        (BUCKET, later.source_key, "v2"),
    ]
    assert result.verification is not None and result.verification.eligible


def test_embedding_composition_uses_matching_context_labels_for_seed_and_query():
    clause = compose_clause_embedding_input(
        document_family="tariff",
        equipment="CHASSIS",
        route="HARBOR-ELM",
        service="import",
        clause_text="rate",
    )
    query = compose_query_embedding_input(
        document_family="tariff",
        equipment="CHASSIS",
        route="HARBOR-ELM",
        service="import",
        invoice_context="rate",
    )
    assert clause == query


def test_missing_tenant_stops_before_embedding_search_or_source_io():
    embeddings = FakeEmbeddings()
    search = FakeSearch([])
    source = FakeSource({})
    pipeline = VectorReceiptPipeline(
        SimpleNamespace(tenant=SimpleNamespace(tenant_id="")),
        embeddings=embeddings,
        search=search,
        source=source,
    )

    with pytest.raises(ValueError, match="tenant_id"):
        pipeline.retrieve(_request())
    assert embeddings.inputs == []
    assert search.calls == []
    assert source.calls == []


@pytest.mark.parametrize("fault", ["source", "hash", "version", "embedding", "model", "input"])
def test_source_hash_version_or_embedding_integrity_failure_abstains(fault: str):
    hit = _hit(
        clause_id="clause-250", amount="250", effective_from=date(2026, 1, 1), version="v1"
    )
    source = _objects([hit])
    if fault == "source":
        source.objects.clear()
    elif fault == "hash":
        source.objects[(hit.source_key, "v1")] = RetainedObject(
            BUCKET, hit.source_key, "v1", b"changed", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
    elif fault == "version":
        hit = replace(hit, source_version_id=None)
    elif fault == "embedding":
        hit = replace(hit, embedding_sha256="0" * 64)
    elif fault == "model":
        hit = replace(hit, embedding_model="wrong-model")
    else:
        hit = replace(hit, embedding_input_sha256="0" * 64)

    result = _pipeline([hit], source).retrieve(_request())
    assert result.retrieval.abstained


def test_wrong_returned_source_identity_abstains():
    hit = _hit(
        clause_id="clause-250", amount="250", effective_from=date(2026, 1, 1), version="v1"
    )
    source = _objects([hit])
    source.objects[(hit.source_key, "v1")] = RetainedObject(
        bucket=BUCKET,
        key="synthetic/wrong-key.txt",
        version_id="v1",
        body=hit.clause_text.encode(),
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = _pipeline([hit], source).retrieve(_request())

    assert result.retrieval.abstained


def test_no_valid_clause_abstains():
    result = _pipeline([], FakeSource({})).retrieve(_request())

    assert result.retrieval.abstained
    assert result.as_dict()["selected_clause_id"] is None

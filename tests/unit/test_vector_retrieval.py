"""Gate 2 retrieval contracts using public-safe fictional Northstar clauses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.core.vector_retrieval import (
    EXACT_SOURCE_CLAUSE_ABSENT,
    EXACT_SOURCE_CLAUSE_HASH_MISMATCH,
    EXACT_SOURCE_EFFECTIVE_DATE_ABSENT,
    EXACT_SOURCE_HASH_MISMATCH,
    TEMPORAL_NOT_YET_EFFECTIVE,
    ClauseDocument,
    ClauseIndex,
    EvaluationQuery,
    RetrievalRequest,
    evaluate_fixture,
    retrieve_brute_force,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "gate2" / "synthetic_clauses.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _documents() -> tuple[ClauseDocument, ...]:
    values = _fixture()["documents"]
    assert isinstance(values, list)
    return tuple(
        ClauseDocument(
            clause_id=value["clause_id"],
            tenant_id=value["tenant_id"],
            carrier_id=value["carrier_id"],
            document_family=value["document_family"],
            capture_id=value["capture_id"],
            source_version_id=value["source_version_id"],
            source_sha256=hashlib.sha256(value["source_text"].encode()).hexdigest(),
            clause_sha256=hashlib.sha256(value["clause_text"].encode()).hexdigest(),
            effective_from=date.fromisoformat(value["effective_from"]),
            effective_to=(
                date.fromisoformat(value["effective_to"]) if value["effective_to"] else None
            ),
            equipment=value["equipment"],
            route=value["route"],
            service=value["service"],
            rate_amount=Decimal(value["rate_amount"]),
            rate_currency=value["rate_currency"],
            rate_unit=value["rate_unit"],
            clause_text=value["clause_text"],
            embedding=tuple(value["embedding"]),
            embedding_integrity_verified=True,
            source_bytes=value["source_text"].encode(),
        )
        for value in values
    )


def _request(**changes: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-northstar-demo",
        "carrier_id": "northstar-ocean-lines",
        "document_family": "detention-tariff",
        "charge_date": date(2026, 3, 15),
        "equipment": "CHASSIS",
        "route": "HARBOR-ELM",
        "service": "DETENTION",
        "query_embedding": (1.0, 0.0, 0.0),
    }
    values.update(changes)
    return RetrievalRequest(**values)  # type: ignore[arg-type]


def _evaluation_queries() -> tuple[EvaluationQuery, ...]:
    values = _fixture()["evaluation_queries"]
    assert isinstance(values, list)
    return tuple(
        EvaluationQuery(
            query_id=value["query_id"],
            request=RetrievalRequest(
                tenant_id=value["tenant_id"],
                carrier_id=value["carrier_id"],
                document_family=value["document_family"],
                charge_date=date.fromisoformat(value["charge_date"]),
                equipment=value["equipment"],
                route=value["route"],
                service=value["service"],
                query_embedding=tuple(value["query_embedding"]),
            ),
            expected_clause_id=value["expected_clause_id"],
        )
        for value in values
    )


def test_earlier_charge_selects_northstar_250_and_rejects_later_350_version():
    result = ClauseIndex.build(_documents()).retrieve(_request())
    by_clause = {candidate.clause_id: candidate for candidate in result.candidates}

    assert result.selected is not None
    assert result.selected.clause_id == "clause-northstar-250"
    assert result.selected.capture_id == "capture-northstar-2026-01"
    assert result.selected.source_version_id == "synthetic-northstar-v1"
    assert result.selected.rate_amount == Decimal("250.00")
    assert result.selected.service == "DETENTION"
    assert result.selected.carrier_id == "northstar-ocean-lines"
    assert result.selected.normalized_l2_distance == 0.0
    assert result.selected.as_dict() == {
        "clause_id": "clause-northstar-250",
        "tenant_id": "tenant-northstar-demo",
        "carrier_id": "northstar-ocean-lines",
        "document_family": "detention-tariff",
        "capture_id": "capture-northstar-2026-01",
        "source_version_id": "synthetic-northstar-v1",
        "equipment": "CHASSIS",
        "route": "HARBOR-ELM",
        "service": "DETENTION",
        "rate_amount": "250.00",
        "rate_currency": "USD",
        "rate_unit": "per_day",
        "embedding_integrity_verified": True,
        "normalized_l2_distance": 0.0,
        "temporal_status": "applies",
        "exact_source_status": "verified",
        "selected": True,
        "rejection_reasons": [],
    }
    assert by_clause["clause-northstar-350"].temporal_status == TEMPORAL_NOT_YET_EFFECTIVE
    assert by_clause["clause-northstar-350"].selected is False
    assert "temporal_not_yet_effective" in by_clause["clause-northstar-350"].rejection_reasons


def test_wrong_equipment_route_and_semantically_similar_service_do_not_win():
    result = ClauseIndex.build(_documents()).retrieve(_request())
    by_clause = {candidate.clause_id: candidate for candidate in result.candidates}

    assert (
        "equipment_mismatch"
        in by_clause["clause-northstar-wrong-equipment"].rejection_reasons
    )
    assert "route_mismatch" in by_clause["clause-northstar-wrong-route"].rejection_reasons
    assert (
        "service_mismatch"
        in by_clause["clause-northstar-similar-inapplicable"].rejection_reasons
    )
    assert (
        "service_mismatch"
        in by_clause["clause-northstar-irrelevant-language"].rejection_reasons
    )


def test_tenant_isolation_excludes_second_tenant_from_candidates():
    result = ClauseIndex.build(_documents()).retrieve(_request())

    assert all(
        candidate.clause_id != "clause-seaside-tenant-isolation"
        for candidate in result.candidates
    )


def test_missing_tenant_or_carrier_scope_abstains_before_any_candidate_is_ranked():
    index = ClauseIndex.build(_documents())

    for changes in ({"tenant_id": ""}, {"carrier_id": ""}):
        result = index.retrieve(_request(**changes))
        assert result.abstained
        assert result.candidates == ()
        assert result.abstention_reasons == ("missing_tenant_carrier_or_document_family_scope",)


def test_removing_valid_250_clause_causes_earlier_charge_abstention():
    documents = tuple(
        document for document in _documents() if document.clause_id != "clause-northstar-250"
    )
    result = ClauseIndex.build(documents).retrieve(_request())

    assert result.abstained
    assert result.abstention_reasons == ("no_candidate_passed_postfilters",)
    assert all(candidate.selected is False for candidate in result.candidates)


def test_missing_clause_in_exact_retained_source_abstains():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    missing_source = b"Synthetic source without the proposed clause."
    missing_clause = replace(
        valid,
        source_bytes=missing_source,
        source_sha256=hashlib.sha256(missing_source).hexdigest(),
    )
    result = ClauseIndex.build((missing_clause,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].exact_source_status == EXACT_SOURCE_CLAUSE_ABSENT
    assert "exact_source_clause_absent" in result.candidates[0].rejection_reasons


def test_changed_retained_bytes_fail_the_exact_hash_filter():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    changed_bytes = valid.source_bytes + b" altered" if valid.source_bytes else b"altered"
    changed = replace(valid, source_bytes=changed_bytes)
    result = ClauseIndex.build((changed,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].exact_source_status == EXACT_SOURCE_HASH_MISMATCH
    assert "exact_source_hash_mismatch" in result.candidates[0].rejection_reasons


def test_retained_source_must_explicitly_state_the_effective_interval_dates():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    date_free_source = valid.source_bytes.replace(b"2026-06-30", b"date-removed")
    date_free = replace(
        valid,
        source_bytes=date_free_source,
        source_sha256=hashlib.sha256(date_free_source).hexdigest(),
    )
    result = ClauseIndex.build((date_free,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].exact_source_status == EXACT_SOURCE_EFFECTIVE_DATE_ABSENT
    assert "exact_source_effective_date_absent" in result.candidates[0].rejection_reasons


def test_tampered_clause_hash_fails_before_text_membership_can_be_trusted():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    tampered = replace(valid, clause_sha256="0" * 64)
    result = ClauseIndex.build((tampered,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].exact_source_status == EXACT_SOURCE_CLAUSE_HASH_MISMATCH
    assert "exact_source_clause_hash_mismatch" in result.candidates[0].rejection_reasons


def test_corrupted_embedding_is_reported_and_cannot_select_a_clause():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    corrupted = replace(valid, embedding=("not-a-number", 0.0, 0.0))
    result = ClauseIndex.build((corrupted,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].normalized_l2_distance is None
    assert "invalid_embedding" in result.candidates[0].rejection_reasons


def test_changed_but_numerically_valid_embedding_requires_integrity_verification():
    valid = next(
        document for document in _documents() if document.clause_id == "clause-northstar-250"
    )
    changed_embedding = replace(
        valid,
        embedding=(0.8, 0.6, 0.0),
        embedding_integrity_verified=False,
    )
    result = ClauseIndex.build((changed_embedding,)).retrieve(_request())

    assert result.abstained
    assert result.candidates[0].normalized_l2_distance is not None
    assert "embedding_hash_mismatch" in result.candidates[0].rejection_reasons


def test_indexed_and_brute_force_retrieval_agree():
    documents = _documents()
    request = _request()

    assert ClauseIndex.build(documents).retrieve(request) == retrieve_brute_force(
        documents, request
    )


def test_committed_synthetic_evaluation_counts_top1_topk_and_expected_abstentions():
    summary = evaluate_fixture(ClauseIndex.build(_documents()), _evaluation_queries(), top_k=3)

    assert summary.query_count == 4
    assert summary.raw_top1_expected_count == 1
    assert summary.raw_top_k_expected_count == 2
    assert summary.selection_expected_count == 2
    assert summary.expected_abstention_count == 2

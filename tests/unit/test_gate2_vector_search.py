"""Focused Gate 2 tests: prefixed vector ranking remains deterministic input only."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.external.gate2_vector_search import (
    EMBEDDING_DIMENSION,
    MAX_RESULT_LIMIT,
    ClauseCarrierScope,
    CockroachClauseVectorSearch,
    VectorSearchError,
)

CARRIER_ID = "20000000-0000-4000-8000-000000000002"
SNAPSHOT_ID = "30000000-0000-4000-8000-000000000002"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class FakeDAL:
    def __init__(self, rows: list[tuple[object, ...]] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[object, ...], str, str]] = []

    def execute(self, sql, params=(), *, tag, kind="sql", render_source="live"):
        self.calls.append((sql, params, tag, kind))
        return self.rows


def _synthetic_row(*, stored_embedding: str | None = None) -> tuple[object, ...]:
    return (
        "40000000-0000-4000-8000-000000000002",
        SNAPSHOT_ID,
        CARRIER_ID,
        "synthetic-version-id",
        "synthetic-source-sha256",
        "synthetic/tariffs/versioned.pdf",
        "synthetic-clause-sha256",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        "item-7",
        "Synthetic retained tariff text",
        Decimal("250.00"),
        "USD",
        "per_day",
        "page 2",
        Decimal("0.9900"),
        date(2026, 1, 1),
        None,
        "40HC",
        "USLAX-USNYC",
        "import",
        "carrier-tariff",
        "VERIFIED",
        "synthetic-embedding-model",
        "synthetic-embedding-input-sha256",
        "synthetic-embedding-sha256",
        0.125,
        stored_embedding or json.dumps([0.0] * EMBEDDING_DIMENSION),
    )


def test_gate2_migration_uses_real_vector_column_prefixed_index_and_provenance_columns():
    sql = (MIGRATIONS_DIR / "005_gate2_clause_vector_search.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS embedding VECTOR(1024)" in sql
    assert "CREATE VECTOR INDEX IF NOT EXISTS tariff_clause_embedding_search_idx" in sql
    assert "(tenant_id, carrier_id, embedding vector_l2_ops)" in sql
    assert "SET sql_safe_updates = false" in sql
    for column in (
        "equipment_type STRING",
        "route_code STRING",
        "service_context STRING",
        "document_family STRING",
        "embedding_model STRING",
        "embedding_input_sha256 STRING",
        "embedding_sha256 STRING",
    ):
        assert column in sql


def test_search_binds_vector_uses_named_index_and_keeps_only_exact_prefix_at_boundary():
    dal = FakeDAL()
    search = CockroachClauseVectorSearch(dal)

    assert search.search(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    ) == []

    sql, params, tag, kind = dal.calls[0]
    vector_cte = sql.split("SELECT candidate.id", maxsplit=1)[0]
    assert "FROM tariff_clauses@tariff_clause_embedding_search_idx AS c" in vector_cte
    assert "WHERE c.tenant_id=%s" in vector_cte
    assert "AND c.carrier_id=%s" in vector_cte
    assert "verification_status='VERIFIED'" not in vector_cte
    assert "embedding IS NOT NULL" not in vector_cte
    assert "ORDER BY c.embedding <-> %s::VECTOR" in vector_cte
    assert "WHERE candidate.verification_status='VERIFIED'" in sql
    assert "AND candidate.embedding IS NOT NULL" in sql
    assert "JOIN tariff_snapshots AS snapshot" in sql
    assert "candidate.id::STRING" in sql
    assert "candidate.snapshot_id::STRING" in sql
    assert "candidate.carrier_id::STRING" in sql
    assert "ORDER BY candidate.embedding <-> %s::VECTOR" in sql
    assert params[0] == CARRIER_ID
    assert params[1].startswith("[")
    assert params[2] == 20  # default result limit multiplied before post-filtering
    assert params[3] == params[1] == params[4]
    assert params[5] == 5
    assert tag == "gate2.tariff_clause_vector_search"
    assert kind == "vector_search"


def test_search_returns_candidate_provenance_and_l2_distance_without_a_verdict():
    dal = FakeDAL(rows=[_synthetic_row()])
    search = CockroachClauseVectorSearch(dal)

    hits = search.search(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    )

    assert len(hits) == 1
    hit = hits[0]
    assert hit.clause_id == "40000000-0000-4000-8000-000000000002"
    assert hit.snapshot_id == SNAPSHOT_ID
    assert hit.source_version_id == "synthetic-version-id"
    assert hit.source_sha256 == "synthetic-source-sha256"
    assert hit.source_key == "synthetic/tariffs/versioned.pdf"
    assert hit.clause_sha256 == "synthetic-clause-sha256"
    assert hit.confidence == Decimal("0.9900")
    assert hit.effective_from == date(2026, 1, 1)
    assert hit.equipment_type == "40HC"
    assert hit.route_code == "USLAX-USNYC"
    assert hit.service_context == "import"
    assert hit.document_family == "carrier-tariff"
    assert hit.verification_status == "VERIFIED"
    assert hit.embedding_model == "synthetic-embedding-model"
    assert hit.embedding_input_sha256 == "synthetic-embedding-input-sha256"
    assert hit.embedding_sha256 == "synthetic-embedding-sha256"
    assert hit.l2_distance == 0.125
    assert hit.embedding == (0.0,) * EMBEDDING_DIMENSION
    assert not hasattr(hit, "verdict")


def test_brute_force_oracle_uses_primary_index_with_the_same_bound_contract():
    dal = FakeDAL(rows=[_synthetic_row()])

    hits = CockroachClauseVectorSearch(dal).search_brute_force(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    )

    sql, params, tag, kind = dal.calls[0]
    assert len(hits) == 1
    assert "FROM tariff_clauses@primary AS c" in sql
    assert "tariff_clause_embedding_search_idx" not in sql
    assert params[0] == CARRIER_ID
    assert tag == "gate2.tariff_clause_vector_search.brute_force"
    assert kind == "vector_search_brute_force"


@pytest.mark.parametrize(
    ("embedding", "limit"),
    [
        ([0.0] * 3, 5),
        ([float("nan")] * EMBEDDING_DIMENSION, 5),
        ([0.0] * EMBEDDING_DIMENSION, 0),
        ([0.0] * EMBEDDING_DIMENSION, MAX_RESULT_LIMIT + 1),
    ],
)
def test_search_rejects_invalid_query_fences_before_database_access(embedding, limit):
    dal = FakeDAL()
    search = CockroachClauseVectorSearch(dal)

    with pytest.raises(VectorSearchError):
        search.search(
            scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
            query_embedding=embedding,
            limit=limit,
        )

    assert dal.calls == []


@pytest.mark.parametrize("carrier_id", ["", "\x00"])
def test_carrier_scope_rejects_missing_or_unsafe_prefix(carrier_id):
    with pytest.raises(VectorSearchError):
        ClauseCarrierScope(carrier_id=carrier_id)


def test_search_fails_closed_on_malformed_database_row():
    search = CockroachClauseVectorSearch(FakeDAL(rows=[("not-a-complete-hit",)]))

    with pytest.raises(VectorSearchError, match="unexpected row shape"):
        search.search(
            scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
            query_embedding=[0.0] * EMBEDDING_DIMENSION,
        )


@pytest.mark.parametrize(
    "stored_embedding",
    [json.dumps([0.0] * 3), json.dumps([float("nan")] * EMBEDDING_DIMENSION)],
)
def test_search_rejects_corrupt_stored_embedding_before_returning_a_hit(stored_embedding):
    row = _synthetic_row(stored_embedding=stored_embedding)
    search = CockroachClauseVectorSearch(FakeDAL(rows=[row]))

    with pytest.raises(VectorSearchError, match="stored embedding"):
        search.search(
            scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
            query_embedding=[0.0] * EMBEDDING_DIMENSION,
        )


def test_explain_uses_byte_identical_runtime_query_body_and_detects_named_vector_index():
    runtime_dal = FakeDAL()
    CockroachClauseVectorSearch(runtime_dal).search(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    )
    runtime_sql, runtime_params, _, _ = runtime_dal.calls[0]

    explain_dal = FakeDAL(rows=[("scan tariff_clause_embedding_search_idx",)])
    proof = CockroachClauseVectorSearch(explain_dal).explain_index_use(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    )

    explain_sql, explain_params, tag, kind = explain_dal.calls[0]
    assert explain_sql == f"EXPLAIN {runtime_sql}"
    assert explain_params == runtime_params
    assert proof.plan_lines == ("scan tariff_clause_embedding_search_idx",)
    assert proof.uses_named_vector_index is True
    assert tag == "gate2.tariff_clause_vector_search.explain"
    assert kind == "vector_search_explain"


def test_explain_reports_false_when_named_vector_index_is_absent():
    dal = FakeDAL(rows=[("scan tariff_clauses primary",)])

    proof = CockroachClauseVectorSearch(dal).explain_index_use(
        scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
        query_embedding=[0.0] * EMBEDDING_DIMENSION,
    )

    assert proof.uses_named_vector_index is False


def test_explain_fails_closed_on_malformed_plan_row():
    dal = FakeDAL(rows=[("one", "two")])

    with pytest.raises(VectorSearchError, match="explain returned an unexpected row shape"):
        CockroachClauseVectorSearch(dal).explain_index_use(
            scope=ClauseCarrierScope(carrier_id=CARRIER_ID),
            query_embedding=[0.0] * EMBEDDING_DIMENSION,
        )

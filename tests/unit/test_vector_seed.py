"""Unit tests for transactional, public-safe Gate 2 vector seeding."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.external.titan_embeddings import embedding_sha256
from src.external.versioned_source import RetainedObject
from src.platform.vector_seed import (
    VECTOR_DIMENSIONS,
    EmbeddedClause,
    SeedClauseSpec,
    SeedEvidenceConflictError,
    SeedInputError,
    seed_synthetic_clauses,
)

TENANT_ID = "tenant-synthetic-northstar"
CARRIER_ID = "carrier-synthetic-northstar"
LANE = "HARBOR-ELM"
CAPTURE_ALIAS = "northstar-2026-01"
VERSION_ID = "synthetic-version-northstar-01"
SOURCE_TEXT = (
    "Synthetic Northstar contract. Effective from 2026-01-01 through 2026-06-30. "
    "DETENTION: USD 250 per day for CHASSIS on HARBOR-ELM."
)
CLAUSE_TEXT = "DETENTION: USD 250 per day for CHASSIS on HARBOR-ELM."


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params):
        normalized = " ".join(sql.split())
        self.state["statements"].append((normalized, params))
        if normalized.startswith("INSERT INTO tariff_snapshots"):
            key = (params[0], params[1], params[2], params[5])
            existing = self.state["snapshots"].get(key)
            if existing is not None:
                self.row = None
                return
            snapshot_id = f"snapshot-{len(self.state['snapshots']) + 1}"
            self.state["snapshots"][key] = (snapshot_id, *params[1:])
            self.row = (snapshot_id,)
            return
        if normalized.startswith("SELECT id, carrier_id, lane, version_label"):
            key = (params[0], params[1], params[2], params[3])
            self.row = self.state["snapshots"].get(key)
            return
        if normalized.startswith("INSERT INTO tariff_clauses"):
            key = (params[0], params[2], params[8])
            existing = self.state["clauses"].get(key)
            if existing is not None:
                self.row = None
                return
            clause_id = f"clause-{len(self.state['clauses']) + 1}"
            # Mirrors the SELECT projection below, excluding its returned id.
            self.state["clauses"][key] = (
                clause_id,
                params[1],
                params[3],
                params[4],
                params[5],
                params[6],
                params[7],
                params[8],
                params[9],
                params[10],
                params[11],
                params[12],
                params[13],
                params[14],
                params[15],
                params[16],
                params[17],
                params[18],
                params[19],
                params[20],
                params[21],
                params[22],
            )
            self.row = (clause_id,)
            return
        if normalized.startswith("SELECT id, carrier_id, clause_ref"):
            self.row = self.state["clauses"].get((params[0], params[1], params[2]))
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return FakeCursor(self.state)


class FakeDAL:
    def __init__(self):
        self.tenant = SimpleNamespace(tenant_id=TENANT_ID)
        self.state = {"snapshots": {}, "clauses": {}, "statements": []}
        self.retry_calls = 0

    def run_with_retry(self, fn):
        self.retry_calls += 1
        before = deepcopy(self.state)
        try:
            return fn(FakeConnection(self.state))
        except Exception:
            self.state = before
            raise


class FakeTitan:
    def __init__(self):
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        values = (1.0,) + (0.0,) * (VECTOR_DIMENSIONS - 1)
        return EmbeddedClause(
            values=values,
            input_sha256="a" * 64,
            embedding_sha256=embedding_sha256(values),
        )


def _spec(**changes):
    values = {
        "capture_alias": CAPTURE_ALIAS,
        "clause_ref": "northstar-item-1",
        "document_family": "detention-contract",
        "source_text": SOURCE_TEXT,
        "clause_text": CLAUSE_TEXT,
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2026, 6, 30),
        "equipment_type": "CHASSIS",
        "route_code": LANE,
        "service_context": "DETENTION",
        "rate_amount": Decimal("250.00"),
        "rate_currency": "USD",
        "rate_unit": "per_day",
    }
    values.update(changes)
    return SeedClauseSpec(**values)


def _retained(body=SOURCE_TEXT.encode()):
    return RetainedObject(
        bucket="fixture-bucket-not-returned",
        key="synthetic/northstar-v1.txt",
        version_id=VERSION_ID,
        body=body,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _seed(dal, titan, spec=None, retained=None):
    return seed_synthetic_clauses(
        dal,
        specs=(_spec() if spec is None else spec,),
        retained_by_alias={CAPTURE_ALIAS: _retained() if retained is None else retained},
        operator_version_ids={CAPTURE_ALIAS: VERSION_ID},
        carrier_id=CARRIER_ID,
        lane=LANE,
        embedding_model="amazon.titan-embed-text-v2:0",
        embedder=titan,
    )


def test_seed_inserts_then_reuses_exact_evidence_idempotently():
    dal = FakeDAL()
    titan = FakeTitan()

    first = _seed(dal, titan)
    second = _seed(dal, titan)

    assert first.snapshot_ids_by_alias == {CAPTURE_ALIAS: "snapshot-1"}
    assert first.clause_ids_by_alias == {CAPTURE_ALIAS: "clause-1"}
    assert (first.snapshots_inserted, first.clauses_inserted) == (1, 1)
    assert (second.snapshots_reused, second.clauses_reused) == (1, 1)
    assert (second.snapshots_inserted, second.clauses_inserted) == (0, 0)
    assert dal.retry_calls == 2


def test_conflicting_reused_snapshot_fails_and_rolls_back_transaction_state():
    dal = FakeDAL()
    titan = FakeTitan()
    _seed(dal, titan)

    with pytest.raises(SeedEvidenceConflictError, match="snapshot"):
        _seed(dal, titan, spec=replace(_spec(), rate_amount=Decimal("275.00")))

    assert len(dal.state["snapshots"]) == 1
    assert len(dal.state["clauses"]) == 1


def test_seed_rejects_non_exact_retained_body_before_titan_or_transaction():
    dal = FakeDAL()
    titan = FakeTitan()

    with pytest.raises(SeedInputError, match="exactly match"):
        _seed(dal, titan, retained=_retained(SOURCE_TEXT.encode() + b" changed"))

    assert titan.calls == []
    assert dal.retry_calls == 0


def test_seed_persists_real_vector_cast_and_all_embedding_metadata_without_network():
    dal = FakeDAL()
    titan = FakeTitan()
    result = _seed(dal, titan)
    clause_insert = next(
        statement
        for statement in dal.state["statements"]
        if statement[0].startswith("INSERT INTO tariff_clauses")
    )
    sql, params = clause_insert
    snapshot_insert = next(
        statement
        for statement in dal.state["statements"]
        if statement[0].startswith("INSERT INTO tariff_snapshots")
    )

    assert result.clauses_inserted == 1
    assert titan.calls == [
        "document_family: detention-contract\nequipment: CHASSIS\nroute: HARBOR-ELM\n"
        "service: DETENTION\ntariff_clause: " + CLAUSE_TEXT
    ]
    assert "%s::VECTOR" in sql
    assert params[11] == "northstar-item-1"
    assert params[13] == "VERIFIED"
    assert params[14] == "verified"
    assert params[19] == "amazon.titan-embed-text-v2:0"
    assert params[20] == "a" * 64
    assert params[21] == embedding_sha256((1.0,) + (0.0,) * (VECTOR_DIMENSIONS - 1))
    assert params[22].startswith("[1.0,0.0")
    assert snapshot_insert[1][6] == "retained-object://exact-version"
    assert snapshot_insert[1][3] == "2026-01-01"
    assert "fixture-bucket-not-returned" not in repr(result)
    assert "synthetic/northstar-v1.txt" not in repr(result)


def test_reuse_accepts_cockroach_vector_formatting_but_rejects_changed_vector_hash():
    dal = FakeDAL()
    titan = FakeTitan()
    _seed(dal, titan)
    key, row = next(iter(dal.state["clauses"].items()))
    # Cockroach can render floats without the JSON writer's trailing .0.
    cockroach_formatted = "[1," + ",".join("0" for _ in range(VECTOR_DIMENSIONS - 1)) + "]"
    dal.state["clauses"][key] = (*row[:-1], cockroach_formatted)

    reused = _seed(dal, titan)

    assert reused.clauses_reused == 1
    changed_vector = "[0,1," + ",".join("0" for _ in range(VECTOR_DIMENSIONS - 2)) + "]"
    dal.state["clauses"][key] = (*row[:-1], changed_vector)
    with pytest.raises(SeedEvidenceConflictError, match="clause"):
        _seed(dal, titan)


def test_spec_mapping_validates_public_fixture_shape():
    spec = _spec()
    rebuilt = SeedClauseSpec.from_mapping(
        {
            **spec.__dict__,
            "effective_from": spec.effective_from.isoformat(),
            "effective_to": spec.effective_to.isoformat(),
            "rate_amount": str(spec.rate_amount),
        }
    )

    assert rebuilt == spec

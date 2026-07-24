"""Gate 3 live trace against tally_gate2_iso — REAL Distributed Vector Indexing.

Seeds a hero tariff clause ($250/calendar-day, effective 2026-06-01) and a
wrong-date distractor ($250 but effective only from 2026-07-01) into
tariff_clauses with REAL Amazon Titan embeddings, then runs the real CockroachDB
VECTOR index search (tariff_clause_embedding_search_idx) via the vector adapter,
maps hits to candidates, and runs the deterministic applicability firewall.

Proves: the hero query selects the named vector index; the wrong-date distractor
may retrieve semantically but is REJECTED deterministically (REJECTED_WRONG_DATE);
the accepted rule is the correctly-dated clause. Full sponsor path: Titan embed +
CockroachDB vector index + deterministic validation.

Writes only to a Gate-3 tenant in tally_gate2_iso. Never touches defaultdb.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from decimal import Decimal
from uuid import uuid4

import boto3
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.core.applicable_rule import (  # noqa: E402
    ApplicabilityQuery,
    RuleCandidate,
    build_hero_query_text,
    decide_applicable_rule,
)
from src.external.dal import DAL, Tenant  # noqa: E402
from src.external.gate2_vector_search import (  # noqa: E402
    ClauseCarrierScope,
    CockroachClauseVectorSearch,
)
from src.external.titan_embeddings import (  # noqa: E402
    MODEL_ID,
    TitanTextEmbeddingsV2,
    embedding_input_sha256,
    embedding_sha256,
)

G3_TENANT = "10000000-0000-4000-8000-0000000000c4"
CARRIER = "20000000-0000-4000-8000-000000000010"
HERO_DATES = tuple(date(2026, 6, d) for d in range(8, 15))
SCOPE_LABEL = "US Oakland dry demurrage"


def _clause_embedding_input(clause_text: str) -> str:
    # Mirror compose_clause_embedding_input label format so query/clause align.
    return "\n".join([
        "document_family: TARIFF",
        "equipment: DRY",
        "route: USOAK",
        "service: STANDARD",
        f"tariff_clause: {clause_text}",
    ])


def _query_embedding_input(query_text: str) -> str:
    return "\n".join([
        "document_family: TARIFF",
        "equipment: DRY",
        "route: USOAK",
        "service: STANDARD",
        f"tariff_clause: {query_text}",
    ])


def _vector_literal(values) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _seed_clause(cur, embedder, *, snapshot_id, clause_ref, clause_text, rate,
                 effective_from, effective_to):
    embed_input = _clause_embedding_input(clause_text)
    emb = embedder.embed(embed_input)
    cur.execute(
        """
        INSERT INTO tariff_clauses
            (tenant_id, id, carrier_id, snapshot_id, clause_ref, clause_kind,
             clause_text, rate_amount, rate_currency, rate_unit, free_time_basis,
             sha256, effective_from, effective_to, source_locator, confidence,
             verification_status, verification_reason, equipment_type, route_code,
             service_context, document_family, embedding_model,
             embedding_input_sha256, embedding_sha256, embedding)
        VALUES (%s,%s,%s,%s,%s,'rate',%s,%s,'USD','CALENDAR_DAY',NULL,%s,%s,%s,
                %s,1.0,'VERIFIED','seeded for gate3 trace','DRY','USOAK','STANDARD',
                'TARIFF',%s,%s,%s,%s::VECTOR)
        ON CONFLICT (tenant_id, id) DO NOTHING;
        """,
        (G3_TENANT, str(uuid4()), CARRIER, snapshot_id, clause_ref, clause_text,
         Decimal(rate), _sha(clause_text), effective_from, effective_to,
         f"s3://representative/{clause_ref}", MODEL_ID,
         embedding_input_sha256(embed_input), embedding_sha256(emb.values),
         _vector_literal(emb.values)),
    )


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def main() -> None:
    dsn = _iso_dsn()
    embedder = TitanTextEmbeddingsV2(
        boto3.client("bedrock-runtime", region_name="us-east-1")
    )
    conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
    snapshot_id = str(uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s,%s) "
                    "ON CONFLICT (id) DO NOTHING;",
                    (G3_TENANT, "Gate3 (fictional)"))
        cur.execute("INSERT INTO carriers (tenant_id, id, scac, name) "
                    "VALUES (%s,%s,'ASTL','Asterline Demo (fictional)') "
                    "ON CONFLICT DO NOTHING;", (G3_TENANT, CARRIER))
        # Clear prior gate3 clauses for idempotent re-run.
        cur.execute("DELETE FROM tariff_clauses WHERE tenant_id=%s;", (G3_TENANT,))
        cur.execute("DELETE FROM tariff_snapshots WHERE tenant_id=%s;", (G3_TENANT,))
        cur.execute(
            """
            INSERT INTO tariff_snapshots
                (tenant_id, id, carrier_id, lane, version_label, effective_date,
                 captured_at, source_url, s3_key, doc_sha256, doc_text,
                 headline_rate, source_version_id)
            VALUES (%s,%s,%s,'USOAK','v1',%s,now(),
                    'https://representative.example/tariff','representative/tariff',
                    %s,'representative tariff',250,'v1')
            ON CONFLICT DO NOTHING;
            """,
            (G3_TENANT, snapshot_id, CARRIER, date(2026, 6, 1), _sha("doc")),
        )
        # Hero clause: $250/day, effective 2026-06-01 (covers June 8-14).
        _seed_clause(
            cur, embedder, snapshot_id=snapshot_id, clause_ref="Clause 4.2",
            clause_text="Demurrage rate: $250 per calendar day after free time expires.",
            rate="250.00", effective_from=date(2026, 6, 1), effective_to=None,
        )
        # Wrong-date distractor: same $250 rate, but effective only from July.
        _seed_clause(
            cur, embedder, snapshot_id=snapshot_id, clause_ref="Clause 9.9",
            clause_text="Demurrage rate: $250 per calendar day under the revised tariff.",
            rate="250.00", effective_from=date(2026, 7, 1), effective_to=None,
        )

    dal = DAL(conn, Tenant(G3_TENANT, "gate3-trace"))
    search = CockroachClauseVectorSearch(dal)

    query_text = build_hero_query_text(scope_label=SCOPE_LABEL, charged_dates=HERO_DATES)
    query_embed = embedder.embed(_query_embedding_input(query_text))

    # Real CockroachDB VECTOR index retrieval + EXPLAIN to prove index selection.
    explain = search.explain_index_use(
        scope=ClauseCarrierScope(carrier_id=CARRIER),
        query_embedding=query_embed.values, limit=5,
    )
    hits = search.search(
        scope=ClauseCarrierScope(carrier_id=CARRIER),
        query_embedding=query_embed.values, limit=5,
    )

    candidates = [
        RuleCandidate(
            clause_id=h.clause_id, public_ref=f"RULE-{h.clause_ref}",
            clause_ref=h.clause_ref, rank=rank, distance=h.l2_distance,
            clause_text=h.clause_text, display_excerpt=h.clause_text[:120],
            rate_amount=h.rate_amount, rate_currency=h.rate_currency,
            rate_unit=h.rate_unit, effective_from=h.effective_from,
            effective_to=h.effective_to,
            scope_code=f"DEMURRAGE:{h.route_code}:{h.equipment_type}",
            equipment_type=h.equipment_type, route_code=h.route_code,
            service_context=h.service_context, verification_status=h.verification_status,
            source_locator=h.source_locator or "", superseded=False,
        )
        for rank, h in enumerate(hits, 1)
    ]
    query = ApplicabilityQuery(
        charged_dates=HERO_DATES, invoice_currency="USD", expected_unit="CALENDAR_DAY",
        scope_code="DEMURRAGE:USOAK:DRY", expected_rate_phrase="$250",
        equipment_type="DRY", route_code="USOAK",
    )
    decision = decide_applicable_rule(candidates, query)

    rejected = [
        {"clause_ref": v.candidate.clause_ref, "rejection": v.rejection_code}
        for v in decision.candidate_validations if v.rejection_code
    ]
    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (gate3 tenant; defaultdb untouched)",
        "sponsor_path": (
            "REAL: Amazon Titan embed + CockroachDB Distributed Vector "
            "Indexing + deterministic validation"
        ),
        "embedding_model": MODEL_ID,
        "vector_index_selected": explain.uses_named_vector_index,
        "candidates_retrieved": len(hits),
        "candidate_refs_by_rank": [c.clause_ref for c in candidates],
        "decision_state": decision.state.value,
        "accepted_clause_ref": (
            decision.accepted.candidate.clause_ref if decision.accepted else None
        ),
        "accepted_rate_minor": (
            decision.accepted.rate_minor if decision.accepted else None
        ),
        "rejected_candidates": rejected,
        "wrong_date_distractor_rejected": any(
            r["rejection"] == "REJECTED_WRONG_DATE" for r in rejected
        ),
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    assert explain.uses_named_vector_index, "must select the named vector index"
    assert decision.state.value == "VERIFIED", "hero clause must be applicable"
    assert decision.accepted.candidate.clause_ref == "Clause 4.2"
    assert trace["wrong_date_distractor_rejected"], "distractor must be rejected"
    conn.close()


if __name__ == "__main__":
    main()

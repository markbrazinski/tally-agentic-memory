"""One bounded Gate 3 worker iteration: real vector retrieval + validation.

Leases a FIND_APPLICABLE_RULE task, builds the hero query from verified
reconstruction facts, embeds it with Titan, runs the real Distributed Vector
Indexing search, persists ranked candidates, then runs the deterministic
applicability firewall. Vector unavailability or no-candidate-passes fails closed
to NEEDS_EVIDENCE — never an embedded clause. Mirrors the reconstruction worker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date

from src.core.applicable_rule import (
    ApplicabilityQuery,
    RuleCandidate,
    build_hero_query_text,
    decide_applicable_rule,
)
from src.external.dal import DAL
from src.external.gate2_vector_search import (
    ClauseCarrierScope,
    ClauseVectorHit,
    CockroachClauseVectorSearch,
    VectorSearchError,
)
from src.external.titan_embeddings import MODEL_ID, TitanTextEmbeddingsV2
from src.platform.applicable_rule_repository import (
    RuleCompletion,
    claim_next_rule_task,
    complete_rule,
    fail_rule,
)

VECTOR_INDEX_NAME = "tariff_clause_embedding_search_idx"
SCOPE_LABEL = "US Oakland dry demurrage"
EXPECTED_RATE_PHRASE = "$250"

Embedder = Callable[[], TitanTextEmbeddingsV2]
SearchFactory = Callable[[DAL], CockroachClauseVectorSearch]


def run_one_rule_task(
    dal: DAL,
    *,
    worker_id: str,
    embedder: TitanTextEmbeddingsV2 | None = None,
    search: CockroachClauseVectorSearch | None = None,
) -> RuleCompletion | None:
    lease = claim_next_rule_task(dal, worker_id=worker_id)
    if lease is None:
        return None
    if not lease.charge_dates:
        fail_rule(dal, lease=lease, error_code="NO_CHARGE_DATES", retryable=False)
        return None

    charge_dates = tuple(date.fromisoformat(d) for d in lease.charge_dates)
    query_text = build_hero_query_text(
        scope_label=SCOPE_LABEL, charged_dates=charge_dates
    )
    try:
        embed = (embedder or TitanTextEmbeddingsV2()).embed(query_text)
        searcher = search or CockroachClauseVectorSearch(dal)
        hits = searcher.search(
            scope=ClauseCarrierScope(carrier_id=lease.carrier_id),
            query_embedding=embed.values,
            limit=5,
        )
    except VectorSearchError:
        fail_rule(dal, lease=lease, error_code="VECTOR_RETRIEVAL_UNAVAILABLE",
                  retryable=True)
        return None
    except Exception:
        # Embedding/provider failure — fail closed, retryable, no fallback clause.
        fail_rule(dal, lease=lease, error_code="VECTOR_EMBED_UNAVAILABLE",
                  retryable=True)
        return None

    # No candidate clause found is NOT a task failure: it is a genuine
    # missing-governing-tariff outcome. Fall through to decide_applicable_rule
    # with an empty candidate set (→ REJECTED / NO_APPLICABLE_RULE) and persist it
    # via complete_rule, which sets NEEDS_EVIDENCE and — when reconstruction
    # coverage is complete — hands off to judgment so the evaluator produces
    # REQUEST_EVIDENCE + RULE_NOT_VERIFIED (Demo v3 INV-1047). fail_rule would
    # instead stall the task with no recommendation.
    candidates = [_hit_to_candidate(hit, rank) for rank, hit in enumerate(hits, 1)]
    query = ApplicabilityQuery(
        charged_dates=charge_dates,
        invoice_currency=lease.invoice_currency,
        expected_unit="CALENDAR_DAY",
        scope_code=lease.scope_code,
        expected_rate_phrase=EXPECTED_RATE_PHRASE,
        equipment_type="DRY",
        route_code="USOAK",
    )
    decision = decide_applicable_rule(candidates, query)
    fingerprint = hashlib.sha256(
        json.dumps({"q": query_text, "recon": lease.reconstruction_id},
                   sort_keys=True).encode()
    ).hexdigest()
    return complete_rule(
        dal, lease=lease, query_text=query_text, query_fingerprint=fingerprint,
        embedding_model=MODEL_ID, embedding_input_sha256=embed.input_sha256,
        vector_index_name=VECTOR_INDEX_NAME, candidates=candidates, decision=decision,
    )


def _hit_to_candidate(hit: ClauseVectorHit, rank: int) -> RuleCandidate:
    return RuleCandidate(
        clause_id=hit.clause_id,
        public_ref=f"RULE-{hit.clause_ref}",
        clause_ref=hit.clause_ref,
        rank=rank,
        distance=hit.l2_distance,
        clause_text=hit.clause_text,
        display_excerpt=_excerpt(hit.clause_text),
        rate_amount=hit.rate_amount,
        rate_currency=hit.rate_currency,
        rate_unit=hit.rate_unit,
        effective_from=hit.effective_from,
        effective_to=hit.effective_to,
        scope_code=_scope_code(hit),
        equipment_type=hit.equipment_type,
        route_code=hit.route_code,
        service_context=hit.service_context,
        verification_status=hit.verification_status,
        source_locator=hit.source_locator or "",
        superseded=False,
    )


def _scope_code(hit: ClauseVectorHit) -> str:
    route = hit.route_code or "UNKNOWN"
    equipment = hit.equipment_type or "UNKNOWN"
    return f"DEMURRAGE:{route}:{equipment}"


def _excerpt(clause_text: str, limit: int = 120) -> str:
    text = " ".join(clause_text.split())
    return text[:limit]

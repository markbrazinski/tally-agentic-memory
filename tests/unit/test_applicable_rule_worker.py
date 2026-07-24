"""Gate 3 worker tests: real vector search seam + fail-closed no-fallback.

Proves: a hero clause hit produces a VERIFIED applicable rule; a wrong-date
distractor that ranks first is rejected deterministically; vector-search
unavailability and embedding failure route to fail_rule (never complete);
empty hits refuse with NO_APPLICABLE_RULE. Zero-network via monkeypatch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from src.external.gate2_vector_search import ClauseVectorHit, VectorSearchError
from src.platform import applicable_rule_worker as worker
from src.platform.applicable_rule_repository import RuleCompletion, RuleTaskLease

CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _lease(charge_dates=None):
    return RuleTaskLease(
        task_id="task-1", invoice_id="invoice-1", reconstruction_id="recon-1",
        attempt=1, worker_id="w1", knowledge_cutoff_at=CUTOFF,
        input_fingerprint="fp-1", carrier_id="carrier-1",
        scope_code="DEMURRAGE:USOAK:DRY",
        charge_dates=tuple(charge_dates if charge_dates is not None
                           else [f"2026-06-{d:02d}" for d in range(8, 15)]),
        invoice_currency="USD", initiated_by=None, actor_display="Rachel",
    )


def _hit(*, clause_ref="Clause 4.2", rate="250.00", eff_from=date(2026, 6, 1),
         text="Demurrage rate: $250 per calendar day after free time.",
         verification="VERIFIED"):
    return ClauseVectorHit(
        clause_id="clause-" + clause_ref, snapshot_id="snap-1", carrier_id="carrier-1",
        source_version_id="v1", source_sha256="a" * 64, source_key="k",
        clause_sha256="b" * 64, snapshot_captured_at=CUTOFF, clause_ref=clause_ref,
        clause_text=text, rate_amount=Decimal(rate), rate_currency="USD",
        rate_unit="CALENDAR_DAY", source_locator="s3://private/clause",
        confidence=Decimal("1.0"), effective_from=eff_from, effective_to=None,
        equipment_type="DRY", route_code="USOAK", service_context=None,
        document_family="TARIFF", verification_status=verification,
        embedding_model="amazon.titan-embed-text-v2:0", embedding_input_sha256="c" * 64,
        embedding_sha256="d" * 64, l2_distance=0.1, embedding=(0.0,) * 1024,
    )


class _Embed:
    values = (0.0,) * 1024
    input_sha256 = "e" * 64


class _Embedder:
    def embed(self, text):
        assert "demurrage" in text
        return _Embed()


class _Search:
    def __init__(self, hits, *, raise_exc=None):
        self._hits = hits
        self._raise = raise_exc

    def search(self, *, scope, query_embedding, limit):
        if self._raise:
            raise self._raise
        return self._hits


def _patch(monkeypatch, *, lease):
    monkeypatch.setattr(worker, "claim_next_rule_task", lambda *a, **k: lease)
    completed = {}
    failed = {}
    monkeypatch.setattr(
        worker, "complete_rule",
        lambda dal, **k: completed.update(k) or RuleCompletion(
            "run-1", "rule-1" if k["decision"].state.value == "VERIFIED" else None,
            k["decision"].state.value, len(k["candidates"]),
            k["decision"].accepted.rate_minor if k["decision"].accepted else None),
    )
    monkeypatch.setattr(
        worker, "fail_rule",
        lambda dal, **k: failed.update(k) or "BLOCKED",
    )
    return completed, failed


def test_hero_clause_verified(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease())
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=_Embedder(), search=_Search([_hit()])
    )
    assert not failed
    assert result.state == "VERIFIED"
    assert result.accepted_rate_minor == 25000
    assert completed["decision"].accepted.rate_minor == 25000


def test_wrong_date_distractor_rejected(monkeypatch):
    # Distractor ranks FIRST (returned first) but is effective only from July.
    completed, failed = _patch(monkeypatch, lease=_lease())
    hits = [
        _hit(clause_ref="Clause 9.9", eff_from=date(2026, 7, 1),
             text="Demurrage rate: $250 per calendar day (new tariff)."),
        _hit(clause_ref="Clause 4.2"),  # correct one, ranks second
    ]
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=_Embedder(), search=_Search(hits)
    )
    assert result.state == "VERIFIED"
    # The accepted rule is the correctly-dated clause, not the top-ranked distractor.
    assert completed["decision"].accepted.candidate.clause_ref == "Clause 4.2"
    # The distractor was persisted as a REJECTED candidate.
    rejected = [v for v in completed["decision"].candidate_validations
                if v.rejection_code == "REJECTED_WRONG_DATE"]
    assert len(rejected) == 1


def test_vector_unavailable_fails_closed(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease())
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=_Embedder(),
        search=_Search([], raise_exc=VectorSearchError("index down")),
    )
    assert result is None
    assert not completed  # NO embedded-clause fallback
    assert failed["error_code"] == "VECTOR_RETRIEVAL_UNAVAILABLE"
    assert failed["retryable"] is True


def test_embedding_failure_fails_closed(monkeypatch):
    class BadEmbedder:
        def embed(self, text):
            raise RuntimeError("bedrock down")

    completed, failed = _patch(monkeypatch, lease=_lease())
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=BadEmbedder(), search=_Search([_hit()])
    )
    assert result is None
    assert not completed
    assert failed["error_code"] == "VECTOR_EMBED_UNAVAILABLE"


def test_empty_hits_refuses(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease())
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=_Embedder(), search=_Search([])
    )
    assert result is None
    assert not completed
    assert failed["error_code"] == "NO_APPLICABLE_RULE"
    assert failed["retryable"] is False


def test_no_charge_dates_fails(monkeypatch):
    completed, failed = _patch(monkeypatch, lease=_lease(charge_dates=[]))
    result = worker.run_one_rule_task(
        object(), worker_id="w1", embedder=_Embedder(), search=_Search([_hit()])
    )
    assert result is None
    assert failed["error_code"] == "NO_CHARGE_DATES"

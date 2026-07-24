"""Pure applicability tests: retrieval-vs-applicability firewall (Gate 3).

Covers the commission validator-mutation matrix (§13.5): each of exact text,
rate, currency, unit, effective date, scope, source-version, supersession must
independently reject an otherwise-passing candidate. A wrong-date candidate that
ranks first is still rejected. Conflicting accepted rates → CONFLICTED.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.core.applicable_rule import (
    ApplicabilityQuery,
    CandidateState,
    RuleCandidate,
    RuleValidationState,
    build_hero_query_text,
    decide_applicable_rule,
    validate_candidate,
)

HERO_DATES = tuple(date(2026, 6, d) for d in range(8, 15))  # June 8..14


def _query(**over):
    base = dict(
        charged_dates=HERO_DATES,
        invoice_currency="USD",
        expected_unit="CALENDAR_DAY",
        scope_code="DEMURRAGE:USOAK:DRY",
        expected_rate_phrase="$250",
        equipment_type="DRY",
        route_code="USOAK",
    )
    base.update(over)
    return ApplicabilityQuery(**base)


def _candidate(**over):
    base = dict(
        clause_id="clause-1",
        public_ref="RULE-1",
        clause_ref="Clause 4.2",
        rank=1,
        distance=0.12,
        clause_text="Demurrage rate: $250 per calendar day after free time.",
        display_excerpt="Demurrage rate: $250 per calendar day",
        rate_amount=Decimal("250.00"),
        rate_currency="USD",
        rate_unit="CALENDAR_DAY",
        effective_from=date(2026, 6, 1),
        effective_to=None,
        scope_code="DEMURRAGE:USOAK:DRY",
        equipment_type="DRY",
        route_code="USOAK",
        service_context=None,
        verification_status="VERIFIED",
        source_locator="s3://private/clause",
        superseded=False,
    )
    base.update(over)
    return RuleCandidate(**base)


def test_hero_candidate_accepted_and_rate_minor():
    v = validate_candidate(_candidate(), _query())
    assert v.state is CandidateState.ACCEPTED
    assert v.rate_minor == 25000  # $250 -> minor units
    assert all(r == "VERIFIED" for r in v.results.values())


def test_wrong_effective_date_rejected_even_if_top_ranked():
    # Clause effective only from July — does not cover the June charged period.
    v = validate_candidate(
        _candidate(rank=1, distance=0.01, effective_from=date(2026, 7, 1)),
        _query(),
    )
    assert v.state is CandidateState.REJECTED
    assert v.rejection_code == "REJECTED_WRONG_DATE"


def test_effective_to_before_period_rejected():
    v = validate_candidate(
        _candidate(effective_from=date(2026, 1, 1), effective_to=date(2026, 6, 10)),
        _query(),
    )
    assert v.state is CandidateState.REJECTED
    assert v.rejection_code == "REJECTED_WRONG_DATE"


def test_text_mismatch_rejected():
    v = validate_candidate(
        _candidate(clause_text="Detention rate: $999 per day."), _query()
    )
    assert v.state is CandidateState.REJECTED
    assert v.rejection_code == "REJECTED_TEXT_MISMATCH"


def test_currency_mismatch_rejected():
    v = validate_candidate(_candidate(rate_currency="EUR"), _query())
    assert v.state is CandidateState.REJECTED
    assert v.results["currency"] == "FAILED"


def test_unit_mismatch_rejected():
    v = validate_candidate(_candidate(rate_unit="PER_HOUR"), _query())
    assert v.state is CandidateState.REJECTED
    assert v.results["unit"] == "FAILED"


def test_scope_mismatch_rejected():
    v = validate_candidate(_candidate(scope_code="DEMURRAGE:USLAX:DRY"), _query())
    assert v.state is CandidateState.REJECTED
    assert v.rejection_code == "REJECTED_SCOPE_MISMATCH"


def test_unverified_source_rejected():
    v = validate_candidate(_candidate(verification_status="UNAVAILABLE"), _query())
    assert v.state is CandidateState.REJECTED
    assert v.rejection_code == "VERSION_UNAVAILABLE"


def test_superseded_rejected():
    v = validate_candidate(_candidate(superseded=True), _query())
    assert v.state is CandidateState.REJECTED
    assert v.results["supersession"] == "FAILED"


def test_unparseable_rate_rejected():
    v = validate_candidate(_candidate(rate_amount=None), _query())
    assert v.state is CandidateState.REJECTED
    assert v.results["exact_rate"] == "FAILED"


def test_decide_accepts_single_valid_lowest_rank():
    good = _candidate(rank=2)
    bad = _candidate(clause_id="c2", public_ref="RULE-2", rank=1,
                     effective_from=date(2026, 7, 1))  # wrong date
    decision = decide_applicable_rule([bad, good], _query())
    assert decision.state is RuleValidationState.VERIFIED
    assert decision.accepted.candidate.public_ref == "RULE-1"


def test_decide_no_candidate_passes_is_refusal():
    bad = _candidate(effective_from=date(2026, 7, 1))
    decision = decide_applicable_rule([bad], _query())
    assert decision.state is RuleValidationState.REJECTED
    assert decision.public_error == "NO_APPLICABLE_RULE"


def test_decide_empty_candidates_refuses():
    decision = decide_applicable_rule([], _query())
    assert decision.state is RuleValidationState.REJECTED
    assert decision.public_error == "NO_APPLICABLE_RULE"


def test_decide_conflicting_rates_is_conflicted():
    a = _candidate(clause_id="a", public_ref="RULE-A", rank=1)
    b = _candidate(
        clause_id="b", public_ref="RULE-B", rank=2,
        rate_amount=Decimal("300.00"),
        clause_text="Demurrage rate: $300 per calendar day.",
        display_excerpt="$300",
    )
    # b's text must contain the query phrase to be 'accepted'; use $250 phrase
    # query only matches a. Give both a matching phrase to force a real conflict.
    q = _query(expected_rate_phrase="per calendar day")
    decision = decide_applicable_rule([a, b], q)
    assert decision.state is RuleValidationState.CONFLICTED
    assert decision.public_error == "RULE_CONFLICT"


def test_hero_query_text_deterministic():
    text = build_hero_query_text(scope_label="US Oakland dry demurrage",
                                 charged_dates=HERO_DATES)
    assert text == (
        "demurrage calendar-day rate for US Oakland dry demurrage "
        "during 2026-06-08 through 2026-06-14"
    )

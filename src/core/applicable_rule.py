"""Pure deterministic applicability validation for Gate 3.

The firewall: vector retrieval yields only a CANDIDATE. A candidate becomes an
APPLICABLE rule solely when every deterministic validator passes here — vector
similarity/distance never decides applicability. Zero external I/O.

Validators (commission §8.3): exact source-version state, exact clause text
containment, exact rate, currency match, unit match, effective-date coverage for
every charged date, scope match, supersession, boundary agreement. A wrong-date
candidate may rank first yet must be rejected. Two candidates passing with
conflicting rates → CONFLICTED (request evidence), never a silent pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class CandidateState(StrEnum):
    RETRIEVED = "RETRIEVED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class RuleValidationState(StrEnum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    CONFLICTED = "CONFLICTED"


# Validator result codes, each independently falsifiable (commission §13.5).
VALIDATORS = (
    "source_version",
    "exact_text",
    "exact_rate",
    "currency",
    "unit",
    "effective_date",
    "scope",
    "supersession",
    "boundary_semantics",
)


@dataclass(frozen=True)
class RuleCandidate:
    """A clause returned by the vector index — candidate only until validated."""

    clause_id: str
    public_ref: str
    clause_ref: str
    rank: int
    distance: float
    clause_text: str
    display_excerpt: str
    rate_amount: Decimal | None
    rate_currency: str | None
    rate_unit: str | None
    effective_from: date | None
    effective_to: date | None
    scope_code: str
    equipment_type: str | None
    route_code: str | None
    service_context: str | None
    verification_status: str
    source_locator: str
    superseded: bool = False


@dataclass(frozen=True)
class ApplicabilityQuery:
    """The verified facts a candidate is validated against."""

    charged_dates: tuple[date, ...]
    invoice_currency: str
    expected_unit: str  # "CALENDAR_DAY"
    scope_code: str
    expected_rate_phrase: str  # e.g. "$250" — must appear in exact clause text
    equipment_type: str | None = None
    route_code: str | None = None


@dataclass(frozen=True)
class CandidateValidation:
    candidate: RuleCandidate
    state: CandidateState
    results: dict[str, str]  # validator -> VERIFIED | FAILED | UNKNOWN
    rejection_code: str | None
    rate_minor: int | None


def _rate_minor(amount: Decimal | None) -> int | None:
    if amount is None:
        return None
    try:
        if amount < 0 or amount.as_tuple().exponent < -2:
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        return None


def validate_candidate(
    candidate: RuleCandidate, query: ApplicabilityQuery
) -> CandidateValidation:
    """Run every deterministic validator. All must pass for ACCEPTED.

    A single FAILED/UNKNOWN validator rejects the candidate with a specific code
    — vector rank/distance is never consulted here.
    """
    results: dict[str, str] = {}

    results["source_version"] = (
        "VERIFIED" if candidate.verification_status == "VERIFIED" else "FAILED"
    )
    results["supersession"] = "FAILED" if candidate.superseded else "VERIFIED"

    # Exact rate: the rate phrase must appear verbatim in the clause text AND the
    # structured rate must parse. Guards against a model-only or mismatched rate.
    rate_minor = _rate_minor(candidate.rate_amount)
    phrase_in_text = _normalize(query.expected_rate_phrase) in _normalize(
        candidate.clause_text
    )
    results["exact_text"] = "VERIFIED" if phrase_in_text else "FAILED"
    results["exact_rate"] = "VERIFIED" if rate_minor is not None else "FAILED"

    results["currency"] = (
        "VERIFIED"
        if (candidate.rate_currency or "").upper() == query.invoice_currency.upper()
        else "FAILED"
    )
    results["unit"] = (
        "VERIFIED"
        if _norm_unit(candidate.rate_unit) == _norm_unit(query.expected_unit)
        else "FAILED"
    )

    # Effective date: the clause must be effective for EVERY charged date.
    results["effective_date"] = (
        "VERIFIED" if _covers_all_dates(candidate, query.charged_dates) else "FAILED"
    )

    results["scope"] = (
        "VERIFIED" if _scope_matches(candidate, query) else "FAILED"
    )
    # Boundary semantics: rate unit is per calendar day and the clause has a
    # concrete effective_from — the versioned boundary policy can bind to it.
    results["boundary_semantics"] = (
        "VERIFIED"
        if candidate.effective_from is not None and results["unit"] == "VERIFIED"
        else "FAILED"
    )

    failed = [v for v in VALIDATORS if results.get(v) != "VERIFIED"]
    if failed:
        return CandidateValidation(
            candidate=candidate,
            state=CandidateState.REJECTED,
            results=results,
            rejection_code=_rejection_code(failed[0]),
            rate_minor=rate_minor,
        )
    return CandidateValidation(
        candidate=candidate,
        state=CandidateState.ACCEPTED,
        results=results,
        rejection_code=None,
        rate_minor=rate_minor,
    )


@dataclass(frozen=True)
class ApplicabilityDecision:
    state: RuleValidationState
    accepted: CandidateValidation | None
    candidate_validations: tuple[CandidateValidation, ...]
    public_error: str | None


def decide_applicable_rule(
    candidates: list[RuleCandidate], query: ApplicabilityQuery
) -> ApplicabilityDecision:
    """Validate all candidates; accept exactly one, or refuse.

    - No candidate passes → REJECTED / NO_APPLICABLE_RULE (fail closed).
    - Exactly one distinct accepted rate → VERIFIED (lowest rank wins ties).
    - Two accepted candidates with conflicting rates → CONFLICTED / RULE_CONFLICT.
    Vector distance never overrides a failed validator.
    """
    validations = tuple(validate_candidate(c, query) for c in candidates)
    accepted = [v for v in validations if v.state is CandidateState.ACCEPTED]
    if not accepted:
        return ApplicabilityDecision(
            state=RuleValidationState.REJECTED,
            accepted=None,
            candidate_validations=validations,
            public_error="NO_APPLICABLE_RULE",
        )
    distinct_rates = {v.rate_minor for v in accepted}
    if len(distinct_rates) > 1:
        return ApplicabilityDecision(
            state=RuleValidationState.CONFLICTED,
            accepted=None,
            candidate_validations=validations,
            public_error="RULE_CONFLICT",
        )
    best = min(accepted, key=lambda v: v.candidate.rank)
    return ApplicabilityDecision(
        state=RuleValidationState.VERIFIED,
        accepted=best,
        candidate_validations=validations,
        public_error=None,
    )


def build_hero_query_text(
    *, scope_label: str, charged_dates: tuple[date, ...]
) -> str:
    """Deterministic hero retrieval query text (commission §8.2).

    Built only from verified facts (scope + charged period), never free text.
    """
    start = min(charged_dates).isoformat()
    end = max(charged_dates).isoformat()
    return (
        f"demurrage calendar-day rate for {scope_label} "
        f"during {start} through {end}"
    )


def _covers_all_dates(candidate: RuleCandidate, dates: tuple[date, ...]) -> bool:
    if candidate.effective_from is None:
        return False
    for d in dates:
        if d < candidate.effective_from:
            return False
        if candidate.effective_to is not None and d > candidate.effective_to:
            return False
    return True


def _scope_matches(candidate: RuleCandidate, query: ApplicabilityQuery) -> bool:
    if candidate.scope_code != query.scope_code:
        return False
    if query.equipment_type and candidate.equipment_type not in (
        None, query.equipment_type
    ):
        return False
    if query.route_code and candidate.route_code not in (None, query.route_code):
        return False
    return True


def _rejection_code(validator: str) -> str:
    return {
        "effective_date": "REJECTED_WRONG_DATE",
        "exact_text": "REJECTED_TEXT_MISMATCH",
        "scope": "REJECTED_SCOPE_MISMATCH",
        "source_version": "VERSION_UNAVAILABLE",
    }.get(validator, f"REJECTED_{validator.upper()}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _norm_unit(unit: str | None) -> str:
    if not unit:
        return ""
    return re.sub(r"[^a-z]", "", unit.lower()).replace("percalendarday", "calendarday")

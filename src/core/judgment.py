"""Pure deterministic judgment engine for Gate 4.

One independently-explainable judgment per charged day, integer-minor-unit
arithmetic only, and a single frozen recommendation. The model never computes an
amount here — every number is Python arithmetic over persisted Gate 2 (charged
days + coverage) and Gate 3 (applicable rule) outputs.

Outcomes:
- A fully-covered day whose invoice rate exceeds the applicable rate →
  RATE_DISCREPANCY (disputed = difference).
- A fully-covered day whose invoice rate equals the applicable rate → SUPPORTED.
- A day missing coverage or applicable rate → INSUFFICIENT_EVIDENCE.

Recommendation:
- Any insufficient day → REQUEST_EVIDENCE (never a partial dispute).
- All supported → APPROVE_FOR_PAYMENT (the $875 restraint case).
- All covered with discrepancy → DISPUTE (the $700 hero).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class DayOutcome(StrEnum):
    SUPPORTED = "SUPPORTED"
    RATE_DISCREPANCY = "RATE_DISCREPANCY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXCLUDED_NOT_CHARGEABLE = "EXCLUDED_NOT_CHARGEABLE"


class RecommendationType(StrEnum):
    DISPUTE = "DISPUTE"
    APPROVE_FOR_PAYMENT = "APPROVE_FOR_PAYMENT"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"


class ReasonCode(StrEnum):
    """Machine-readable reasons a recommendation withholds or supports action
    (Delta §2.1). Recorded on the frozen recommendation for inspection."""

    MISSING_DAY_SOURCE = "MISSING_DAY_SOURCE"
    MISSING_DAY_ACCESS_EVIDENCE = "MISSING_DAY_ACCESS_EVIDENCE"
    RULE_NOT_VERIFIED = "RULE_NOT_VERIFIED"
    SOURCE_VERSION_UNAVAILABLE = "SOURCE_VERSION_UNAVAILABLE"
    SOURCE_INTEGRITY_FAILED = "SOURCE_INTEGRITY_FAILED"


@dataclass(frozen=True)
class DayInput:
    charge_date: date
    invoice_rate_minor: int
    applicable_rate_minor: int | None
    currency: str
    coverage_state: str  # PRESENT_VERIFIED | MISSING | ...
    chargeable: bool
    # The reconstruction's per-day missing-requirement codes (e.g.
    # ("TERMINAL_ACCESS",)). Lets the evaluator emit a precise reason code —
    # MISSING_DAY_ACCESS_EVIDENCE vs the generic MISSING_DAY_SOURCE. Defaulted
    # so existing callers/tests are unaffected.
    missing_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayJudgment:
    charge_date: date
    invoice_rate_minor: int
    applicable_rate_minor: int | None
    discrepancy_minor: int
    currency: str
    outcome: DayOutcome
    explanation: str


def judge_day(day: DayInput) -> DayJudgment:
    """Deterministically resolve one charged day. Integer minor units only."""
    if not day.chargeable:
        return DayJudgment(
            day.charge_date, day.invoice_rate_minor, day.applicable_rate_minor,
            0, day.currency, DayOutcome.EXCLUDED_NOT_CHARGEABLE,
            "Day is not chargeable under the sourced timeline.",
        )
    if (
        day.coverage_state != "PRESENT_VERIFIED"
        or day.applicable_rate_minor is None
    ):
        return DayJudgment(
            day.charge_date, day.invoice_rate_minor, day.applicable_rate_minor,
            0, day.currency, DayOutcome.INSUFFICIENT_EVIDENCE,
            "Source coverage or applicable rate is missing for this day.",
        )
    discrepancy = day.invoice_rate_minor - day.applicable_rate_minor
    if discrepancy == 0:
        return DayJudgment(
            day.charge_date, day.invoice_rate_minor, day.applicable_rate_minor,
            0, day.currency, DayOutcome.SUPPORTED,
            "Invoice rate matches the applicable recorded tariff.",
        )
    return DayJudgment(
        day.charge_date, day.invoice_rate_minor, day.applicable_rate_minor,
        discrepancy, day.currency, DayOutcome.RATE_DISCREPANCY,
        f"Invoice rate exceeds the applicable tariff by "
        f"{_fmt(discrepancy, day.currency)} for this day.",
    )


@dataclass(frozen=True)
class Recommendation:
    recommendation_type: RecommendationType
    disputed_amount_minor: int
    supported_amount_minor: int
    claimed_amount_minor: int
    currency: str
    days_total: int
    days_covered: int
    evidence_coverage: str
    judgments: tuple[DayJudgment, ...]
    reason_codes: tuple[ReasonCode, ...]
    digest: str
    summary: str


def resolve_recommendation(days: list[DayInput]) -> Recommendation:
    """Judge every day and resolve one deterministic recommendation.

    Currency is required consistent across days; a mismatch is a hard failure
    (never silently coerced). The disputed amount is the exact sum of per-day
    discrepancies — there is no aggregate-only path.
    """
    if not days:
        raise ValueError("NO_CHARGED_DAYS")
    currency = days[0].currency
    if any(d.currency != currency for d in days):
        raise ValueError("CURRENCY_MISMATCH")

    judgments = tuple(judge_day(d) for d in days)
    chargeable = [j for j in judgments if j.outcome is not DayOutcome.EXCLUDED_NOT_CHARGEABLE]
    claimed = sum(j.invoice_rate_minor for j in chargeable)
    disputed = sum(
        j.discrepancy_minor for j in judgments
        if j.outcome is DayOutcome.RATE_DISCREPANCY
    )
    supported = sum(
        j.applicable_rate_minor
        for j in judgments
        if j.outcome in (DayOutcome.SUPPORTED, DayOutcome.RATE_DISCREPANCY)
        and j.applicable_rate_minor is not None
    )
    days_total = len(chargeable)
    days_covered = sum(
        1 for j in chargeable if j.outcome is not DayOutcome.INSUFFICIENT_EVIDENCE
    )

    # Reason codes explain a withheld/insufficient decision (Delta §2.1). A day
    # that resolves INSUFFICIENT_EVIDENCE is either missing source coverage
    # (MISSING_DAY_SOURCE) or missing a verified applicable rate (RULE_NOT_VERIFIED).
    # Derived from the same DayInput the judgment used — never client-supplied.
    reason: list[ReasonCode] = []
    by_day = {d.charge_date: d for d in days}
    for j in chargeable:
        if j.outcome is not DayOutcome.INSUFFICIENT_EVIDENCE:
            continue
        src = by_day.get(j.charge_date)
        if src is not None and src.coverage_state != "PRESENT_VERIFIED":
            # A per-day terminal-access gap is the specific hero reason: the
            # retained TERMINAL_ACCESS_SNAPSHOT exists in memory but is not yet
            # bound to this day. Otherwise it's a generic missing source.
            if src is not None and "TERMINAL_ACCESS" in src.missing_requirements:
                code = ReasonCode.MISSING_DAY_ACCESS_EVIDENCE
            else:
                code = ReasonCode.MISSING_DAY_SOURCE
        else:
            code = ReasonCode.RULE_NOT_VERIFIED
        if code not in reason:
            reason.append(code)
    reason_codes = tuple(reason)

    has_insufficient = any(
        j.outcome is DayOutcome.INSUFFICIENT_EVIDENCE for j in chargeable
    )
    if has_insufficient or days_total == 0:
        rec_type = RecommendationType.REQUEST_EVIDENCE
        summary = "Evidence is incomplete; requesting evidence before deciding."
    elif disputed > 0:
        rec_type = RecommendationType.DISPUTE
        summary = f"Dispute {_fmt(disputed, currency)} across {days_total} sourced days."
    else:
        rec_type = RecommendationType.APPROVE_FOR_PAYMENT
        summary = f"Approve {_fmt(supported, currency)} for payment; rates match."

    coverage = f"{days_covered} of {days_total} days"
    digest = _digest(
        rec_type, disputed, supported, claimed, currency, judgments, reason_codes
    )
    return Recommendation(
        recommendation_type=rec_type,
        disputed_amount_minor=disputed,
        supported_amount_minor=supported,
        claimed_amount_minor=claimed,
        currency=currency,
        days_total=days_total,
        days_covered=days_covered,
        evidence_coverage=coverage,
        judgments=judgments,
        reason_codes=reason_codes,
        digest=digest,
        summary=summary,
    )


def recommendation_fingerprint(days: list[DayInput]) -> str:
    """Deterministic fingerprint over the judgment inputs (for freezing)."""
    payload = [
        {
            "d": d.charge_date.isoformat(),
            "inv": d.invoice_rate_minor,
            "app": d.applicable_rate_minor,
            "cur": d.currency,
            "cov": d.coverage_state,
            "chg": d.chargeable,
        }
        for d in days
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest(
    rec_type, disputed, supported, claimed, currency, judgments, reason_codes=()
) -> str:
    payload = {
        "type": rec_type.value,
        "disputed": disputed,
        "supported": supported,
        "claimed": claimed,
        "currency": currency,
        "reasons": [r.value for r in reason_codes],
        "days": [
            {"d": j.charge_date.isoformat(), "o": j.outcome.value,
             "disc": j.discrepancy_minor}
            for j in judgments
        ],
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fmt(minor: int, currency: str) -> str:
    return f"{currency} {minor / 100:.2f}"

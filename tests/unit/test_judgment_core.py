"""Pure judgment tests: the locked $700, the $875 restraint, REQUEST_EVIDENCE.

Every amount is integer minor units and independently explainable per day.
Covers the demo-locked hero and restraint cases, missing-evidence restraint,
deterministic replay, and currency-mismatch fail-closed (handoff §12).
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.judgment import (
    DayInput,
    DayOutcome,
    RecommendationType,
    judge_day,
    recommendation_fingerprint,
    resolve_recommendation,
)

HERO_DATES = [date(2026, 6, d) for d in range(8, 15)]  # 7 days


def _days(invoice_minor, applicable_minor, *, coverage="PRESENT_VERIFIED",
          chargeable=True, currency="USD"):
    return [
        DayInput(d, invoice_minor, applicable_minor, currency, coverage, chargeable)
        for d in HERO_DATES
    ]


def test_hero_seven_day_700_dispute():
    # 7 days x ($350 - $250) = $700, each day an independent $100 discrepancy.
    rec = resolve_recommendation(_days(35000, 25000))
    assert rec.recommendation_type is RecommendationType.DISPUTE
    assert rec.disputed_amount_minor == 70000  # $700
    assert rec.claimed_amount_minor == 245000  # $2,450
    assert rec.supported_amount_minor == 175000  # $1,750
    assert rec.days_total == 7
    assert rec.days_covered == 7
    # seven independent rows, each $100 discrepancy — no aggregate-only path
    assert len(rec.judgments) == 7
    assert all(j.outcome is DayOutcome.RATE_DISCREPANCY for j in rec.judgments)
    assert all(j.discrepancy_minor == 10000 for j in rec.judgments)
    assert sum(j.discrepancy_minor for j in rec.judgments) == 70000


def test_restraint_875_approve_for_payment():
    # $125/day matches applicable $125/day for 7 days -> approve $875.
    rec = resolve_recommendation(_days(12500, 12500))
    assert rec.recommendation_type is RecommendationType.APPROVE_FOR_PAYMENT
    assert rec.disputed_amount_minor == 0
    assert rec.supported_amount_minor == 87500  # $875
    assert all(j.outcome is DayOutcome.SUPPORTED for j in rec.judgments)


def test_missing_evidence_requests_evidence_not_dispute():
    # A discrepancy exists on covered days, but one day lacks coverage ->
    # REQUEST_EVIDENCE, never a partial dispute.
    days = _days(35000, 25000)
    days[3] = DayInput(days[3].charge_date, 35000, None, "USD", "MISSING", True)
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.REQUEST_EVIDENCE
    assert rec.days_covered == 6
    assert any(j.outcome is DayOutcome.INSUFFICIENT_EVIDENCE for j in rec.judgments)


def test_no_applicable_rate_is_insufficient():
    rec = resolve_recommendation(_days(35000, None, coverage="PRESENT_VERIFIED"))
    assert rec.recommendation_type is RecommendationType.REQUEST_EVIDENCE
    assert all(j.outcome is DayOutcome.INSUFFICIENT_EVIDENCE for j in rec.judgments)


def test_excluded_day_not_counted():
    days = _days(35000, 25000)
    days[6] = DayInput(days[6].charge_date, 35000, 25000, "USD", "PRESENT_VERIFIED",
                       False)  # not chargeable
    rec = resolve_recommendation(days)
    assert rec.days_total == 6  # excluded day dropped from totals
    assert rec.disputed_amount_minor == 60000  # 6 x $100


def test_currency_mismatch_fails_closed():
    days = _days(35000, 25000)
    days[0] = DayInput(days[0].charge_date, 35000, 25000, "EUR", "PRESENT_VERIFIED", True)
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        resolve_recommendation(days)


def test_empty_days_fails():
    with pytest.raises(ValueError, match="NO_CHARGED_DAYS"):
        resolve_recommendation([])


def test_deterministic_replay_identical_digest():
    a = resolve_recommendation(_days(35000, 25000))
    b = resolve_recommendation(_days(35000, 25000))
    assert a.digest == b.digest
    assert recommendation_fingerprint(_days(35000, 25000)) == \
        recommendation_fingerprint(_days(35000, 25000))


def test_different_inputs_different_fingerprint():
    assert recommendation_fingerprint(_days(35000, 25000)) != \
        recommendation_fingerprint(_days(35000, 20000))


def test_single_day_discrepancy_explained():
    j = judge_day(DayInput(date(2026, 6, 10), 35000, 25000, "USD",
                           "PRESENT_VERIFIED", True))
    assert j.outcome is DayOutcome.RATE_DISCREPANCY
    assert j.discrepancy_minor == 10000
    assert "USD 100.00" in j.explanation


def test_rounding_boundary_minor_units_exact():
    # $250.005 is not representable in minor units upstream; here we assert the
    # engine never introduces fractional cents — pure integer subtraction.
    rec = resolve_recommendation(_days(35001, 25000))  # $350.01 vs $250.00
    assert rec.disputed_amount_minor == 7 * 10001
    assert all(isinstance(j.discrepancy_minor, int) for j in rec.judgments)

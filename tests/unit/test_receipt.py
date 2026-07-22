from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.receipt import (
    TariffExtraction,
    calculate_overcharge,
    parse_invoice_claim,
    verify_tariff_extraction,
)

ROOT = Path(__file__).resolve().parents[2]
TARIFF_BYTES = (ROOT / "tests/fixtures/gate1/northstar-tariff.txt").read_bytes()
INVOICE_BYTES = (ROOT / "tests/fixtures/gate1/northstar-invoice.json").read_bytes()
CLAUSE = (
    "Section 4 — Import demurrage: The rate is USD $250.00 per day for each "
    "chargeable day after free time."
)


def _valid_extraction() -> TariffExtraction:
    return TariffExtraction.from_mapping(
        {
            "rate_amount": "250.00",
            "rate_currency": "USD",
            "rate_unit": "per_day",
            "rate_text": "USD $250.00 per day",
            "effective_from": "2026-07-01",
            "effective_to": "2026-12-31",
            "clause_text": CLAUSE,
            "source_locator": "Section 4",
            "confidence": "0.99",
        }
    )


def test_valid_250_extraction_commits_as_eligible():
    result = verify_tariff_extraction(TARIFF_BYTES, _valid_extraction())

    assert result.eligible is True
    assert result.reason == "verified"
    assert len(result.source_sha256) == 64
    assert len(result.clause_sha256) == 64


def test_extracted_value_absent_from_source_abstains():
    extraction = replace(_valid_extraction(), clause_text="A made-up clause at USD $250.00/day.")

    result = verify_tariff_extraction(TARIFF_BYTES, extraction)

    assert result.eligible is False
    assert "clause_absent_from_source" in result.reasons


def test_clause_present_but_rate_outside_clause_abstains():
    extraction = replace(_valid_extraction(), rate_text="USD $350.00 per day")

    result = verify_tariff_extraction(TARIFF_BYTES, extraction)

    assert result.eligible is False
    assert "rate_text_absent_from_clause" in result.reasons


def test_invalid_effective_interval_abstains():
    extraction = replace(
        _valid_extraction(), effective_from=_valid_extraction().effective_to,
        effective_to=_valid_extraction().effective_from,
    )

    result = verify_tariff_extraction(TARIFF_BYTES, extraction)

    assert result.eligible is False
    assert "invalid_effective_interval" in result.reasons


def test_invoice_350_versus_recorded_250_produces_700_for_seven_days():
    claim = parse_invoice_claim(INVOICE_BYTES)
    calculation = calculate_overcharge(
        recorded_rate=_valid_extraction().rate_amount,
        claimed_rate=claim.claimed_rate,
        charge_days=claim.charge_days,
    )

    assert calculation.overcharge == Decimal("700.00")
    assert calculation.should_file is True
    assert calculation.recommendation == "dispute_overcharge"


def test_valid_charge_at_250_produces_no_overcharge_finding():
    calculation = calculate_overcharge(
        recorded_rate=Decimal("250.00"),
        claimed_rate=Decimal("250.00"),
        charge_days=7,
    )

    assert calculation.overcharge == Decimal("0.00")
    assert calculation.should_file is False
    assert calculation.recommendation == "no_overcharge"


def test_invoice_claim_is_read_from_retained_bytes_not_a_filename():
    claim = parse_invoice_claim(INVOICE_BYTES)

    assert claim.claimed_rate == Decimal("350.00")
    assert claim.charge_days == 7
    with pytest.raises(ValueError):
        parse_invoice_claim(b"not json")

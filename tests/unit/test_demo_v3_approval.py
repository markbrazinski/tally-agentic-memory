"""INV-1041 (clean approval) and INV-1047 (refusal) resolve through the real
judgment core — no network, no DB. Also locks INV-1041's fixtures to the same
$90/day × 6 = $540 math the seal drive uses, so the PDF, the expected-claims,
and the seeded tariff clause cannot silently drift apart.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pdfplumber

from scripts.demo_v3_approval import (
    CHARGE_DATES,
    DAILY_RATE_MINOR,
    TOTAL_MINOR,
)
from src.core.judgment import (
    DayInput,
    ReasonCode,
    RecommendationType,
    resolve_recommendation,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "demo"


def test_inv_1041_resolves_approve_for_payment_zero_discrepancy_540():
    # Clean invoice: every charged day PRESENT_VERIFIED, invoice rate == applicable.
    days = [
        DayInput(d, DAILY_RATE_MINOR, DAILY_RATE_MINOR, "USD",
                 "PRESENT_VERIFIED", True)
        for d in CHARGE_DATES
    ]
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.APPROVE_FOR_PAYMENT
    assert rec.disputed_amount_minor == 0
    assert all(j.discrepancy_minor == 0 for j in rec.judgments)
    assert rec.supported_amount_minor == TOTAL_MINOR == 54000  # $540.00
    assert rec.claimed_amount_minor == 54000


def test_inv_1047_resolves_request_evidence_rule_not_verified():
    # Refusal: full coverage but NO applicable rate (governing tariff absent).
    days = [
        DayInput(date(2026, 6, d), 12500, None, "USD", "PRESENT_VERIFIED", True)
        for d in range(8, 15)
    ]
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.REQUEST_EVIDENCE
    assert ReasonCode.RULE_NOT_VERIFIED in rec.reason_codes


def test_inv_1041_fixtures_are_consistent_with_the_540_math():
    expected = json.loads((FIXTURE_DIR / "INV-1041.expected-claims.json").read_text())
    assert expected["charged_days"] == len(CHARGE_DATES) == 6
    assert expected["daily_rate"]["amount_minor"] == DAILY_RATE_MINOR == 9000
    assert expected["total"]["amount_minor"] == TOTAL_MINOR == 54000

    pdf_path = FIXTURE_DIR / "INV-1041.pdf"
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    with pdfplumber.open(pdf_path) as pdf:
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
    assert "INV-1041" in text
    assert expected["container_number"] in text  # OOLU-840112-5 (distinct shipment)
    assert "USD $90.00 per day" in text
    assert "USD $540.00" in text
    assert "Charged Days: 6" in text
    assert "FICTIONAL DEMO INVOICE" in text


def test_inv_1041_shipment_is_distinct_from_hero_and_refusal():
    recon = json.loads((FIXTURE_DIR / "INV-1041.reconstruction-events.json").read_text())
    assert recon["shipment_ref"] == "OOLU8401125"
    assert recon["shipment_ref"] not in {"TLLU4829317", "MSCU7011453"}
    # Boundary events only — no per-day terminal-access heartbeat.
    types = {e["event_type"] for e in recon["events"]}
    assert "TERMINAL_ACCESS_SNAPSHOT" not in types
    assert {"DISCHARGED", "AVAILABLE", "FREE_TIME_START", "FREE_TIME_END",
            "GATE_OUT"} <= types

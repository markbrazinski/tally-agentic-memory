from __future__ import annotations

import json
from pathlib import Path

import pdfplumber

from src.core.intake_claims import validate_extracted_claims

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "demo"


def _fixture_pages():
    with pdfplumber.open(FIXTURE_DIR / "INV-1048.pdf") as pdf:
        return [
            {
                "text": page.extract_text(),
                "words": page.extract_words(),
                "width": page.width,
                "height": page.height,
            }
            for page in pdf.pages
        ]


def _raw_claims():
    """Claims whose excerpts are verbatim substrings of the designed hero PDF.

    Anchoring is exact-match by design (an excerpt that is not literally in the
    source makes the field UNVERIFIED), so these quotes track the document's real
    wording -- mostly its REMITTANCE NOTES prose, which restates every fact.
    """
    return {
        "claims": {
            "invoice_number": {
                "value": "INV-1048",
                "text_excerpt": "Reference invoice INV-1048",
                "page_number": 1,
            },
            "container_number": {
                "value": "TLLU-482931-7",
                "text_excerpt": "OAK-77421 TLLU-482931-7",
                "page_number": 1,
            },
            "bill_of_lading": {
                "value": "OAK-77421",
                "text_excerpt": "bill of lading OAK-77421",
                "page_number": 1,
            },
            "charge_type": {
                "value": "Demurrage",
                "text_excerpt": "Demurrage assessed at USD $350.00 per day",
                "page_number": 1,
            },
            "period_start": {
                "value": "June 8, 2026",
                "text_excerpt": "covering June 8, 2026 through June 14, 2026.",
                "page_number": 1,
            },
            "period_end": {
                "value": "June 14, 2026",
                "text_excerpt": "through June 14, 2026.",
                "page_number": 1,
            },
            "charged_days": {
                "value": 7,
                "text_excerpt": "per day for 7 days,",
                "page_number": 1,
            },
            "daily_rate": {
                "value": "$350.00",
                "text_excerpt": "assessed at USD $350.00 per day",
                "page_number": 1,
            },
            "total": {
                "value": "$2,450.00",
                "text_excerpt": "Subtotal $2,450.00",
                "page_number": 1,
            },
            "issued_date": {
                "value": "June 22, 2026",
                "text_excerpt": "June 22, 2026 USD",
                "page_number": 1,
            },
        }
    }


def test_claims_validate_to_expected_manifest_with_real_pdf_anchors():
    result = validate_extracted_claims(_raw_claims(), _fixture_pages())
    expected = json.loads(
        (FIXTURE_DIR / "INV-1048.expected-claims.json").read_text()
    )

    assert result.passed
    claims = {claim.field_name: claim for claim in result.claims}
    assert claims["container_number"].normalized_value == "TLLU4829317"
    assert claims["bill_of_lading"].normalized_value == expected["bill_of_lading"]
    assert claims["period_start"].normalized_value == expected["charge_period"]["start"]
    assert claims["period_end"].normalized_value == expected["charge_period"]["end"]
    assert claims["charged_days"].normalized_value == expected["charged_days"]
    assert claims["daily_rate"].amount_minor == expected["daily_rate"]["amount_minor"]
    assert claims["total"].amount_minor == expected["total"]["amount_minor"]
    assert all(
        claim.anchor.bounding_box["x1"] > claim.anchor.bounding_box["x0"]
        for claim in result.claims
    )


def test_unanchored_model_value_is_rejected():
    raw = _raw_claims()
    raw["claims"]["total"]["text_excerpt"] = "Subtotal $9,999.00"

    result = validate_extracted_claims(raw, _fixture_pages())

    assert not result.passed
    assert "UNANCHORED_TOTAL" in result.issue_codes


def test_arithmetic_mismatch_is_rejected_even_when_all_values_are_anchored():
    raw = _raw_claims()
    raw["claims"]["charged_days"]["value"] = 6

    result = validate_extracted_claims(raw, _fixture_pages())

    assert "CHARGE_PERIOD_DAY_COUNT_MISMATCH" in result.issue_codes
    assert "CLAIMED_TOTAL_ARITHMETIC_MISMATCH" in result.issue_codes

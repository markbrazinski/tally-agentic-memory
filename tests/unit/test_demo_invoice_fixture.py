from __future__ import annotations

import json
from pathlib import Path

import pdfplumber

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "demo"


def test_representative_invoice_is_a_real_text_pdf_matching_expected_claims():
    pdf_path = FIXTURE_DIR / "INV-1048.pdf"
    expected = json.loads(
        (FIXTURE_DIR / "INV-1048.expected-claims.json").read_text()
    )

    assert pdf_path.read_bytes().startswith(b"%PDF-")
    with pdfplumber.open(pdf_path) as pdf:
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
        words = pdf.pages[0].extract_words()

    assert len(words) > 20
    assert expected["invoice_number"] in text
    assert expected["container_number"] in text
    assert expected["bill_of_lading"] in text
    assert "June 8, 2026 through June 14, 2026" in text
    assert "Charged Days: 7" in text
    assert "USD $350.00 per day" in text
    assert "USD $2,450.00" in text
    assert "Issued: June 22, 2026" in text
    assert "FICTIONAL DEMO INVOICE" in text

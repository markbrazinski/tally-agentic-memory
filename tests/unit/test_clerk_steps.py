"""Unit tests for src/core/clerk_steps.py (Clerk steps 2-3, pure functions).

No mocks needed - these are pure functions per TDD §4's design law ("the
LLM reads and writes; Python decides"). Covers bundle-0.md's B0-S2 named
test requirement: "tests on steps 2-3 (pure)."
"""

from __future__ import annotations

from datetime import date

from src.core.clerk_steps import check_presence, check_timing
from src.core.fields import FIELD_KEYS


def _verbatim_entry(text: str) -> dict:
    return {"value": text, "verbatim": text, "page": 1, "confidence": 0.9}


# --- Step 2: presence check ---


def test_check_presence_returns_one_result_per_canon_field():
    results = check_presence({})
    assert len(results) == 13
    assert {r.key for r in results} == set(FIELD_KEYS)


def test_check_presence_marks_field_present_when_verbatim_exists():
    extracted = {"port_of_discharge": _verbatim_entry("Northport, CA")}
    results = check_presence(extracted)
    result = next(r for r in results if r.key == "port_of_discharge")
    assert result.present is True
    assert result.how == "verbatim"


def test_check_presence_marks_field_missing_when_absent():
    results = check_presence({})
    result = next(r for r in results if r.key == "port_of_discharge")
    assert result.present is False
    assert result.how == "missing"


def test_check_presence_marks_field_missing_when_no_verified_verbatim():
    """An entry with a value but no verbatim (failed the anti-hallucination
    gate in step 1) must not count as present."""
    extracted = {"port_of_discharge": {"value": "Northport, CA", "verbatim": None}}
    results = check_presence(extracted)
    result = next(r for r in results if r.key == "port_of_discharge")
    assert result.present is False
    assert result.how == "missing"


def test_proper_party_basis_explicit_verbatim_wins_over_implicit_heuristic():
    extracted = {
        "proper_party_basis": _verbatim_entry("Consignee is the proper party per contract."),
    }
    results = check_presence(extracted, billed_party_name="Meridian Home & Hardware")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is True
    assert result.how == "verbatim"


def test_proper_party_basis_implicit_consignee_label_match():
    extracted = {
        "port_of_discharge": _verbatim_entry(
            "Consignee: Meridian Home & Hardware, Northport, CA"
        ),
    }
    results = check_presence(extracted, billed_party_name="Meridian Home & Hardware")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is True
    assert result.how == "implicit:consignee_label"


def test_proper_party_basis_label_found_but_name_mismatch():
    extracted = {
        "port_of_discharge": _verbatim_entry("Consignee: Some Other Company Inc."),
    }
    results = check_presence(extracted, billed_party_name="Meridian Home & Hardware")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is False
    assert result.how == "label_mismatch"


def test_proper_party_basis_short_name_does_not_false_positive_inside_longer_company_name():
    """Regression test: a raw substring check let a short/generic
    billed_party_name (e.g. "Home") false-positive-match as a fragment
    inside an unrelated longer, ampersand-joined company name (e.g.
    "Meridian Home & Hardware") that happens to contain it as a word.
    The billed party here is genuinely "Home" (a different, unrelated
    entity) - it must not match just because the substring appears."""
    extracted = {
        "port_of_discharge": _verbatim_entry(
            "Consignee: Meridian Home & Hardware, Northport, CA"
        ),
    }
    results = check_presence(extracted, billed_party_name="Home")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is False
    assert result.how == "label_mismatch"


def test_proper_party_basis_no_label_anywhere_is_missing():
    extracted = {
        "port_of_discharge": _verbatim_entry("Northport, CA"),
    }
    results = check_presence(extracted, billed_party_name="Meridian Home & Hardware")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is False
    assert result.how == "missing"


def test_proper_party_basis_scans_all_entries_not_just_the_first_label_hit():
    """A mismatched label in one field must not short-circuit the scan -
    the heuristic should still find a genuine match in a later entry."""
    extracted = {
        "a": _verbatim_entry("Consignee: Wrong Corp"),
        "b": _verbatim_entry("Shipper: Meridian Home & Hardware"),
    }
    results = check_presence(extracted, billed_party_name="Meridian Home & Hardware")
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is True
    assert result.how == "implicit:consignee_label"


def test_proper_party_basis_no_billed_party_name_is_missing_not_a_crash():
    extracted = {"port_of_discharge": _verbatim_entry("Consignee: Meridian Home & Hardware")}
    results = check_presence(extracted, billed_party_name=None)
    result = next(r for r in results if r.key == "proper_party_basis")
    assert result.present is False
    assert result.how == "missing"


# --- Step 3: timing check ---


def test_check_timing_iso_dates_within_window():
    result = check_timing("2026-05-01", "2026-04-15")
    assert result.ambiguous is False
    assert result.invoice_date == date(2026, 5, 1)
    assert result.last_charge_date == date(2026, 4, 15)
    assert result.days == 16
    assert result.within_30 is True


def test_check_timing_iso_dates_outside_window_is_defective_regardless_of_completeness():
    result = check_timing("2026-06-01", "2026-04-15")
    assert result.ambiguous is False
    assert result.days == 47
    assert result.within_30 is False


def test_check_timing_exactly_30_days_is_within_window():
    result = check_timing("2026-05-15", "2026-04-15")
    assert result.days == 30
    assert result.within_30 is True


def test_check_timing_dmy_hint_parses_correctly():
    # 04/05/2026 with DMY hint = May 4, not April 5
    result = check_timing("04/05/2026", "01/04/2026", date_format_hint="DMY")
    assert result.ambiguous is False
    assert result.invoice_date == date(2026, 5, 4)
    assert result.last_charge_date == date(2026, 4, 1)


def test_check_timing_mdy_hint_parses_correctly():
    # 04/05/2026 with MDY hint = April 5, not May 4
    result = check_timing("04/05/2026", "03/15/2026", date_format_hint="MDY")
    assert result.ambiguous is False
    assert result.invoice_date == date(2026, 4, 5)
    assert result.last_charge_date == date(2026, 3, 15)


def test_check_timing_ambiguous_numeric_date_with_no_hint_is_never_guessed():
    result = check_timing("04/05/2026", "01/04/2026", date_format_hint=None)
    assert result.ambiguous is True
    assert result.within_30 is None  # never a confident wrong verdict


def test_check_timing_missing_invoice_date_is_ambiguous():
    result = check_timing(None, "2026-04-15")
    assert result.ambiguous is True


def test_check_timing_missing_last_charge_date_is_ambiguous():
    result = check_timing("2026-05-01", None)
    assert result.ambiguous is True


def test_check_timing_unparseable_garbage_is_ambiguous_not_a_crash():
    result = check_timing("not a date", "2026-04-15")
    assert result.ambiguous is True


def test_check_timing_invoice_date_before_last_charge_date_is_ambiguous_not_a_confident_pass():
    """Regression test: a negative day delta (invoice_date earlier than
    last_charge_date - a data-entry error or swapped-field bug upstream)
    previously satisfied `days <= WINDOW_DAYS` trivially, silently
    producing within_30=True/ambiguous=False for a nonsensical date
    relationship. Must route to NEEDS_REVIEW instead, same as any other
    case the check can't confidently resolve."""
    result = check_timing("2026-04-01", "2026-05-01")
    assert result.days == -30
    assert result.ambiguous is True
    assert result.within_30 is None

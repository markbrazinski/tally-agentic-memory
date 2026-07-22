"""Clerk steps 2-3: presence check and timing check. Pure functions, no I/O.

Per TDD §4's design law: "the LLM reads and writes; Python decides." Both
steps operate on step 1's already-extracted, already-verbatim-gated
`extracted` dict - nothing here calls Bedrock or touches a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from dateutil import parser as dateutil_parser

from src.core.fields import FIELDS_541_6

PROPER_PARTY_BASIS_FIELD = "proper_party_basis"
BILLED_PARTY_LABELS = ("Consignee:", "Shipper:", "Contract party:")

WINDOW_DAYS = 30


@dataclass(frozen=True)
class FieldResult:
    key: str
    present: bool
    how: str  # "verbatim" | "implicit:consignee_label" | "label_mismatch" | "missing"


def check_presence(
    extracted: dict, *, billed_party_name: str | None = None
) -> tuple[FieldResult, ...]:
    """Step 2: for each of the 13 fields, is it present, and how do we know.

    `extracted` is step 1's output shape: {field_key: {"value":..., "verbatim":...,
    ...}} for fields the anti-hallucination gate verified, or absent/None
    for fields it didn't. `billed_party_name` is the invoice's billed-party
    name (from a field already verified elsewhere in `extracted`, e.g.
    invoice header) - used only for the proper-party-basis heuristic below.
    """
    results = []
    for spec in FIELDS_541_6:
        entry = extracted.get(spec.key)
        has_verbatim = bool(entry and entry.get("verbatim"))

        if spec.key == PROPER_PARTY_BASIS_FIELD and not has_verbatim:
            results.append(_check_proper_party_implicit(extracted, billed_party_name))
            continue

        if has_verbatim:
            results.append(FieldResult(key=spec.key, present=True, how="verbatim"))
        else:
            results.append(FieldResult(key=spec.key, present=False, how="missing"))
    return tuple(results)


def _billed_party_name_appears_standalone(billed_party_name: str, verbatim: str) -> bool:
    """A real match, not a fragment of a longer name. Raw `in` substring
    matching lets a short/generic billed_party_name (e.g. "Home") false-
    positive-match inside an unrelated longer company name that happens
    to contain it as a word (e.g. "Meridian Home & Hardware"). Bounding
    on non-alphanumeric AND rejecting a trailing "& <more text>" (looking
    past any whitespace, not just the immediately adjacent character)
    catches exactly that ampersand-joined-company-name fragment case,
    while still matching genuine standalone short names."""
    pattern = (
        r"(?<![A-Za-z0-9])" + re.escape(billed_party_name) + r"(?![A-Za-z0-9])(?!\s*&)"
    )
    return bool(re.search(pattern, verbatim))


def _check_proper_party_implicit(
    extracted: dict, billed_party_name: str | None
) -> FieldResult:
    """Adversarial finding #1: field 7 is routinely satisfied implicitly -
    the invoice addresses a party labeled Consignee:/Shipper:/Contract
    party: matching the billed party, with no explicit basis sentence.
    Never an LLM judgment - a label-adjacency scan over already-verified
    verbatim spans only."""
    if not billed_party_name:
        return FieldResult(key=PROPER_PARTY_BASIS_FIELD, present=False, how="missing")

    found_label = False
    for entry in extracted.values():
        if not entry:
            continue
        verbatim = entry.get("verbatim", "") or ""
        for label in BILLED_PARTY_LABELS:
            if label in verbatim:
                found_label = True
                if _billed_party_name_appears_standalone(billed_party_name, verbatim):
                    return FieldResult(
                        key=PROPER_PARTY_BASIS_FIELD,
                        present=True,
                        how="implicit:consignee_label",
                    )
    if found_label:
        return FieldResult(key=PROPER_PARTY_BASIS_FIELD, present=False, how="label_mismatch")
    return FieldResult(key=PROPER_PARTY_BASIS_FIELD, present=False, how="missing")


@dataclass(frozen=True)
class WindowResult:
    invoice_date: date | None
    last_charge_date: date | None
    days: int | None
    within_30: bool | None
    ambiguous: bool


# Tried in order: ISO 8601 first (unambiguous), then the carrier's
# date_format_hint via dateutil with dayfirst set explicitly. A hint of
# None with a non-ISO, non-long-form date is never guessed.
def _parse_date(raw: str, *, date_format_hint: str | None) -> date | None:
    raw = raw.strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass

    if date_format_hint == "DMY":
        try:
            return dateutil_parser.parse(raw, dayfirst=True).date()
        except (ValueError, OverflowError):
            return None
    if date_format_hint == "MDY":
        try:
            return dateutil_parser.parse(raw, dayfirst=False).date()
        except (ValueError, OverflowError):
            return None
    if date_format_hint == "ISO":
        return None  # already tried fromisoformat above; a hint of ISO that failed stays failed

    # No hint: only accept unambiguous long forms (dateutil's fuzzy=False
    # default already rejects genuinely fuzzy text) - but a numeric
    # short-form date like "04/05/2026" is ambiguous without a hint and
    # must not be guessed either direction.
    if _looks_like_ambiguous_numeric_date(raw):
        return None
    try:
        return dateutil_parser.parse(raw).date()
    except (ValueError, OverflowError):
        return None


def _looks_like_ambiguous_numeric_date(raw: str) -> bool:
    import re

    return bool(re.match(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$", raw))


def check_timing(
    invoice_date_raw: str | None,
    last_charge_date_raw: str | None,
    *,
    date_format_hint: str | None = None,
) -> WindowResult:
    """Step 3: 30-day window check. Ambiguous dates never get guessed -
    they produce ambiguous=True, which the caller must map to
    NEEDS_REVIEW, never a confident wrong verdict."""
    if not invoice_date_raw or not last_charge_date_raw:
        return WindowResult(
            invoice_date=None, last_charge_date=None, days=None, within_30=None, ambiguous=True
        )

    invoice_date = _parse_date(invoice_date_raw, date_format_hint=date_format_hint)
    last_charge_date = _parse_date(last_charge_date_raw, date_format_hint=date_format_hint)

    if invoice_date is None or last_charge_date is None:
        return WindowResult(
            invoice_date=invoice_date,
            last_charge_date=last_charge_date,
            days=None,
            within_30=None,
            ambiguous=True,
        )

    delta: timedelta = invoice_date - last_charge_date
    days = delta.days

    if days < 0:
        # Invoice dated BEFORE the last charge date - a nonsensical date
        # relationship (data-entry error, swapped fields upstream). Never
        # confidently pass this as "within window" just because a
        # negative number satisfies days <= WINDOW_DAYS - route to
        # NEEDS_REVIEW instead, same as any other unparseable case.
        return WindowResult(
            invoice_date=invoice_date,
            last_charge_date=last_charge_date,
            days=days,
            within_30=None,
            ambiguous=True,
        )

    return WindowResult(
        invoice_date=invoice_date,
        last_charge_date=last_charge_date,
        days=days,
        within_30=days <= WINDOW_DAYS,
        ambiguous=False,
    )

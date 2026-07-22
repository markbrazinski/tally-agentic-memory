"""The 13-field canon (FIELDS_541_6): pure data, zero external deps.

Per TDD §4: citations rendered on verdict chips, subsection numbering
verified against the live eCFR in B2 (one session task, not this one -
the field list below matches the TDD's own table verbatim, not an
independently re-derived reading of 46 CFR 541.6).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    key: str
    requirement: str
    cite: str | None = None


FIELDS_541_6: tuple[FieldSpec, ...] = (
    FieldSpec("date_container_available", "Date container was made available"),
    FieldSpec("port_of_discharge", "Port of discharge"),
    FieldSpec("container_numbers", "Container number(s)"),
    FieldSpec(
        "earliest_return_date",
        "Earliest return date (export) — N/A allowed for import demurrage",
    ),
    FieldSpec("free_time_days", "Allowed free time in days"),
    FieldSpec("free_time_start", "Free time start date"),
    FieldSpec(
        "proper_party_basis",
        "Basis that charged party is the proper party",
        cite="541.6(a)(7)",
    ),
    FieldSpec("free_time_end", "Free time end date"),
    FieldSpec("applicable_rule", "Tariff rule the daily rate is based on"),
    FieldSpec("applicable_rate", "Rate(s) per that rule"),
    FieldSpec("total_amount_due", "Total amount due"),
    FieldSpec("contact_for_disputes", "Contact for questions / mitigation requests"),
    FieldSpec(
        "certifications",
        "Statements: charges consistent with FMC rules; carrier performance "
        "did not cause or contribute",
    ),
)

FIELD_KEYS: tuple[str, ...] = tuple(f.key for f in FIELDS_541_6)

assert len(FIELD_KEYS) == 13, "FIELDS_541_6 must always be exactly the 13-field canon"

"""Anti-hallucination gate: pure function, zero I/O.

Per CLAUDE.md's agent design principles: "Schema-validated LLM output,
always; extraction quotes must appear verbatim in the source or the
field is UNVERIFIED." This is the sole enforcement point of that
principle - every verbatim string a model claims must appear in the
source text (normalized whitespace) or the field drops to
value=None/verbatim=None/confidence=0.0. The model may not assert a
value it cannot quote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.fields import FIELD_KEYS


@dataclass(frozen=True)
class ExtractedField:
    value: str | None
    verbatim: str | None
    page: int | None
    confidence: float


@dataclass(frozen=True)
class ExtractionResult:
    fields: dict[str, ExtractedField]
    invoice_no: str | None
    currency: str
    notes_footnotes: tuple[str, ...]
    is_image_only: bool


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def apply_anti_hallucination_gate(raw_result: dict, source_text: str) -> ExtractionResult:
    """Every verbatim string must appear in the source text (normalized
    whitespace) or the field drops to value=None, verbatim=None,
    confidence=0.0 - present=false, how='unverified' downstream (step 2
    never even sees a verbatim it can't trust)."""
    normalized_source = _normalize_whitespace(source_text)
    raw_fields = raw_result.get("fields", {})
    gated_fields: dict[str, ExtractedField] = {}

    for key in FIELD_KEYS:
        entry = raw_fields.get(key) or {}
        verbatim = entry.get("verbatim")
        if verbatim and _normalize_whitespace(verbatim) in normalized_source:
            gated_fields[key] = ExtractedField(
                value=entry.get("value"),
                verbatim=verbatim,
                page=entry.get("page"),
                confidence=float(entry.get("confidence", 0.0)),
            )
        else:
            # Fails the gate (or was never present) - never carry forward
            # an unverifiable quote, per the design law that the model may
            # not assert a value it cannot quote.
            gated_fields[key] = ExtractedField(value=None, verbatim=None, page=None, confidence=0.0)

    return ExtractionResult(
        fields=gated_fields,
        invoice_no=raw_result.get("invoice_no"),
        currency=raw_result.get("currency", "USD"),
        notes_footnotes=tuple(raw_result.get("notes_footnotes", [])),
        is_image_only=False,  # text-channel-only this session; always False
    )

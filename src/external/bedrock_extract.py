"""Clerk step 1: field extraction via Bedrock Claude Sonnet 4.6.

Text-channel-only this session (bundle-0.md B0-S2 pre-flight answer: image
channel is Bundle 2 scope). pdfplumber text extraction feeds one of the two
extractors below; the anti-hallucination gate itself (every verbatim string
must appear in the source text, normalized whitespace, or the field drops
to present=false, how='unverified') is pure and lives in src/core/extraction.py
- re-exported here for callers that only need to import this module. This
file owns the impure half: the real Bedrock network call and its stub.

Two implementations behind one interface (ExtractorProtocol): BedrockExtractor
(the real thing, confirmed working live 2026-07-08 via a real InvokeModel
call through the us.anthropic.claude-sonnet-4-6 cross-region inference
profile) and CannedResponseExtractor (the documented fallback bundle-0.md's
own pre-flight note allows "if access lags"). Bedrock access is live and
confirmed this session, so BedrockExtractor is what's actually wired up -
the stub exists so the interface boundary is real and tested, not
theoretical.
"""

from __future__ import annotations

import json
from typing import Protocol

import boto3

from src.core.extraction import ExtractedField, ExtractionResult, apply_anti_hallucination_gate
from src.core.fields import FIELD_KEYS, FIELDS_541_6

INFERENCE_PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = "us-east-1"
MIN_TEXT_DENSITY_CHARS_PER_PAGE = 200

__all__ = [
    "ExtractedField",
    "ExtractionResult",
    "apply_anti_hallucination_gate",
    "BedrockExtractor",
    "CannedResponseExtractor",
    "ExtractorProtocol",
    "is_image_only",
    "extracted_result_to_dict",
]


class ExtractorProtocol(Protocol):
    def extract(
        self, pdf_text: str, *, date_format_hint: str | None = None
    ) -> dict: ...  # returns the raw {"fields": {...}, "invoice_no": ..., ...} shape


def _build_system_prompt(date_format_hint: str | None) -> str:
    field_lines = "\n".join(f"- {f.key}: {f.requirement}" for f in FIELDS_541_6)
    hint_line = (
        f"This carrier's date format is {date_format_hint}."
        if date_format_hint
        else "No date format hint is available for this carrier."
    )
    return (
        "You are extracting fields from a demurrage/detention invoice for FMC "
        "rule 541.6 compliance review. Extract exactly these 13 fields:\n"
        f"{field_lines}\n\n"
        f"{hint_line}\n\n"
        "For every field: return a VERBATIM quote from the source text (never "
        "paraphrase), the page number, and a confidence 0.0-1.0. If a field is "
        "not present in the text, return null for value/verbatim - never guess "
        "or infer a value that isn't directly quotable."
    )


class BedrockExtractor:
    """The real thing: one Bedrock InvokeModel call via the cross-region
    inference profile, tool-use JSON output, validated against FIELD_KEYS."""

    def __init__(self, client=None):
        self._client = client or boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def extract(self, pdf_text: str, *, date_format_hint: str | None = None) -> dict:
        system_prompt = _build_system_prompt(date_format_hint)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": f"Invoice text:\n\n{pdf_text}"}],
            "tools": [_EXTRACTION_TOOL_SCHEMA],
            "tool_choice": {"type": "tool", "name": "extract_invoice_fields"},
        }
        response = self._client.invoke_model(
            modelId=INFERENCE_PROFILE_ID,
            body=json.dumps(body),
        )
        payload = json.loads(response["body"].read())
        for block in payload.get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        raise ValueError("Bedrock response had no tool_use block")


class CannedResponseExtractor:
    """Fallback behind the same interface, per bundle-0.md's own pre-flight
    note: "if access lags, continue on a canned-response stub." Every field
    comes back null/unverified - never a fabricated value - so anything
    downstream sees a legitimately-empty extraction, not a fake one."""

    def extract(self, pdf_text: str, *, date_format_hint: str | None = None) -> dict:
        return {
            "fields": {key: {"value": None, "verbatim": None, "page": None, "confidence": 0.0}
                       for key in FIELD_KEYS},
            "invoice_no": None,
            "currency": "USD",
            "notes_footnotes": [],
        }


_EXTRACTION_TOOL_SCHEMA = {
    "name": "extract_invoice_fields",
    "description": "Return the 13 extracted fields with verbatim quotes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "properties": {
                    key: {
                        "type": "object",
                        "properties": {
                            "value": {"type": ["string", "null"]},
                            "verbatim": {"type": ["string", "null"]},
                            "page": {"type": ["integer", "null"]},
                            "confidence": {"type": "number"},
                        },
                        "required": ["value", "verbatim", "confidence"],
                    }
                    for key in FIELD_KEYS
                },
            },
            "invoice_no": {"type": ["string", "null"]},
            "currency": {"type": "string"},
            "notes_footnotes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["fields"],
    },
}


def is_image_only(pdf_text: str, page_count: int) -> bool:
    if page_count <= 0:
        return True
    return (len(pdf_text) / page_count) < MIN_TEXT_DENSITY_CHARS_PER_PAGE


def extracted_result_to_dict(result: ExtractionResult) -> dict:
    """Shape matching invoices.extracted's JSONB column and the
    GET_invoices_id_fields.json contract fixture."""
    return {
        "fields": {
            key: {
                "value": f.value,
                "verbatim": f.verbatim,
                "page": f.page,
                "confidence": f.confidence,
            }
            for key, f in result.fields.items()
        },
        "invoice_no": result.invoice_no,
        "currency": result.currency,
        "notes_footnotes": list(result.notes_footnotes),
        "is_image_only": result.is_image_only,
    }

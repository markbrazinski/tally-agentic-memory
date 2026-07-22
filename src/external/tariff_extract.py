"""Bedrock adapter for Gate 1 tariff-fact extraction.

The adapter returns a schema-validated proposal. Eligibility remains a pure,
deterministic decision in :mod:`src.core.receipt`.
"""

from __future__ import annotations

import json
from typing import Protocol

import boto3

from src.core.receipt import TariffExtraction

INFERENCE_PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = "us-east-1"


class TariffExtractorProtocol(Protocol):
    def extract(self, source_text: str) -> TariffExtraction: ...


class BedrockTariffExtractor:
    """One tool-use extraction call; no eligibility judgment."""

    def __init__(self, client=None):
        self._client = client or boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def extract(self, source_text: str) -> TariffExtraction:
        response = self._client.invoke_model(
            modelId=INFERENCE_PROFILE_ID,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 2048,
                    "system": (
                        "Extract one tariff rate fact from the supplied retained source. "
                        "Return exact source text, never a paraphrase. Do not infer a rate, "
                        "currency, unit, date, or locator that is absent. The caller will "
                        "independently reject any output not present in the source."
                    ),
                    "messages": [
                        {"role": "user", "content": f"Retained tariff source:\n\n{source_text}"}
                    ],
                    "tools": [TARIFF_EXTRACTION_TOOL],
                    "tool_choice": {"type": "tool", "name": "extract_tariff_fact"},
                }
            ),
        )
        payload = json.loads(response["body"].read())
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "extract_tariff_fact":
                return TariffExtraction.from_mapping(block["input"])
        raise ValueError("Bedrock tariff extraction had no extract_tariff_fact tool result")


TARIFF_EXTRACTION_TOOL = {
    "name": "extract_tariff_fact",
    "description": "Extract a tariff rate and its exact supporting clause.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rate_amount": {"type": "string", "description": "Decimal amount only"},
            "rate_currency": {"type": "string", "description": "ISO 4217 code"},
            "rate_unit": {"type": "string", "enum": ["per_day"]},
            "rate_text": {
                "type": "string",
                "description": "Exact rate phrase appearing inside clause_text",
            },
            "effective_from": {"type": "string", "format": "date"},
            "effective_to": {"type": ["string", "null"], "format": "date"},
            "clause_text": {"type": "string", "description": "Verbatim retained-source text"},
            "source_locator": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "rate_amount",
            "rate_currency",
            "rate_unit",
            "rate_text",
            "effective_from",
            "effective_to",
            "clause_text",
            "source_locator",
            "confidence",
        ],
    },
}

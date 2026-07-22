from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.external.tariff_extract import INFERENCE_PROFILE_ID, BedrockTariffExtractor

VALID_INPUT = {
    "rate_amount": "250.00",
    "rate_currency": "USD",
    "rate_unit": "per_day",
    "rate_text": "USD $250.00 per day",
    "effective_from": "2026-07-01",
    "effective_to": None,
    "clause_text": "The rate is USD $250.00 per day.",
    "source_locator": "Section 4",
    "confidence": 0.98,
}


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append({**kwargs, "body": json.loads(kwargs["body"])})
        return {"body": FakeBody(self.payload)}


def test_bedrock_tariff_extractor_returns_validated_structured_fact():
    client = FakeClient(
        {"content": [{"type": "tool_use", "name": "extract_tariff_fact", "input": VALID_INPUT}]}
    )

    result = BedrockTariffExtractor(client).extract("The rate is USD $250.00 per day.")

    assert result.rate_amount == Decimal("250.00")
    assert result.rate_unit == "per_day"
    assert client.calls[0]["modelId"] == INFERENCE_PROFILE_ID
    assert client.calls[0]["body"]["tool_choice"]["name"] == "extract_tariff_fact"


def test_bedrock_tariff_extractor_rejects_missing_required_fields():
    client = FakeClient(
        {"content": [{"type": "tool_use", "name": "extract_tariff_fact", "input": {}}]}
    )

    with pytest.raises(ValueError, match="missing tariff extraction fields"):
        BedrockTariffExtractor(client).extract("source")


def test_bedrock_tariff_extractor_rejects_non_tool_response():
    client = FakeClient({"content": [{"type": "text", "text": "I think it is 250"}]})

    with pytest.raises(ValueError, match="no extract_tariff_fact"):
        BedrockTariffExtractor(client).extract("source")

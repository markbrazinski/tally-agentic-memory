"""Unit tests for src/external/bedrock_extract.py.

Per CLAUDE.md ("All external calls mocked in tests; zero network calls in
the test suite"), BedrockExtractor's real invoke_model call is exercised
against a mocked boto3 client, never the live Bedrock API - the live
call itself was confirmed manually this session (2026-07-08, real
InvokeModel response via the us. cross-region inference profile), which
is what proves the schema this test's mock assumes is accurate.
"""

from __future__ import annotations

import json

from src.core.fields import FIELD_KEYS
from src.external.bedrock_extract import (
    BedrockExtractor,
    CannedResponseExtractor,
    apply_anti_hallucination_gate,
    extracted_result_to_dict,
    is_image_only,
)


class FakeStreamingBody:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()


class FakeBedrockClient:
    def __init__(self, tool_input: dict):
        self._tool_input = tool_input
        self.invoke_calls: list[dict] = []

    def invoke_model(self, modelId: str, body: str):
        self.invoke_calls.append({"modelId": modelId, "body": json.loads(body)})
        return {
            "body": FakeStreamingBody(
                {"content": [{"type": "tool_use", "name": "extract_invoice_fields",
                              "input": self._tool_input}]}
            )
        }


# --- anti-hallucination gate ---


def test_gate_keeps_field_whose_verbatim_appears_in_source_text():
    source_text = "Port of discharge: Harbor City, CA. Total due: $1,050.00."
    raw = {"fields": {**{k: {"value": None, "verbatim": None, "confidence": 0.0}
                          for k in FIELD_KEYS},
                       "port_of_discharge": {
                           "value": "Harbor City, CA",
                           "verbatim": "Port of discharge: Harbor City, CA",
                           "page": 1,
                           "confidence": 0.95,
                       }}}
    result = apply_anti_hallucination_gate(raw, source_text)
    assert result.fields["port_of_discharge"].value == "Harbor City, CA"
    assert result.fields["port_of_discharge"].confidence == 0.95


def test_gate_drops_field_whose_verbatim_does_not_appear_in_source_text():
    source_text = "Total due: $1,050.00."
    raw = {"fields": {**{k: {"value": None, "verbatim": None, "confidence": 0.0}
                          for k in FIELD_KEYS},
                       "port_of_discharge": {
                           "value": "Harbor City, CA",
                           "verbatim": "Port of discharge: Harbor City, CA",  # NOT in source
                           "page": 1,
                           "confidence": 0.95,
                       }}}
    result = apply_anti_hallucination_gate(raw, source_text)
    field = result.fields["port_of_discharge"]
    assert field.value is None
    assert field.verbatim is None
    assert field.confidence == 0.0


def test_gate_matches_verbatim_with_normalized_whitespace():
    """A model quote with different line-wrapping than the source should
    still pass the gate - it's the same text, just re-flowed."""
    source_text = "Port of\ndischarge:   Harbor   City, CA."
    raw = {"fields": {**{k: {"value": None, "verbatim": None, "confidence": 0.0}
                          for k in FIELD_KEYS},
                       "port_of_discharge": {
                           "value": "Harbor City, CA",
                           "verbatim": "Port of discharge: Harbor City, CA",
                           "page": 1,
                           "confidence": 0.9,
                       }}}
    result = apply_anti_hallucination_gate(raw, source_text)
    assert result.fields["port_of_discharge"].value == "Harbor City, CA"


def test_gate_field_absent_from_raw_result_stays_unverified():
    raw = {"fields": {}}
    result = apply_anti_hallucination_gate(raw, "some text")
    for key in FIELD_KEYS:
        assert result.fields[key].value is None


def test_gate_result_always_returns_all_13_canon_fields():
    raw = {"fields": {}}
    result = apply_anti_hallucination_gate(raw, "some text")
    assert set(result.fields.keys()) == set(FIELD_KEYS)


def test_gate_is_image_only_always_false_this_session():
    raw = {"fields": {}}
    result = apply_anti_hallucination_gate(raw, "some text")
    assert result.is_image_only is False


# --- is_image_only ---


def test_is_image_only_true_below_density_threshold():
    assert is_image_only(pdf_text="short", page_count=1) is True


def test_is_image_only_false_above_density_threshold():
    dense_text = "x" * 300
    assert is_image_only(pdf_text=dense_text, page_count=1) is False


def test_is_image_only_true_when_zero_pages():
    assert is_image_only(pdf_text="", page_count=0) is True


# --- CannedResponseExtractor ---


def test_canned_response_extractor_returns_all_null_fields():
    extractor = CannedResponseExtractor()
    raw = extractor.extract("any text")
    assert set(raw["fields"].keys()) == set(FIELD_KEYS)
    for entry in raw["fields"].values():
        assert entry["value"] is None
        assert entry["verbatim"] is None


def test_canned_response_extractor_never_fabricates_a_verbatim():
    """The stub must never produce a quote that could accidentally pass
    the anti-hallucination gate - it should always gate to unverified."""
    extractor = CannedResponseExtractor()
    raw = extractor.extract("Port of discharge: Harbor City, CA")
    gated = apply_anti_hallucination_gate(raw, "Port of discharge: Harbor City, CA")
    for field in gated.fields.values():
        assert field.value is None


# --- BedrockExtractor (mocked client) ---


def test_bedrock_extractor_calls_invoke_model_with_correct_model_id():
    tool_input = {"fields": {}, "invoice_no": "INV-001", "currency": "USD",
                  "notes_footnotes": []}
    client = FakeBedrockClient(tool_input)
    extractor = BedrockExtractor(client=client)

    extractor.extract("some invoice text")

    assert len(client.invoke_calls) == 1
    assert client.invoke_calls[0]["modelId"] == "us.anthropic.claude-sonnet-4-6"


def test_bedrock_extractor_returns_the_tool_use_input():
    tool_input = {"fields": {"port_of_discharge": {"value": "LA", "verbatim": "LA",
                                                     "page": 1, "confidence": 0.9}},
                  "invoice_no": "INV-002", "currency": "USD", "notes_footnotes": []}
    client = FakeBedrockClient(tool_input)
    extractor = BedrockExtractor(client=client)

    raw = extractor.extract("some invoice text")

    assert raw["invoice_no"] == "INV-002"
    assert raw["fields"]["port_of_discharge"]["value"] == "LA"


def test_bedrock_extractor_passes_date_format_hint_into_system_prompt():
    tool_input = {"fields": {}, "invoice_no": None, "currency": "USD", "notes_footnotes": []}
    client = FakeBedrockClient(tool_input)
    extractor = BedrockExtractor(client=client)

    extractor.extract("some text", date_format_hint="DMY")

    system_prompt = client.invoke_calls[0]["body"]["system"]
    assert "DMY" in system_prompt


# --- extracted_result_to_dict shape ---


def test_extracted_result_to_dict_matches_contract_fixture_shape():
    raw = {"fields": {}, "invoice_no": "INV-003", "currency": "USD", "notes_footnotes": ["a note"]}
    result = apply_anti_hallucination_gate(raw, "")
    as_dict = extracted_result_to_dict(result)

    assert set(as_dict.keys()) == {"fields", "invoice_no", "currency", "notes_footnotes",
                                     "is_image_only"}
    assert as_dict["invoice_no"] == "INV-003"
    assert as_dict["notes_footnotes"] == ["a note"]

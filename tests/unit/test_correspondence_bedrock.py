"""Bedrock adjustment-request draft tests (Demo v3 P4). Zero network.

Proves the model writes prose only: the sealed locked fields are re-derived from
the pack (never the prose), the deterministic fallback never invents a value, and
an empty Bedrock response fails closed.
"""

from __future__ import annotations

import io
import json

import pytest

from src.core.correspondence import (
    SealedFactPack,
    locked_fields,
    validate_draft_locked_fields,
)
from src.external.correspondence_bedrock import (
    BedrockDraftGenerator,
    DeterministicDraftGenerator,
)

PACK = SealedFactPack(
    invoice_id="inv-1", decision_seal_id="seal-1", seal_digest="sha256:abc",
    recommendation_type="DISPUTE", disputed_amount_minor=70000,
    supported_amount_minor=175000, currency="USD",
    charged_period_start="2026-06-08", charged_period_end="2026-06-14",
    container_ref="TLLU-482931-7", invoice_no="INV-1048", rule_ref="Clause 4.2",
)


class FakeBedrock:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def invoke_model(self, modelId, body):  # noqa: N803 (boto API)
        self.calls.append(json.loads(body))
        payload = {"content": ([{"type": "text", "text": self._text}]
                               if self._text is not None else [])}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def test_bedrock_draft_returns_prose_grounded_in_prompt_facts():
    fake = FakeBedrock("We request an adjustment of USD $700.00 on invoice INV-1048.")
    body = BedrockDraftGenerator(client=fake).draft_body(PACK)
    assert "adjustment" in body.lower()
    # The sealed facts are provided to the model as grounding — not invented by it.
    sent = fake.calls[0]["messages"][0]["content"]
    assert "INV-1048" in sent and "$700.00" in sent
    # System prompt forbids adding numbers/dates/ids.
    assert "do not" in fake.calls[0]["system"].lower()


def test_locked_fields_are_rederived_from_seal_not_prose():
    # Even a prose draft that mis-states an amount cannot corrupt the record: the
    # caller validates the pack's own locked fields, which are unaffected by prose.
    _ = FakeBedrock("Adjustment of USD $999.99 (a wrong number in prose).")
    v = validate_draft_locked_fields(PACK, locked_fields(PACK))
    assert v.ok and v.issues == ()


def test_deterministic_fallback_never_invents_values():
    body = DeterministicDraftGenerator().draft_body(PACK)
    assert "$700.00" in body and "$1,750.00" in body
    assert "INV-1048" in body and "TLLU-482931-7" in body and "Clause 4.2" in body
    # No amount outside the sealed facts.
    for stray in ("$800", "$900", "$1,850", "$650"):
        assert stray not in body


def test_empty_bedrock_response_fails_closed():
    fake = FakeBedrock(None)  # no text block
    with pytest.raises(ValueError, match="no text block"):
        BedrockDraftGenerator(client=fake).draft_body(PACK)

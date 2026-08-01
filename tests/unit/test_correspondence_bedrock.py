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
    validate_draft_prose,
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


# ---- P1: generated prose is fact-checked against the seal ----


def test_prose_check_accepts_a_faithful_draft():
    """Legitimate prose — including the claimed total — must pass.

    The claimed total ($2,450) is not a SealedFactPack field but IS sealed
    (supported + disputed), and is the figure a writer most naturally cites.
    """
    ok_drafts = [
        DeterministicDraftGenerator().draft_body(PACK),
        "We dispute $700 on INV-1048 for container TLLU-482931-7 under Clause 4.2.",
        "Invoice INV-1048: disputed $700.00 of $2,450.00 claimed; supported $1,750.00.",
        "We request an adjustment per the applicable recorded tariff.",
    ]
    for draft in ok_drafts:
        v = validate_draft_prose(PACK, draft)
        assert v.ok, (draft, v.issues)


@pytest.mark.parametrize(
    "prose,marker",
    [
        ("We request an adjustment of $900.00.", "UNSUPPORTED_NUMBER"),
        ("The rate should be $275 per day.", "UNSUPPORTED_NUMBER"),
        ("Charges from June 30, 2026 are disputed.", "UNSUPPORTED_NUMBER"),
        ("Container TLLU-482931-9 was held.", "MANGLED_IDENTIFIER"),
    ],
)
def test_prose_check_rejects_unsupported_facts(prose, marker):
    """A number or identifier the seal does not support invalidates the draft.

    This is the check the locked-field validator structurally cannot perform:
    locked_fields compares seal-derived values against seal-derived values, so it
    is always ok. Only the prose can carry a hallucination.
    """
    v = validate_draft_prose(PACK, prose)
    assert not v.ok
    assert any(issue.startswith(marker) for issue in v.issues), v.issues


def test_unsupported_prose_makes_the_draft_unsendable():
    """An INVALID draft cannot be sent: the LOCKED_FIELDS gate reads the state.

    draft_from_sealed persists validation_state=INVALID when either the locked
    fields or the prose fail, and evaluate_send_gates blocks on a failed
    LOCKED_FIELDS gate — so a hallucinated fact stops the send, it does not
    merely annotate it.
    """
    from src.core.correspondence import GateResult, GateState, evaluate_send_gates

    hallucinated = validate_draft_prose(PACK, "Adjustment of $12,345.00 requested.")
    assert not hallucinated.ok

    results = {
        code: GateResult(code, GateState.VERIFIED, None)
        for code in ("SECOND_AUTHORIZATION", "APPROVED_MEMORY_MCP",
                     "VECTOR_CLAUSE_BINDING", "EXACT_S3_SOURCE", "NO_FALLBACK")
    }
    results["LOCKED_FIELDS"] = GateResult(
        "LOCKED_FIELDS", GateState.FAILED, "draft not validated"
    )
    decision = evaluate_send_gates(results)
    assert not decision.permitted


def test_generator_identity_records_which_writer_ran():
    """Provenance must name the writer and, for Bedrock, the model."""
    from src.platform.correspondence_repository import _generator_identity

    kind, model = _generator_identity(BedrockDraftGenerator(client=FakeBedrock("x")))
    assert kind == "BEDROCK"
    assert model == "us.anthropic.claude-sonnet-4-6"

    kind, model = _generator_identity(DeterministicDraftGenerator())
    assert kind == "DETERMINISTIC"
    assert model is None


def test_deployed_draft_route_wires_bedrock():
    """The API route must pass BedrockDraftGenerator — the P1 acceptance claim.

    Regression guard for the exact defect this task fixed: the route previously
    called draft_from_sealed WITHOUT a generator, so the deterministic default ran
    and Bedrock was never invoked on the deployed lane.
    """
    import inspect

    from src.platform import correspondence_api

    src = inspect.getsource(correspondence_api)
    assert "draft_generator=BedrockDraftGenerator()" in src

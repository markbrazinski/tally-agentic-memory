"""Bedrock adjustment-request draft (Demo v3 P4). Prose only, from sealed facts.

A model may write the correspondence PROSE, but never a locked financial or
identifier field — those come verbatim from the SealedFactPack and are enforced
by core.correspondence.validate_draft_locked_fields after generation. This module
owns the impure Bedrock call and a deterministic canned fallback behind one
interface, mirroring bedrock_extract.py.

The draft is grounded strictly in the sealed fact pack passed in the prompt; the
model is instructed to state only those facts and add no new numbers, dates, or
identifiers. Whatever it returns, the caller re-derives the locked fields from
the seal (not from the prose) — so a hallucinated number cannot enter the record.
"""

from __future__ import annotations

import json
from typing import Protocol

import boto3

from src.core.correspondence import SealedFactPack

INFERENCE_PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = "us-east-1"


class DraftGeneratorProtocol(Protocol):
    def draft_body(self, pack: SealedFactPack) -> str: ...


def _dollars(minor: int, currency: str) -> str:
    return f"{currency} ${minor / 100:,.2f}"


def _fact_lines(pack: SealedFactPack) -> str:
    return (
        f"- Invoice: {pack.invoice_no}\n"
        f"- Container: {pack.container_ref}\n"
        f"- Charged period: {pack.charged_period_start} through {pack.charged_period_end}\n"
        f"- Disputed amount: {_dollars(pack.disputed_amount_minor, pack.currency)}\n"
        f"- Supported total: {_dollars(pack.supported_amount_minor, pack.currency)}\n"
        f"- Governing tariff reference: {pack.rule_ref}\n"
        f"- Recommendation: {pack.recommendation_type}"
    )


def _system_prompt() -> str:
    return (
        "You draft a concise, professional demurrage adjustment-request email body "
        "for an importer disputing a carrier invoice. Use ONLY the facts provided. "
        "Do NOT introduce any number, amount, date, container, invoice id, or rate "
        "that is not in the facts. Do not restate the facts as a bullet list; write "
        "2-4 short sentences that reference the disputed amount and the reason (the "
        "invoiced rate differs from the applicable recorded tariff). No greeting "
        "block, no signature, no subject line — body prose only."
    )


class BedrockDraftGenerator:
    """Real Bedrock InvokeModel call; returns prose only."""

    def __init__(self, client=None):
        self._client = client or boto3.client(
            "bedrock-runtime", region_name=BEDROCK_REGION
        )

    def draft_body(self, pack: SealedFactPack) -> str:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "system": _system_prompt(),
            "messages": [{
                "role": "user",
                "content": f"Facts (do not add to these):\n{_fact_lines(pack)}",
            }],
        }
        response = self._client.invoke_model(
            modelId=INFERENCE_PROFILE_ID, body=json.dumps(body)
        )
        payload = json.loads(response["body"].read())
        parts = [
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if not text:
            raise ValueError("Bedrock draft response had no text block")
        return text


class DeterministicDraftGenerator:
    """Fallback behind the same interface (no network). Prose only, derived from
    the sealed facts — never invents values. Used when Bedrock access lags; the
    locked-field validator treats both generators identically."""

    def draft_body(self, pack: SealedFactPack) -> str:
        disputed = _dollars(pack.disputed_amount_minor, pack.currency)
        supported = _dollars(pack.supported_amount_minor, pack.currency)
        return (
            f"We are requesting an adjustment of {disputed} on invoice "
            f"{pack.invoice_no} for container {pack.container_ref}. For the charged "
            f"period {pack.charged_period_start} through {pack.charged_period_end}, "
            f"the invoiced rate differs from the applicable recorded tariff "
            f"({pack.rule_ref}); the supported total is {supported}. Please review "
            f"and adjust the disputed amount accordingly."
        )

"""Pure Gate 6 contracts: sealed-record fact pack, locked-field draft
validation, and the send-gate evaluation. Zero external I/O.

The draft's financial and identifier fields are LOCKED to the sealed record — a
model may write prose but never a locked field. The send gate is all-or-nothing:
MCP, vector binding, exact source, no-fallback, second authorization, and
locked-field integrity must every one be VERIFIED, or the send is blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from src.core.receipt import canonical_json_bytes, prefixed_sha256


class GateState(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


# Every gate that must pass before a controlled send. Order is display order.
SEND_GATES = (
    "SECOND_AUTHORIZATION",
    "LOCKED_FIELDS",
    "APPROVED_MEMORY_MCP",
    "VECTOR_CLAUSE_BINDING",
    "EXACT_S3_SOURCE",
    "NO_FALLBACK",
)


@dataclass(frozen=True)
class SealedFactPack:
    """The only inputs a draft may use — copied from the sealed decision."""

    invoice_id: str
    decision_seal_id: str
    seal_digest: str
    recommendation_type: str
    disputed_amount_minor: int
    supported_amount_minor: int
    currency: str
    charged_period_start: str
    charged_period_end: str
    container_ref: str
    invoice_no: str
    rule_ref: str


def locked_fields(pack: SealedFactPack) -> dict:
    """The fields a draft must reproduce verbatim from the seal."""
    return {
        "recommendation_type": pack.recommendation_type,
        "disputed_amount_minor": pack.disputed_amount_minor,
        "supported_amount_minor": pack.supported_amount_minor,
        "currency": pack.currency,
        "charged_period_start": pack.charged_period_start,
        "charged_period_end": pack.charged_period_end,
        "container_ref": pack.container_ref,
        "invoice_no": pack.invoice_no,
        "rule_ref": pack.rule_ref,
    }


def locked_fields_digest(pack: SealedFactPack) -> str:
    return prefixed_sha256(canonical_json_bytes(locked_fields(pack)))


@dataclass(frozen=True)
class DraftValidation:
    ok: bool
    issues: tuple[str, ...]


def validate_draft_locked_fields(
    pack: SealedFactPack, draft_locked_fields: dict
) -> DraftValidation:
    """A draft is valid only if its locked fields equal the sealed fact pack's.

    Compared via canonical serialization (never dict ==). Any drift — a changed
    amount, date, identifier, or decision type — invalidates the draft.
    """
    expected = locked_fields(pack)
    if canonical_json_bytes(expected) != canonical_json_bytes(draft_locked_fields):
        issues = tuple(
            f"LOCKED_FIELD_DRIFT:{k}"
            for k in sorted(expected)
            if expected[k] != draft_locked_fields.get(k)
        ) or ("LOCKED_FIELD_SET_MISMATCH",)
        return DraftValidation(False, issues)
    return DraftValidation(True, ())


def _supported_number_tokens(pack: SealedFactPack) -> set[str]:
    """Every numeric string a draft is allowed to contain, from the seal alone.

    Money is admitted in the shapes a writer legitimately uses: minor units, plain
    dollars, and thousands-grouped, with and without decimals ("70000", "700",
    "700.00", "$700.00"). Dates contribute both their ISO form and their component
    parts, so "June 8, 2026" clears via 8/2026 while an invented "June 9" does not.
    """
    allowed: set[str] = set()

    for minor in (pack.disputed_amount_minor, pack.supported_amount_minor):
        whole, cents = divmod(int(minor), 100)
        allowed.update({
            str(int(minor)), str(whole), f"{whole:,}",
            f"{whole}.{cents:02d}", f"{whole:,}.{cents:02d}",
        })

    for value in (pack.charged_period_start, pack.charged_period_end):
        text = str(value)
        allowed.add(text)
        for part in text.replace("-", " ").replace("/", " ").split():
            allowed.add(part)
            allowed.add(part.lstrip("0") or "0")

    # Identifiers frequently embed digits (TLLU-482931-7, INV-1048, Clause 4.2).
    # Harvest dotted forms BEFORE bare runs so a clause number like "4.2" is
    # admitted whole rather than only as "4" and "2".
    for ident in (pack.container_ref, pack.invoice_no, pack.rule_ref):
        text = str(ident)
        for run in re.findall(r"\d+(?:\.\d+)*", text):
            allowed.add(run)
            allowed.add(run.lstrip("0") or "0")

    # The claimed total is sealed (supported + disputed) and is the one figure a
    # writer naturally cites that is not itself a pack field.
    claimed = int(pack.supported_amount_minor) + int(pack.disputed_amount_minor)
    whole, cents = divmod(claimed, 100)
    allowed.update({
        str(claimed), str(whole), f"{whole:,}",
        f"{whole}.{cents:02d}", f"{whole:,}.{cents:02d}",
    })

    return allowed


def validate_draft_prose(pack: SealedFactPack, prose: str) -> DraftValidation:
    """Reject generated prose that asserts a number the seal does not support.

    ``validate_draft_locked_fields`` compares seal-derived fields to seal-derived
    fields, so it cannot catch a hallucination in the free text. This does: every
    numeric token in the prose must be traceable to the sealed fact pack, and any
    sealed identifier the prose names must be spelled exactly.

    Deliberately one-directional — prose need not mention every fact, it simply
    may not introduce one. Ordinals and small counts ("2-4 sentences", "7 days")
    are admitted only when the seal supports them; everything else is an issue.
    """
    allowed = _supported_number_tokens(pack)
    issues: list[str] = []

    for token in re.findall(r"\$?\d[\d,]*(?:\.\d+)?", prose or ""):
        bare = token.lstrip("$")
        if bare in allowed or bare.replace(",", "") in allowed:
            continue
        issues.append(f"UNSUPPORTED_NUMBER:{token}")

    # An identifier may be omitted, but a mangled one is a fabricated reference.
    lowered = (prose or "").lower()
    for name, ident in (("container_ref", pack.container_ref),
                        ("invoice_no", pack.invoice_no)):
        stem = re.split(r"[\s,;]", str(ident).strip())[0]
        if stem and stem.lower()[:4] in lowered and stem.lower() not in lowered:
            issues.append(f"MANGLED_IDENTIFIER:{name}")

    if issues:
        return DraftValidation(False, tuple(sorted(set(issues))))
    return DraftValidation(True, ())


@dataclass(frozen=True)
class GateResult:
    gate_code: str
    state: GateState
    detail: str | None


@dataclass(frozen=True)
class SendDecision:
    permitted: bool
    gate_results: tuple[GateResult, ...]
    blocked_reason: str | None


def evaluate_send_gates(results: dict[str, GateResult]) -> SendDecision:
    """Send is permitted only if EVERY gate is VERIFIED. First failing (or
    missing) gate is the blocked reason. No gate may be skipped."""
    ordered: list[GateResult] = []
    blocked_reason: str | None = None
    for code in SEND_GATES:
        result = results.get(code) or GateResult(code, GateState.NOT_RUN, "gate not run")
        ordered.append(result)
        if result.state is not GateState.VERIFIED and blocked_reason is None:
            blocked_reason = _blocked_code(code, result.state)
    return SendDecision(
        permitted=blocked_reason is None,
        gate_results=tuple(ordered),
        blocked_reason=blocked_reason,
    )


def _blocked_code(gate_code: str, state: GateState) -> str:
    if state is GateState.NOT_RUN:
        return f"GATE_NOT_RUN:{gate_code}"
    return {
        "SECOND_AUTHORIZATION": "SEND_BLOCKED_AUTHORIZATION",
        "LOCKED_FIELDS": "SEND_BLOCKED_LOCKED_FIELDS",
        "APPROVED_MEMORY_MCP": "SEND_BLOCKED_MEMORY",
        "VECTOR_CLAUSE_BINDING": "SEND_BLOCKED_VECTOR",
        "EXACT_S3_SOURCE": "SEND_BLOCKED_SOURCE",
        "NO_FALLBACK": "SEND_BLOCKED_FALLBACK",
    }.get(gate_code, f"SEND_BLOCKED:{gate_code}")


def build_subject(pack: SealedFactPack) -> str:
    return f"Adjustment request · {pack.invoice_no} · {pack.container_ref}"

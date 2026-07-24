"""Pure Gate 6 tests: locked-field validation + all-must-pass send gate."""

from __future__ import annotations

from src.core.correspondence import (
    GateResult,
    GateState,
    SealedFactPack,
    build_subject,
    evaluate_send_gates,
    locked_fields,
    locked_fields_digest,
    validate_draft_locked_fields,
)


def _pack(**over):
    base = dict(
        invoice_id="inv-1", decision_seal_id="seal-1", seal_digest="sha256:s",
        recommendation_type="DISPUTE", disputed_amount_minor=70000,
        supported_amount_minor=175000, currency="USD",
        charged_period_start="2026-06-08", charged_period_end="2026-06-14",
        container_ref="TLLU4829317", invoice_no="INV-1048", rule_ref="Clause 4.2",
    )
    base.update(over)
    return SealedFactPack(**base)


def _all_verified():
    from src.core.correspondence import SEND_GATES
    return {c: GateResult(c, GateState.VERIFIED, None) for c in SEND_GATES}


def test_valid_draft_matches_sealed_locked_fields():
    pack = _pack()
    v = validate_draft_locked_fields(pack, locked_fields(pack))
    assert v.ok
    assert v.issues == ()


def test_draft_with_changed_amount_rejected():
    pack = _pack()
    tampered = dict(locked_fields(pack))
    tampered["disputed_amount_minor"] = 60000  # model tried to change the amount
    v = validate_draft_locked_fields(pack, tampered)
    assert not v.ok
    assert "LOCKED_FIELD_DRIFT:disputed_amount_minor" in v.issues


def test_draft_with_changed_identifier_rejected():
    pack = _pack()
    tampered = dict(locked_fields(pack))
    tampered["container_ref"] = "MSCU0000000"
    v = validate_draft_locked_fields(pack, tampered)
    assert not v.ok
    assert "LOCKED_FIELD_DRIFT:container_ref" in v.issues


def test_locked_fields_digest_deterministic():
    assert locked_fields_digest(_pack()) == locked_fields_digest(_pack())
    assert locked_fields_digest(_pack()) != locked_fields_digest(
        _pack(disputed_amount_minor=1))


def test_all_gates_verified_permits_send():
    decision = evaluate_send_gates(_all_verified())
    assert decision.permitted
    assert decision.blocked_reason is None
    assert len(decision.gate_results) == 6


def test_failed_mcp_gate_blocks_send():
    gates = _all_verified()
    gates["APPROVED_MEMORY_MCP"] = GateResult(
        "APPROVED_MEMORY_MCP", GateState.FAILED, "mcp unavailable")
    decision = evaluate_send_gates(gates)
    assert not decision.permitted
    assert decision.blocked_reason == "SEND_BLOCKED_MEMORY"


def test_failed_vector_gate_blocks_send():
    gates = _all_verified()
    gates["VECTOR_CLAUSE_BINDING"] = GateResult(
        "VECTOR_CLAUSE_BINDING", GateState.FAILED, "binding mismatch")
    assert evaluate_send_gates(gates).blocked_reason == "SEND_BLOCKED_VECTOR"


def test_failed_source_gate_blocks_send():
    gates = _all_verified()
    gates["EXACT_S3_SOURCE"] = GateResult(
        "EXACT_S3_SOURCE", GateState.FAILED, "version unavailable")
    assert evaluate_send_gates(gates).blocked_reason == "SEND_BLOCKED_SOURCE"


def test_failed_no_fallback_gate_blocks_send():
    gates = _all_verified()
    gates["NO_FALLBACK"] = GateResult("NO_FALLBACK", GateState.FAILED, "fallback used")
    assert evaluate_send_gates(gates).blocked_reason == "SEND_BLOCKED_FALLBACK"


def test_missing_second_authorization_blocks_send():
    gates = _all_verified()
    del gates["SECOND_AUTHORIZATION"]
    decision = evaluate_send_gates(gates)
    assert not decision.permitted
    assert decision.blocked_reason == "GATE_NOT_RUN:SECOND_AUTHORIZATION"


def test_subject_format():
    assert build_subject(_pack()) == "Adjustment request · INV-1048 · TLLU4829317"

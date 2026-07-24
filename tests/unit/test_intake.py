from __future__ import annotations

import pytest

from src.core.intake import (
    AggregateStatus,
    IntakeState,
    Money,
    TaskType,
    normalize_display_filename,
    project_aggregate_status,
    require_transition,
    task_input_fingerprint,
    validate_pdf_envelope,
    validate_public_event,
)


def test_money_requires_integer_minor_units_and_normalizes_currency():
    assert Money(amount_minor=245000, currency="usd") == Money(245000, "USD")
    with pytest.raises(TypeError):
        Money(amount_minor=2450.0, currency="USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money(amount_minor=245000, currency="US")


def test_pdf_envelope_validates_bytes_and_normalizes_untrusted_filename():
    result = validate_pdf_envelope(b"%PDF-1.7\nbody", "../../unsafe/invoice.pdf")

    assert result.display_filename == "invoice.pdf"
    assert result.byte_length == 13
    assert len(result.sha256) == 64


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "EMPTY_FILE"),
        (b"not a pdf", "UNSUPPORTED_FILE"),
    ],
)
def test_pdf_envelope_rejects_invalid_input(body, expected):
    with pytest.raises(ValueError, match=expected):
        validate_pdf_envelope(body, "invoice.pdf")


def test_filename_normalization_never_preserves_path_components():
    assert normalize_display_filename(r"C:\private\hero") == "hero.pdf"
    assert normalize_display_filename("\x00") == "invoice.pdf"


def test_locked_intake_transition_and_status_projection():
    require_transition(IntakeState.RECEIVED, IntakeState.INITIAL_PROCESSING)
    require_transition(IntakeState.INITIAL_PROCESSING, IntakeState.CLAIMS_EXTRACTED)
    require_transition(IntakeState.CLAIMS_EXTRACTED, IntakeState.READY_FOR_RECONSTRUCTION)

    assert project_aggregate_status(IntakeState.RECEIVED) is AggregateStatus.RECEIVED
    assert (
        project_aggregate_status(IntakeState.INITIAL_PROCESSING)
        is AggregateStatus.INITIAL_PROCESSING
    )
    assert (
        project_aggregate_status(IntakeState.READY_FOR_RECONSTRUCTION)
        is AggregateStatus.RECONSTRUCTING
    )
    assert (
        project_aggregate_status(IntakeState.REQUIRED_FIELD_MISSING)
        is AggregateStatus.NEEDS_EVIDENCE
    )


def test_invalid_or_repeated_transition_is_rejected():
    with pytest.raises(ValueError, match="INVALID_INTAKE_TRANSITION"):
        require_transition(IntakeState.RECEIVED, IntakeState.CLAIMS_EXTRACTED)
    with pytest.raises(ValueError, match="INVALID_INTAKE_TRANSITION"):
        require_transition(IntakeState.RECEIVED, IntakeState.RECEIVED)


def test_task_fingerprint_is_order_stable_and_input_sensitive():
    left = task_input_fingerprint(
        task_type=TaskType.EXTRACT_INVOICE_CLAIMS,
        input_refs=[{"version": 1, "id": "source-1", "type": "invoice_source"}],
    )
    right = task_input_fingerprint(
        task_type=TaskType.EXTRACT_INVOICE_CLAIMS,
        input_refs=[{"type": "invoice_source", "id": "source-1", "version": 1}],
    )
    changed = task_input_fingerprint(
        task_type=TaskType.EXTRACT_INVOICE_CLAIMS,
        input_refs=[{"type": "invoice_source", "id": "source-1", "version": 2}],
    )

    assert left == right
    assert changed != left


def _public_event():
    return {
        "event_id": "evt-1",
        "event_type": "invoice.received",
        "schema_version": 1,
        "invoice_id": "invoice-1",
        "sequence": 1,
        "occurred_at": "2026-07-23T18:00:00Z",
        "role": "INTAKE_AGENT",
        "task": "PRESERVE_INVOICE_SOURCE",
        "tool": {"display_name": "Amazon S3"},
        "state": "COMPLETED",
        "status": "RECEIVED",
        "summary": "Invoice received and original source verified",
        "produced_object_refs": [
            {"type": "invoice_source", "id": "source-1", "version": 1}
        ],
        "public_error": None,
    }


def test_public_event_enforces_complete_canonical_envelope():
    validate_public_event(_public_event())

    event = _public_event()
    event.pop("sequence")
    with pytest.raises(ValueError, match="missing public event fields"):
        validate_public_event(event)


def test_public_event_rejects_untyped_refs_and_private_fields():
    event = _public_event()
    event["produced_object_refs"] = ["source-1"]
    with pytest.raises(ValueError, match="typed and versioned"):
        validate_public_event(event)

    event = _public_event()
    event["s3_version_id"] = "private-version"
    with pytest.raises(ValueError, match="private field"):
        validate_public_event(event)


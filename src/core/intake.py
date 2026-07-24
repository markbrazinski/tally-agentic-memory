"""Pure contracts for the locked-demo Intake/Orchestration spine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import Any

MAX_PDF_BYTES = 15 * 1024 * 1024
PDF_SIGNATURE = b"%PDF-"


class IntakeState(StrEnum):
    RECEIVED = "RECEIVED"
    INITIAL_PROCESSING = "INITIAL_PROCESSING"
    CLAIMS_EXTRACTED = "CLAIMS_EXTRACTED"
    READY_FOR_RECONSTRUCTION = "READY_FOR_RECONSTRUCTION"
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    UNREADABLE = "UNREADABLE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    BLOCKED_SOURCE_VERSION_UNAVAILABLE = "BLOCKED_SOURCE_VERSION_UNAVAILABLE"


class AggregateStatus(StrEnum):
    RECEIVED = "RECEIVED"
    INITIAL_PROCESSING = "INITIAL_PROCESSING"
    RECONSTRUCTING = "RECONSTRUCTING"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    BLOCKED = "BLOCKED"


class TaskState(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class TaskType(StrEnum):
    PRESERVE_INVOICE_SOURCE = "PRESERVE_INVOICE_SOURCE"
    EXTRACT_INVOICE_CLAIMS = "EXTRACT_INVOICE_CLAIMS"
    VALIDATE_INVOICE_CLAIMS = "VALIDATE_INVOICE_CLAIMS"
    START_RECONSTRUCTION = "START_RECONSTRUCTION"


_ALLOWED_TRANSITIONS = {
    IntakeState.RECEIVED: {
        IntakeState.INITIAL_PROCESSING,
        IntakeState.UNSUPPORTED_FILE,
        IntakeState.BLOCKED_SOURCE_VERSION_UNAVAILABLE,
    },
    IntakeState.INITIAL_PROCESSING: {
        IntakeState.CLAIMS_EXTRACTED,
        IntakeState.UNREADABLE,
        IntakeState.EXTRACTION_FAILED,
        IntakeState.BLOCKED_SOURCE_VERSION_UNAVAILABLE,
    },
    IntakeState.CLAIMS_EXTRACTED: {
        IntakeState.READY_FOR_RECONSTRUCTION,
        IntakeState.REQUIRED_FIELD_MISSING,
        IntakeState.BLOCKED_SOURCE_VERSION_UNAVAILABLE,
    },
    IntakeState.READY_FOR_RECONSTRUCTION: {
        IntakeState.BLOCKED_SOURCE_VERSION_UNAVAILABLE,
    },
}


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer")
        normalized = self.currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", normalized)


@dataclass(frozen=True)
class PdfEnvelope:
    display_filename: str
    byte_length: int
    sha256: str


def validate_pdf_envelope(
    body: bytes,
    filename: str | None,
    *,
    maximum_bytes: int = MAX_PDF_BYTES,
) -> PdfEnvelope:
    if not body:
        raise ValueError("EMPTY_FILE")
    if len(body) > maximum_bytes:
        raise ValueError("FILE_TOO_LARGE")
    if not body.startswith(PDF_SIGNATURE):
        raise ValueError("UNSUPPORTED_FILE")
    display_name = normalize_display_filename(filename)
    return PdfEnvelope(
        display_filename=display_name,
        byte_length=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def normalize_display_filename(filename: str | None) -> str:
    candidate = PurePath((filename or "invoice.pdf").replace("\\", "/")).name.strip()
    candidate = re.sub(r"[\x00-\x1f\x7f]", "", candidate)
    if not candidate:
        candidate = "invoice.pdf"
    if not candidate.lower().endswith(".pdf"):
        candidate += ".pdf"
    return candidate[:255]


def project_aggregate_status(
    intake_state: IntakeState,
    *,
    reconstruction_active: bool = False,
) -> AggregateStatus:
    if reconstruction_active or intake_state is IntakeState.READY_FOR_RECONSTRUCTION:
        return AggregateStatus.RECONSTRUCTING
    if intake_state is IntakeState.RECEIVED:
        return AggregateStatus.RECEIVED
    if intake_state in {IntakeState.INITIAL_PROCESSING, IntakeState.CLAIMS_EXTRACTED}:
        return AggregateStatus.INITIAL_PROCESSING
    if intake_state is IntakeState.REQUIRED_FIELD_MISSING:
        return AggregateStatus.NEEDS_EVIDENCE
    return AggregateStatus.BLOCKED


def require_transition(current: IntakeState, target: IntakeState) -> None:
    if target not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"INVALID_INTAKE_TRANSITION:{current.value}:{target.value}")


def task_input_fingerprint(*, task_type: TaskType, input_refs: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"task_type": task_type.value, "input_refs": input_refs},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_public_event(event: dict[str, Any]) -> None:
    required = {
        "event_id",
        "event_type",
        "schema_version",
        "invoice_id",
        "sequence",
        "occurred_at",
        "role",
        "task",
        "tool",
        "state",
        "status",
        "summary",
        "produced_object_refs",
        "public_error",
    }
    missing = sorted(required - event.keys())
    if missing:
        raise ValueError(f"missing public event fields: {', '.join(missing)}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", str(event["event_type"])):
        raise ValueError("event_type must be a lowercase namespaced string")
    if isinstance(event["sequence"], bool) or int(event["sequence"]) <= 0:
        raise ValueError("sequence must be positive")
    refs = event["produced_object_refs"]
    if not isinstance(refs, list) or any(
        not isinstance(ref, dict) or set(("type", "id", "version")) - ref.keys()
        for ref in refs
    ):
        raise ValueError("produced_object_refs must be typed and versioned")
    serialized = json.dumps(event, sort_keys=True).lower()
    for forbidden in (
        "s3_bucket",
        "s3_object_key",
        "s3_version_id",
        "sql_text",
        "model_response",
        "prompt_text",
        "private_error",
    ):
        if forbidden in serialized:
            raise ValueError(f"private field in public event: {forbidden}")


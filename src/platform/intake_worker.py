"""One bounded durable Intake worker iteration; no fixture/model fallback."""

from __future__ import annotations

from io import BytesIO

import pdfplumber

from src.core.intake_claims import validate_extracted_claims
from src.external.dal import DAL
from src.external.intake_bedrock import IntakeBedrockExtractor
from src.external.invoice_source_store import (
    SourcePersistenceError,
    VersionedInvoiceSourceStore,
)
from src.platform.intake_tasks import (
    ExtractionCompletion,
    claim_next_extraction_task,
    complete_extraction,
    fail_extraction,
)


def run_one_intake_task(
    dal: DAL,
    *,
    worker_id: str,
    source_store: VersionedInvoiceSourceStore,
    extractor: IntakeBedrockExtractor | None = None,
) -> ExtractionCompletion | None:
    lease = claim_next_extraction_task(dal, worker_id=worker_id)
    if lease is None:
        return None
    try:
        pdf_bytes = source_store.get_exact(lease.source)
    except SourcePersistenceError as exc:
        fail_extraction(
            dal,
            lease=lease,
            error_code=str(exc),
            retryable=False,
        )
        return None

    try:
        pages = _read_pdf_pages(pdf_bytes)
        result = (extractor or IntakeBedrockExtractor()).extract(
            [page["text"] for page in pages]
        )
        validation = validate_extracted_claims(result.claims, pages)
        return complete_extraction(
            dal,
            lease=lease,
            validation=validation,
            raw_response_sha256=result.raw_response_sha256,
            provider_request_ref_private=result.provider_request_ref_private,
        )
    except Exception as exc:
        fail_extraction(
            dal,
            lease=lease,
            error_code=_safe_error_code(exc),
            retryable=True,
        )
        return None


def _read_pdf_pages(body: bytes) -> list[dict]:
    with pdfplumber.open(BytesIO(body)) as pdf:
        return [
            {
                "text": page.extract_text() or "",
                "words": page.extract_words(),
                "width": float(page.width),
                "height": float(page.height),
            }
            for page in pdf.pages
        ]


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError) and str(exc) == "BEDROCK_SCHEMA_INVALID":
        return "BEDROCK_SCHEMA_INVALID"
    return "EXTRACTION_DEPENDENCY_UNAVAILABLE"

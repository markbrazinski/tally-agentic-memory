from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.external.intake_bedrock import BedrockClaimExtraction
from src.external.invoice_source_store import (
    SourcePersistenceError,
    StoredInvoiceSource,
)
from src.platform.intake_tasks import ExtractionCompletion, ExtractionTaskLease
from src.platform.intake_worker import run_one_intake_task

PDF = Path(__file__).parents[1] / "fixtures" / "demo" / "INV-1048.pdf"


def _lease():
    return ExtractionTaskLease(
        task_id="task-1",
        invoice_id="invoice-1",
        source_id="source-1",
        attempt=1,
        worker_id="worker-1",
        lease_expires_at=datetime(2026, 7, 23, 18, 2, tzinfo=UTC),
        knowledge_cutoff_at=datetime(2026, 7, 23, 18, 0, tzinfo=UTC),
        source=StoredInvoiceSource("bucket", "key", "version", "a" * 64, 100),
        initiated_by=None,
        actor_display="Rachel Martinez",
    )


class Store:
    def get_exact(self, source):
        return PDF.read_bytes()


class Extractor:
    def extract(self, pages):
        assert "INV-1048" in pages[0]
        return BedrockClaimExtraction(
            claims={"claims": {}},
            raw_response_sha256="b" * 64,
            provider_request_ref_private="request-private",
        )


def test_worker_uses_exact_source_and_commits_validated_output(monkeypatch):
    lease = _lease()
    captured = {}
    monkeypatch.setattr(
        "src.platform.intake_worker.claim_next_extraction_task",
        lambda *args, **kwargs: lease,
    )

    def complete(*args, **kwargs):
        captured.update(kwargs)
        return ExtractionCompletion("run-1", "set-1", 1, None, "NEEDS_EVIDENCE")

    monkeypatch.setattr("src.platform.intake_worker.complete_extraction", complete)

    result = run_one_intake_task(
        object(),
        worker_id="worker-1",
        source_store=Store(),
        extractor=Extractor(),
    )

    assert result.status == "NEEDS_EVIDENCE"
    assert captured["raw_response_sha256"] == "b" * 64
    assert not captured["validation"].passed


def test_missing_exact_source_blocks_without_calling_bedrock(monkeypatch):
    lease = _lease()
    failures = []
    monkeypatch.setattr(
        "src.platform.intake_worker.claim_next_extraction_task",
        lambda *args, **kwargs: lease,
    )
    monkeypatch.setattr(
        "src.platform.intake_worker.fail_extraction",
        lambda *args, **kwargs: failures.append(kwargs),
    )

    class MissingStore:
        def get_exact(self, source):
            raise SourcePersistenceError("SOURCE_VERSION_UNAVAILABLE")

    class ForbiddenExtractor:
        def extract(self, pages):
            raise AssertionError("Bedrock must not run without the exact source")

    result = run_one_intake_task(
        object(),
        worker_id="worker-1",
        source_store=MissingStore(),
        extractor=ForbiddenExtractor(),
    )

    assert result is None
    assert failures[0]["error_code"] == "SOURCE_VERSION_UNAVAILABLE"
    assert failures[0]["retryable"] is False

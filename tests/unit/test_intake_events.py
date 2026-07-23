from __future__ import annotations

from datetime import UTC, datetime

from src.platform.intake_events import _project_event, sse_stream


def _row():
    return (
        "event-1",
        "invoice.received",
        1,
        "invoice-1",
        1,
        datetime(2026, 7, 23, 18, 0, tzinfo=UTC),
        "INTAKE_AGENT",
        "PRESERVE_INVOICE_SOURCE",
        "Amazon S3",
        "COMPLETED",
        "RECEIVED",
        "Invoice received and original source verified",
        [],
        [{"type": "invoice_source", "id": "source-1", "version": 1}],
        1,
        12,
        None,
    )


def test_event_projection_is_canonical_and_public_safe():
    event = _project_event(_row())

    assert event["event_id"] == "event-1"
    assert event["tool"] == {"display_name": "Amazon S3"}
    assert "bucket" not in str(event).lower()
    assert "version_id" not in str(event).lower()


def test_unknown_sse_cursor_requires_snapshot_reconciliation(monkeypatch):
    monkeypatch.setattr(
        "src.platform.intake_events.load_event_history",
        lambda *args, **kwargs: ([], True),
    )
    monkeypatch.setattr("src.platform.intake_events.time.sleep", lambda _: None)

    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    stream = sse_stream(lambda: Context(), last_event_id="expired")

    assert "stream.reconcile_required" in next(stream)

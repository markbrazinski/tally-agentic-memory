"""Durable public event projection, outbox relay, and SSE replay."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from src.core.intake import validate_public_event
from src.external.dal import DAL


def load_event_history(
    dal: DAL,
    *,
    invoice_id: str | None = None,
    after_sequence: int = 0,
    after_event_id: str | None = None,
    delivered_only: bool = False,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], bool]:
    tenant_id = dal.tenant.tenant_id
    cursor_unknown = False
    global_cursor: tuple[Any, ...] | None = None
    if after_event_id:
        with dal.conn.cursor() as cur:
            cur.execute(
                """
                SELECT invoice_id, sequence, occurred_at
                FROM invoice_events
                WHERE tenant_id=%s AND id=%s;
                """,
                (tenant_id, after_event_id),
            )
            cursor = cur.fetchone()
        if cursor is None:
            return [], True
        if invoice_id is not None and str(cursor[0]) != invoice_id:
            return [], True
        if invoice_id is None:
            global_cursor = (cursor[2], cursor[0], int(cursor[1]))
        else:
            after_sequence = int(cursor[1])

    clauses = ["e.tenant_id=%s"]
    params: list[Any] = [tenant_id]
    if global_cursor is not None:
        clauses.append("(e.occurred_at, e.invoice_id, e.sequence) > (%s, %s, %s)")
        params.extend(global_cursor)
    elif invoice_id is not None:
        clauses.append("e.sequence > %s")
        params.append(after_sequence)
    if invoice_id is not None:
        clauses.append("e.invoice_id=%s")
        params.append(invoice_id)
    if delivered_only:
        clauses.append("o.state='DELIVERED'")
    params.append(min(max(limit, 1), 500))
    with dal.conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT e.id, e.event_type, e.schema_version, e.invoice_id,
                   e.sequence, e.occurred_at, e.role, e.task,
                   e.tool_display_name, e.state, e.aggregate_status,
                   e.summary, e.input_object_refs, e.produced_object_refs,
                   e.output_count, e.elapsed_ms, e.public_error
            FROM invoice_events e
            JOIN event_outbox o
              ON o.tenant_id=e.tenant_id AND o.event_id=e.id
            WHERE {" AND ".join(clauses)}
            ORDER BY e.occurred_at, e.invoice_id, e.sequence
            LIMIT %s;
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [_project_event(row) for row in rows], cursor_unknown


def relay_outbox_batch(dal: DAL, *, limit: int = 100) -> int:
    tenant_id = dal.tenant.tenant_id

    def _relay(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM event_outbox
                WHERE tenant_id=%s AND state='PENDING'
                  AND available_at <= now()
                  AND (lease_expires_at IS NULL OR lease_expires_at < now())
                ORDER BY available_at, id
                LIMIT %s
                FOR UPDATE SKIP LOCKED;
                """,
                (tenant_id, min(max(limit, 1), 500)),
            )
            ids = [str(row[0]) for row in cur.fetchall()]
            if not ids:
                return 0
            cur.execute("SELECT now();")
            delivered_at = cur.fetchone()[0]
            for outbox_id in ids:
                cur.execute(
                    """
                    UPDATE event_outbox
                    SET state='DELIVERED', delivery_count=delivery_count + 1,
                        delivered_at=%s, updated_at=%s
                    WHERE tenant_id=%s AND id=%s AND state='PENDING';
                    """,
                    (delivered_at, delivered_at, tenant_id, outbox_id),
                )
            return len(ids)

    return dal.run_with_retry(_relay)


def sse_stream(
    dal_factory,
    *,
    last_event_id: str | None,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
) -> Iterator[str]:
    cursor = last_event_id
    heartbeat_at = time.monotonic() + heartbeat_seconds
    cursor_checked = False
    while True:
        with dal_factory() as dal:
            events, unknown = load_event_history(
                dal,
                after_event_id=cursor,
                delivered_only=True,
            )
        if unknown and not cursor_checked:
            payload = {
                "event_type": "stream.reconcile_required",
                "schema_version": 1,
                "summary": "Event cursor is unavailable; reload server snapshots",
            }
            yield f"event: stream.reconcile_required\ndata: {json.dumps(payload)}\n\n"
            cursor = None
        cursor_checked = True
        for event in events:
            cursor = event["event_id"]
            yield (
                f"id: {cursor}\n"
                f"event: {event['event_type']}\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )
            heartbeat_at = time.monotonic() + heartbeat_seconds
        now = time.monotonic()
        if now >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = now + heartbeat_seconds
        time.sleep(poll_seconds)


def _project_event(row: tuple[Any, ...]) -> dict[str, Any]:
    event = {
        "event_id": str(row[0]),
        "event_type": row[1],
        "schema_version": int(row[2]),
        "invoice_id": str(row[3]),
        "sequence": int(row[4]),
        "occurred_at": row[5].isoformat(),
        "role": row[6],
        "task": row[7],
        "tool": (
            {"display_name": row[8]}
            if row[8]
            else {"display_name": "Deterministic validation"}
        ),
        "state": row[9],
        "status": row[10],
        "summary": row[11],
        "input_object_refs": _json_value(row[12], []),
        "produced_object_refs": _json_value(row[13], []),
        "output_count": row[14],
        "elapsed_ms": row[15],
        "public_error": _json_value(row[16], None),
    }
    validate_public_event(event)
    return event


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return value if isinstance(value, (dict, list)) else json.loads(value)

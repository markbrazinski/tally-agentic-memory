"""Gate 2 live driver-side reconstruction trace against tally_gate2_iso.

Proves the durable Gate-2 write path end to end on REAL CockroachDB (the isolated
tally_gate2_iso database): seed a fictional invoice + active claim set +
START_RECONSTRUCTION task and the representative pre-invoice memory, run the
reconstruction, and read back the immutable reconstruction version, seven charged
days, coverage, events, and outbox rows.

BOUNDARY / HONESTY: the isolated Managed MCP endpoint is NOT provisioned, so this
script reads the mcp_reconstruction_memory_v1 view through the DRIVER and labels
that read `read_path="driver-diagnostic"`. This is explicitly NOT the Managed MCP
sponsor trace, which remains deferred until an isolated MCP endpoint exists. The
worker's real MCP path (src/platform/reconstruction_worker.py) is unit-proven to
fail closed with no fallback; this script exercises the persistence/projection
half against live infrastructure.

Writes ONLY to tally_gate2_iso. Never touches defaultdb or any other database.
Emits a public-safe trace JSON with counts and states — no private identifiers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse as up
from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.intake import TaskType, task_input_fingerprint  # noqa: E402
from src.core.reconstruction import (  # noqa: E402
    RawEventRow,
    adjudicate_charged_days,
    classify_coverage,
    resolve_charge_boundary,
    resolve_terminal_state,
    validate_events,
)
from src.external.dal import DAL, Tenant  # noqa: E402
from src.external.reconstruction_mcp import build_reconstruction_query  # noqa: E402
from src.external.reconstruction_seed import (  # noqa: E402
    seed_reconstruction_memory,
)
from src.platform.reconstruction_repository import (  # noqa: E402
    ReconstructionTaskLease,
    complete_reconstruction,
)

TENANT = "10000000-0000-4000-8000-0000000000a2"  # fictional Gate-2 isolated tenant
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)  # invoice received_at


def _iso_dsn() -> str:
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env",
    )
    raw = None
    for candidate in (
        os.environ.get("TALLY_CRDB_DSN"),
        _read_env(env_path),
        _read_env("/Users/markbrazinski/Desktop/coding fun/tally-agent/.env"),
    ):
        if candidate:
            raw = candidate
            break
    if not raw:
        raise SystemExit("no TALLY_CRDB_DSN available")
    return up.urlunparse(up.urlparse(raw)._replace(path="/tally_gate2_iso"))


def _read_env(path: str) -> str | None:
    try:
        m = re.search(r"TALLY_CRDB_DSN=(.+)", open(path).read())
    except OSError:
        return None
    return m.group(1).strip().strip('"').strip("'") if m else None


def _seed_prereqs(conn, *, tenant_id, invoice_id, source_id, claim_set_id, task_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;",
            (tenant_id, "Gate2 Isolated (fictional)"),
        )
        cur.execute(
            "INSERT INTO carriers (tenant_id, id, scac, name) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING;",
            (tenant_id, "20000000-0000-4000-8000-000000000001", "ASTL",
             "Asterline Demo (fictional)"),
        )
        cur.execute(
            """
            INSERT INTO invoices
                (tenant_id, id, carrier_id, invoice_no, received_at, s3_key,
                 sha256, status, intake_state, aggregate_status, status_sequence,
                 active_claim_set_version, row_version, display_name)
            VALUES (%s, %s, %s, 'INV-1048', %s, 'representative/INV-1048.pdf',
                    %s, 'RECONSTRUCTING', 'READY_FOR_RECONSTRUCTION',
                    'RECONSTRUCTING', 5, 1, 2, 'INV-1048.pdf')
            ON CONFLICT (tenant_id, id) DO NOTHING;
            """,
            (tenant_id, invoice_id, "20000000-0000-4000-8000-000000000001", CUTOFF,
             "e" * 64),
        )
        cur.execute(
            """
            INSERT INTO invoice_sources
                (tenant_id, id, invoice_id, source_type, display_filename,
                 mime_type, byte_length, sha256, s3_bucket_ref_private,
                 s3_object_key_private, s3_version_id_private, preservation_status,
                 provenance_classification, public_disclosure, verified_at,
                 received_at)
            VALUES (%s, %s, %s, 'INVOICE_PDF', 'INV-1048.pdf', 'application/pdf',
                    1024, %s, 'demo-bucket', 'representative/INV-1048.pdf',
                    'v1', 'VERSION_VERIFIED', 'DEMO_SCENARIO',
                    'Representative demonstration data', %s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (tenant_id, source_id, invoice_id, "e" * 64, CUTOFF, CUTOFF),
        )
        # Active claim set with the charged-period + rate claims the lease reads.
        cur.execute(
            """
            INSERT INTO extraction_runs
                (tenant_id, id, invoice_id, source_id, source_sha256,
                 source_version_ref_private, model_id, schema_version,
                 template_version, attempt, requested_at, validation_state)
            VALUES (%s, %s, %s, %s, %s, 'v1', 'demo-model', 'v1', 'v1', 1, %s,
                    'VALIDATED')
            ON CONFLICT DO NOTHING;
            """,
            (tenant_id, str(uuid4()), invoice_id, source_id, "e" * 64, CUTOFF),
        )
        run_id = None
        cur.execute(
            "SELECT id FROM extraction_runs WHERE tenant_id=%s AND invoice_id=%s LIMIT 1;",
            (tenant_id, invoice_id),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO claim_sets
                (tenant_id, id, invoice_id, claim_set_version, extraction_run_id,
                 validation_state)
            VALUES (%s, %s, %s, 1, %s, 'VALIDATED')
            ON CONFLICT DO NOTHING;
            """,
            (tenant_id, claim_set_id, invoice_id, run_id),
        )
        claims = [
            ("container_number", "IDENTIFIER", json.dumps("TLLU4829317"), None, None),
            ("period_start", "DATE", json.dumps("2026-06-08"), None, None),
            ("period_end", "DATE", json.dumps("2026-06-14"), None, None),
            ("charged_days", "INTEGER", json.dumps(7), None, None),
            ("daily_rate", "MONEY",
             json.dumps({"amount_minor": 35000, "currency": "USD"}), 35000, "USD"),
            ("total", "MONEY",
             json.dumps({"amount_minor": 245000, "currency": "USD"}), 245000, "USD"),
        ]
        for field_name, vtype, normalized, amount, currency in claims:
            cur.execute(
                """
                INSERT INTO extracted_claims
                    (tenant_id, claim_set_id, field_name, value_type, raw_value,
                     normalized_value, amount_minor, currency, validation_state,
                     text_excerpt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'VALIDATED', 'demo')
                ON CONFLICT DO NOTHING;
                """,
                (tenant_id, claim_set_id, field_name, vtype, "demo", normalized,
                 amount, currency),
            )
        refs = [
            {"type": "invoice_source", "id": source_id, "version": 1},
            {"type": "claim_set", "id": claim_set_id, "version": 1},
        ]
        fingerprint = task_input_fingerprint(
            task_type=TaskType.START_RECONSTRUCTION, input_refs=refs
        )
        cur.execute(
            """
            INSERT INTO workflow_tasks
                (tenant_id, id, invoice_id, task_type, task_version, state,
                 actor_display, knowledge_cutoff_at, input_fingerprint,
                 input_object_refs, current_attempt, lease_owner, public_summary)
            VALUES (%s, %s, %s, 'START_RECONSTRUCTION', 1, 'RUNNING',
                    'trace-worker', %s, %s, %s, 1, 'trace-worker-1',
                    'Reconstructing')
            ON CONFLICT (tenant_id, invoice_id, task_type, task_version,
                         input_fingerprint) DO NOTHING;
            """,
            (tenant_id, task_id, invoice_id, CUTOFF, fingerprint, json.dumps(refs)),
        )
        return fingerprint


def main() -> None:
    dsn = _iso_dsn()
    tenant_id = TENANT
    invoice_id = str(uuid4())
    source_id = str(uuid4())
    claim_set_id = str(uuid4())
    task_id = str(uuid4())

    with psycopg.connect(dsn, connect_timeout=15, autocommit=True) as raw_conn:
        fingerprint = _seed_prereqs(
            raw_conn, tenant_id=tenant_id, invoice_id=invoice_id,
            source_id=source_id, claim_set_id=claim_set_id, task_id=task_id,
        )

    dal = DAL(psycopg.connect(dsn, connect_timeout=15, autocommit=True),
              Tenant(tenant_id, "reconstruction-trace"))
    seed_counts = seed_reconstruction_memory(dal, invoice_id=invoice_id)

    # Read the MCP memory view — via DRIVER (diagnostic), not Managed MCP.
    query = build_reconstruction_query(
        shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
        knowledge_cutoff_iso=CUTOFF.isoformat().replace("+00:00", "Z"),
    )
    with dal.conn.cursor() as cur:
        cur.execute(query)
        cols = [c.name for c in cur.description]
        raw_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    rows = [
        RawEventRow(
            public_ref=r["public_ref"], event_type=r["event_type"],
            shipment_ref=r["shipment_ref"], container_ref=r["container_ref"],
            source_public_ref=r["source_public_ref"],
            source_verification_state=r["source_verification_state"],
            display_anchor=r["display_anchor"],
            provenance_classification=r["provenance_classification"],
            occurred_at=r["occurred_at"].isoformat(),
            recorded_at=r["recorded_at"].isoformat(),
            observed_at=r["observed_at"].isoformat() if r.get("observed_at") else None,
        )
        for r in raw_rows
    ]
    validation = validate_events(
        rows, knowledge_cutoff=CUTOFF, shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
    )
    boundary = resolve_charge_boundary(validation.accepted)
    charge_dates = [date(2026, 6, d) for d in range(8, 15)]
    days = adjudicate_charged_days(
        charge_dates=charge_dates, invoice_rate_minor=35000, currency="USD",
        events=validation.accepted, boundary=boundary,
    )
    coverage = classify_coverage(
        events=validation.accepted, have_invoice_source=True,
        have_container_identity=True, have_charged_dates=True,
        have_invoice_rate=True,
    )
    terminal = resolve_terminal_state(days)
    lease = ReconstructionTaskLease(
        task_id=task_id, invoice_id=invoice_id, attempt=1,
        worker_id="trace-worker-1", lease_expires_at=CUTOFF,
        knowledge_cutoff_at=CUTOFF, input_fingerprint=fingerprint,
        claim_set_version=1, source_id=source_id, shipment_ref="TLLU4829317",
        container_ref="TLLU4829317", invoice_rate_minor=35000, currency="USD",
        charge_dates=tuple(d.isoformat() for d in charge_dates),
        initiated_by=None, actor_display="trace-worker",
    )
    day_roles = {
        d.charge_date.isoformat(): {
            "AVAILABILITY_BOUNDARY": [e.public_ref for e in validation.accepted
                                      if e.event_type.value == "AVAILABLE"],
            "FREE_TIME_BOUNDARY": [e.public_ref for e in validation.accepted
                                   if e.event_type.value == "FREE_TIME_END"],
            "CHARGE_END": [e.public_ref for e in validation.accepted
                           if e.event_type.value == "GATE_OUT"],
        }
        for d in days
    }
    completion = complete_reconstruction(
        dal, lease=lease, events=validation.accepted, days=days,
        coverage=coverage, terminal_state=terminal, day_event_roles=day_roles,
        mcp_correlation_id=task_id, mcp_query_ref_private="driver-diagnostic",
        issue_codes=validation.issue_codes,
    )

    # Read back durable state.
    with dal.conn.cursor() as cur:
        cur.execute(
            "SELECT version, state, event_count, days_total, days_complete "
            "FROM reconstructions WHERE tenant_id=%s AND id=%s;",
            (tenant_id, completion.reconstruction_id),
        )
        recon = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM reconstruction_events WHERE tenant_id=%s "
            "AND reconstruction_id=%s;",
            (tenant_id, completion.reconstruction_id),
        )
        event_rows = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE state='SOURCE_COMPLETE') "
            "FROM reconstruction_charged_days WHERE tenant_id=%s "
            "AND reconstruction_id=%s;",
            (tenant_id, completion.reconstruction_id),
        )
        day_total, day_complete = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM invoice_events WHERE tenant_id=%s "
            "AND invoice_id=%s AND role='RECONSTRUCTION_AGENT';",
            (tenant_id, invoice_id),
        )
        recon_events = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM event_outbox o JOIN invoice_events e "
            "ON e.tenant_id=o.tenant_id AND e.id=o.event_id "
            "WHERE o.tenant_id=%s AND e.invoice_id=%s "
            "AND e.role='RECONSTRUCTION_AGENT';",
            (tenant_id, invoice_id),
        )
        outbox_rows = cur.fetchone()[0]

    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (isolated; defaultdb untouched)",
        "read_path": (
            "driver-diagnostic (isolated Managed MCP endpoint not provisioned; "
            "MCP sponsor trace deferred)"
        ),
        "seed_counts": seed_counts,
        "memory_rows_returned": len(raw_rows),
        "events_accepted": len(validation.accepted),
        "events_rejected": len(validation.rejected),
        "reconstruction": {
            "version": recon[0],
            "state": recon[1],
            "event_count": recon[2],
            "days_total": recon[3],
            "days_complete": recon[4],
        },
        "readback": {
            "reconstruction_event_rows": event_rows,
            "charged_day_rows": day_total,
            "charged_days_source_complete": day_complete,
            "public_reconstruction_events": recon_events,
            "outbox_rows": outbox_rows,
        },
        "terminal_state": terminal.value,
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    dal.close()


if __name__ == "__main__":
    main()

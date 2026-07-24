"""Gate 2 live NEGATIVE counterfactual against tally_gate2_iso.

Proves the fail-closed contract on real infrastructure: when a required source
version is unavailable, the Managed MCP read view (mcp_reconstruction_memory_v1)
excludes that event, the boundary cannot form, and the reconstruction resolves to
NEEDS_EVIDENCE — never COMPLETE. No fixture/driver fallback resurrects it.

Writes only to a dedicated negative-case tenant in tally_gate2_iso. Prints a
public-safe result. Never touches defaultdb.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.core.reconstruction import (  # noqa: E402
    RawEventRow,
    adjudicate_charged_days,
    resolve_charge_boundary,
    resolve_terminal_state,
    validate_events,
)
from src.external.dal import DAL, Tenant  # noqa: E402
from src.external.reconstruction_mcp import build_reconstruction_query  # noqa: E402
from src.external.reconstruction_seed import load_source_package  # noqa: E402

NEG_TENANT = "10000000-0000-4000-8000-0000000000b3"
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)


def main() -> None:
    dsn = _iso_dsn()
    pkg = load_source_package()
    dal = DAL(psycopg.connect(dsn, connect_timeout=15, autocommit=True),
              Tenant(NEG_TENANT, "reconstruction-negative"))
    with dal.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING;",
            (NEG_TENANT, "Gate2 Negative (fictional)"),
        )
        # Clear any prior negative-run memory for idempotency.
        cur.execute("DELETE FROM shipment_event_memory WHERE tenant_id=%s;",
                    (NEG_TENANT,))
        # Seed all five events, but mark GATE_OUT source UNAVAILABLE (missing
        # exact source version) — the required boundary event fails closed.
        for event in pkg["events"]:
            state = ("UNAVAILABLE" if event["event_type"] == "GATE_OUT"
                     else "VERIFIED")
            cur.execute(
                """
                INSERT INTO shipment_event_memory
                    (tenant_id, public_ref, shipment_ref, container_ref,
                     event_type, source_public_ref, source_version_state,
                     display_anchor, provenance_classification, occurred_at,
                     recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (NEG_TENANT, event["public_ref"], pkg["shipment_ref"],
                 pkg["container_ref"], event["event_type"],
                 event["source_public_ref"], state, event["display_anchor"],
                 pkg["provenance_classification"],
                 datetime.fromisoformat(event["occurred_at"]),
                 datetime.fromisoformat(event["recorded_at"])),
            )

    # The Managed MCP connection is tenant/database-scoped; the driver diagnostic
    # must replicate that scope, so we add the tenant filter the MCP layer would
    # enforce at connection level (the view carries tenant_id).
    base = build_reconstruction_query(
        shipment_ref="TLLU4829317", container_ref="TLLU4829317",
        knowledge_cutoff_iso=CUTOFF.isoformat().replace("+00:00", "Z"),
    )
    scoped = base.replace(
        "WHERE shipment_ref", f"WHERE tenant_id = '{NEG_TENANT}' AND shipment_ref"
    )
    with dal.conn.cursor() as cur:
        cur.execute(scoped)
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
        )
        for r in raw_rows
    ]
    validation = validate_events(
        rows, knowledge_cutoff=CUTOFF, shipment_ref="TLLU4829317",
        container_ref="TLLU4829317",
    )
    boundary = resolve_charge_boundary(validation.accepted)
    days = adjudicate_charged_days(
        charge_dates=[date(2026, 6, d) for d in range(8, 15)],
        invoice_rate_minor=35000, currency="USD",
        events=validation.accepted, boundary=boundary,
    )
    terminal = resolve_terminal_state(days)

    gate_out_present = any(e.event_type.value == "GATE_OUT" for e in validation.accepted)
    result = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (negative-case tenant; defaultdb untouched)",
        "scenario": "GATE_OUT source version UNAVAILABLE",
        "view_rows_returned": len(raw_rows),
        "gate_out_excluded_by_view": not gate_out_present,
        "boundary_formed": boundary is not None,
        "terminal_state": terminal.value,
        "days_source_complete": sum(1 for d in days if d.state.value == "SOURCE_COMPLETE"),
        "no_complete_reconstruction": terminal.value != "COMPLETE",
        "mock_fallback": False,
    }
    print(json.dumps(result, indent=2))
    assert result["gate_out_excluded_by_view"], "view must exclude unverified source"
    assert result["terminal_state"] != "COMPLETE", "must not COMPLETE without source"
    dal.close()


if __name__ == "__main__":
    main()

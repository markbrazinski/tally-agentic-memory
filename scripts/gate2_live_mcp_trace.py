"""Gate 2 LIVE Managed MCP reconstruction trace against tally_gate2_iso.

Closes the one deferred Gate-2 sub-check: a REAL CockroachDB Managed MCP read
(not the driver-diagnostic). It reads the fresh read-only OAuth bundle from SSM
(minted by scripts/gate5b_oauth_bootstrap.py), points the SAME cluster's Managed
MCP at the isolated tally_gate2_iso database, and runs the fixed reconstruction
SELECT through the real Managed MCP client. Then it drives the full reconstruction
from the MCP-returned rows and reads back the persisted version.

Run AFTER re-authorizing the connector:
    AWS_PROFILE=gate5-deployer python scripts/gate5b_oauth_bootstrap.py   # browser
    AWS_PROFILE=gate5-deployer python scripts/gate2_live_mcp_trace.py

Reads only; writes only to a Gate-2-live tenant in tally_gate2_iso. Never touches
defaultdb. No token value is printed.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from uuid import uuid4

import boto3
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.core.reconstruction import (  # noqa: E402
    adjudicate_charged_days,
    classify_coverage,
    resolve_charge_boundary,
    resolve_terminal_state,
    validate_events,
)
from src.external.cockroach_mcp import (  # noqa: E402
    CockroachManagedMCP,
    ManagedMCPConfig,
    MCPAuthenticationError,
)
from src.external.dal import DAL, Tenant  # noqa: E402
from src.external.oauth_tokens import SSMTokenStore  # noqa: E402
from src.external.reconstruction_mcp import read_reconstruction_memory  # noqa: E402
from src.external.reconstruction_seed import seed_reconstruction_memory  # noqa: E402
from src.platform.reconstruction_repository import (  # noqa: E402
    ReconstructionTaskLease,
    complete_reconstruction,
)

OAUTH_PARAM = os.environ.get("TALLY_OAUTH_TOKEN_PARAMETER",
                             "/tally/gate5/oauth-token-bundle")
MCP_CLUSTER_PARAM = "/tally/gate5/mcp-cluster-id"
ISO_DATABASE = "tally_gate2_iso"
LIVE_TENANT = "10000000-0000-4000-8000-00000000ab02"
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
HERO_DATES = [date(2026, 6, d) for d in range(8, 15)]
SHIP = "TLLU4829317"


def _ssm_value(ssm, name: str) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _live_mcp_config(ssm) -> ManagedMCPConfig:
    bundle = SSMTokenStore(ssm, parameter_name=OAUTH_PARAM).load()
    cluster_id = _ssm_value(ssm, MCP_CLUSTER_PARAM)
    return ManagedMCPConfig(
        cluster_id=cluster_id, database=ISO_DATABASE,
        access_token=bundle.access_token, service_identity="oauth-read-only-client",
        permission_mode="oauth-read-only",
    )


def _seed(cur, dal):
    """Minimal invoice + START_RECONSTRUCTION task + representative memory."""
    from src.core.intake import TaskType, task_input_fingerprint

    invoice_id, source_id, claim_set_id, task_id = (str(uuid4()) for _ in range(4))
    cur.execute("INSERT INTO tenants (id,name) VALUES (%s,'Gate2-live (fictional)') "
                "ON CONFLICT (id) DO NOTHING;", (LIVE_TENANT,))
    cur.execute("INSERT INTO carriers (tenant_id,id,scac,name) VALUES "
                "(%s,'20000000-0000-4000-8000-0000000000b2','ASTL','Asterline') "
                "ON CONFLICT DO NOTHING;", (LIVE_TENANT,))
    cur.execute(
        """INSERT INTO invoices (tenant_id,id,carrier_id,invoice_no,received_at,s3_key,
            sha256,status,intake_state,aggregate_status,status_sequence,
            active_claim_set_version,row_version,display_name)
           VALUES (%s,%s,'20000000-0000-4000-8000-0000000000b2','INV-1048',%s,'k',%s,
            'RECONSTRUCTING','READY_FOR_RECONSTRUCTION','RECONSTRUCTING',5,1,2,
            'INV-1048.pdf');""",
        (LIVE_TENANT, invoice_id, CUTOFF, uuid4().hex))
    cur.execute(
        """INSERT INTO invoice_sources (tenant_id,id,invoice_id,source_type,
            display_filename,mime_type,byte_length,sha256,s3_bucket_ref_private,
            s3_object_key_private,s3_version_id_private,preservation_status,
            provenance_classification,public_disclosure,verified_at,received_at)
           VALUES (%s,%s,%s,'INVOICE_PDF','INV-1048.pdf','application/pdf',1024,%s,
            'demo-bucket','intake/INV-1048.pdf','v1','VERSION_VERIFIED','DEMO_SCENARIO',
            'Representative demonstration data',%s,%s);""",
        (LIVE_TENANT, source_id, invoice_id, uuid4().hex, CUTOFF, CUTOFF))
    refs = [{"type": "invoice_source", "id": source_id, "version": 1},
            {"type": "claim_set", "id": claim_set_id, "version": 1}]
    fp = task_input_fingerprint(task_type=TaskType.START_RECONSTRUCTION, input_refs=refs)
    cur.execute(
        """INSERT INTO workflow_tasks (tenant_id,id,invoice_id,task_type,task_version,
            state,actor_display,knowledge_cutoff_at,input_fingerprint,input_object_refs,
            current_attempt,lease_owner) VALUES
            (%s,%s,%s,'START_RECONSTRUCTION',1,'RUNNING','w',%s,%s,%s,1,'wlive');""",
        (LIVE_TENANT, task_id, invoice_id, CUTOFF, fp, json.dumps(refs)))
    seed_reconstruction_memory(dal, invoice_id=invoice_id)
    return invoice_id, source_id, task_id, fp


def main() -> None:
    ssm = boto3.client("ssm", region_name="us-east-1")
    try:
        cfg = _live_mcp_config(ssm)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "MCP_CONFIG_UNAVAILABLE",
                          "hint": "run scripts/gate5b_oauth_bootstrap.py first",
                          "error": type(exc).__name__}, indent=2))
        raise SystemExit(1) from exc

    conn = psycopg.connect(_iso_dsn(), connect_timeout=20, autocommit=True)
    dal = DAL(conn, Tenant(LIVE_TENANT, "reconstruction-live"))
    with conn.cursor() as cur:
        for tbl in ["reconstruction_day_event_bindings", "reconstruction_coverage",
                    "reconstruction_charged_days", "reconstruction_events",
                    "reconstructions", "reconstruction_source_artifacts",
                    "shipment_event_memory", "workflow_task_attempts",
                    "workflow_tasks", "invoice_sources", "event_outbox",
                    "invoice_events", "invoices"]:
            cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s;", (LIVE_TENANT,))
        invoice_id, source_id, task_id, fp = _seed(cur, dal)

    # THE LIVE MANAGED MCP READ.
    try:
        with CockroachManagedMCP(cfg) as mcp:
            write_denied = mcp.verify_known_write_tool_denied()
            memory = read_reconstruction_memory(
                mcp, shipment_ref=SHIP, container_ref=SHIP,
                knowledge_cutoff_iso=CUTOFF.isoformat().replace("+00:00", "Z"),
                correlation_id=task_id)
    except MCPAuthenticationError:
        print(json.dumps({"status": "MCP_TOKEN_EXPIRED",
                          "hint": "re-run scripts/gate5b_oauth_bootstrap.py to mint a "
                                  "fresh read-only token, then retry"}, indent=2))
        conn.close()
        raise SystemExit(2) from None

    v = validate_events(list(memory.rows), knowledge_cutoff=CUTOFF,
                        shipment_ref=SHIP, container_ref=SHIP)
    boundary = resolve_charge_boundary(v.accepted)
    days = adjudicate_charged_days(charge_dates=HERO_DATES, invoice_rate_minor=35000,
                                   currency="USD", events=v.accepted, boundary=boundary)
    coverage = classify_coverage(events=v.accepted, have_invoice_source=True,
                                 have_container_identity=True, have_charged_dates=True,
                                 have_invoice_rate=True)
    terminal = resolve_terminal_state(days)
    roles = {d.charge_date.isoformat(): {
        "AVAILABILITY_BOUNDARY": [e.public_ref for e in v.accepted
                                  if e.event_type.value == "AVAILABLE"],
        "FREE_TIME_BOUNDARY": [e.public_ref for e in v.accepted
                               if e.event_type.value == "FREE_TIME_END"],
        "CHARGE_END": [e.public_ref for e in v.accepted
                       if e.event_type.value == "GATE_OUT"]} for d in days}
    lease = ReconstructionTaskLease(
        task_id=task_id, invoice_id=invoice_id, attempt=1, worker_id="wlive",
        lease_expires_at=CUTOFF, knowledge_cutoff_at=CUTOFF, input_fingerprint=fp,
        claim_set_version=1, source_id=source_id, shipment_ref=SHIP, container_ref=SHIP,
        invoice_rate_minor=35000, currency="USD",
        charge_dates=tuple(d.isoformat() for d in HERO_DATES), initiated_by=None,
        actor_display="wlive")
    completion = complete_reconstruction(
        dal, lease=lease, events=v.accepted, days=days, coverage=coverage,
        terminal_state=terminal, day_event_roles=roles,
        mcp_correlation_id=memory.correlation_id,
        mcp_query_ref_private=memory.server_request_id or memory.correlation_id,
        issue_codes=v.issue_codes)

    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (gate2-live tenant; defaultdb untouched)",
        "read_path": "LIVE CockroachDB Managed MCP (real sponsor trace)",
        "mcp_write_tool_denied": write_denied,
        "mcp_rows_returned": memory.returned_row_count,
        "mcp_server_request_id_present": bool(memory.server_request_id),
        "events_accepted": len(v.accepted),
        "reconstruction": {"state": completion.state,
                           "days_complete": completion.days_complete,
                           "days_total": completion.days_total},
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    assert write_denied, "MCP identity must be read-only"
    assert memory.returned_row_count == 5
    assert completion.state == "COMPLETE" and completion.days_complete == 7
    conn.close()


if __name__ == "__main__":
    main()

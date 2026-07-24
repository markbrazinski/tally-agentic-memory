"""Seed a completed hero (INV-1048 → DISPUTE $700) under the UI's tenant.

Runs the full pipeline (reconstruction → applicable rule → judgment) for one
fresh invoice under the tenant the local backend reads (TALLY_TENANT_ID, default
the gate7 UI tenant), and LEAVES IT in place so GET /api/invoices and
GET /api/invoices/{id}/reconstruction return real COMPLETE data for the workbench.

Reuses the Gate-7 integrated trace's seed + stage functions. Idempotent: resets
the UI tenant, reseeds, and runs the pipeline. The recommendation is left FROZEN
and unapproved so the UI can drive the human Approve step live.

Writes only to the UI tenant in tally_gate2_iso. Never touches defaultdb.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import boto3
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.external.dal import DAL, Tenant  # noqa: E402
from src.external.reconstruction_seed import seed_reconstruction_memory  # noqa: E402
from src.external.titan_embeddings import TitanTextEmbeddingsV2  # noqa: E402

# Load the gate7 trace module for its seed + stage helpers.
_g7_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gate7_integrated_trace.py")
_spec = importlib.util.spec_from_file_location("g7ui", _g7_path)
g7 = importlib.util.module_from_spec(_spec)
sys.modules["g7ui"] = g7
_spec.loader.exec_module(g7)

# The tenant the local backend reads (matches RUN_LOCAL.md).
UI_TENANT = os.environ.get("TALLY_TENANT_ID", "10000000-0000-4000-8000-00000000a007")


def _seed_approver_user(dsn: str) -> None:
    """Ensure the demo approver (rachel.martinez) exists under the UI tenant so
    the live Approve endpoint can resolve the authenticated user."""
    from src.external import seed_demo_tenant as sdt

    with psycopg.connect(dsn, connect_timeout=15, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s) "
                        "ON CONFLICT (id) DO NOTHING;", (UI_TENANT, "Gate7 UI"))
            cur.execute(
                """
                INSERT INTO users (tenant_id, email, display_name, title, role)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, email) DO NOTHING;
                """,
                (UI_TENANT, sdt.DEMO_USER["email"], sdt.DEMO_USER["display_name"],
                 sdt.DEMO_USER["title"], sdt.DEMO_USER["role"]),
            )


def main() -> None:
    # Point the gate7 helpers at the UI tenant.
    g7.G7_TENANT = UI_TENANT
    dsn = _iso_dsn()
    _seed_approver_user(dsn)
    embedder = TitanTextEmbeddingsV2(
        boto3.client("bedrock-runtime", region_name="us-east-1")
    )
    conn = psycopg.connect(dsn, connect_timeout=25, autocommit=True)
    with conn.cursor() as cur:
        g7.reset(cur)
        invoice_id, source_id, claim_set_id, task_id, fp = g7.seed_invoice(cur)
    dal = DAL(conn, Tenant(UI_TENANT, "ui-seed"))
    seed_reconstruction_memory(dal, invoice_id=invoice_id)

    with conn.cursor() as cur:
        recon = g7.run_reconstruction(dal, cur, invoice_id=invoice_id,
                                      source_id=source_id, task_id=task_id, fp=fp)
        rule, index_selected, _decision = g7.run_rule(
            dal, cur, embedder, invoice_id=invoice_id,
            reconstruction_id=recon.reconstruction_id)
        judgment = g7.run_judgment(dal, cur, invoice_id=invoice_id,
                                   reconstruction_id=recon.reconstruction_id)

    result = {
        "status": "SEEDED",
        "tenant_id": UI_TENANT,
        "invoice_id": invoice_id,
        "reconstruction_state": recon.state,
        "applicable_rule_state": rule.state,
        "vector_index_selected": index_selected,
        "recommendation": judgment.recommendation_type,
        "disputed_minor": judgment.disputed_amount_minor,
        "next": "start the backend with TALLY_TENANT_ID above, then npm run dev",
    }
    print(json.dumps(result, indent=2))
    conn.close()


if __name__ == "__main__":
    main()

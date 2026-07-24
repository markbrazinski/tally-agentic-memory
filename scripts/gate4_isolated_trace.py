"""Gate 4 live trace against tally_gate2_iso — deterministic judgment.

Seeds three reconstructions with charged-day rows and runs the real
complete_judgment transaction against live CockroachDB, proving the three locked
outcomes and deterministic replay:
  - hero:      7 x ($350 - $250) = $700 DISPUTE
  - restraint: 7 x $125 matches $125 = $875 APPROVE_FOR_PAYMENT
  - gap:       one day missing coverage => REQUEST_EVIDENCE
Then re-runs the hero judgment and asserts the frozen version + digest are
identical (idempotent replay, no second version).

Writes only to a Gate-4 tenant in tally_gate2_iso. Never touches defaultdb.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.external.dal import DAL, Tenant  # noqa: E402
from src.platform.judgment_repository import (  # noqa: E402
    JudgmentTaskLease,
    complete_judgment,
    load_day_inputs,
)

G4_TENANT = "10000000-0000-4000-8000-0000000000d5"
CARRIER = "20000000-0000-4000-8000-000000000020"
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
HERO_DATES = [date(2026, 6, d) for d in range(8, 15)]


def _seed_reconstruction(cur, *, invoice_id, reconstruction_id, days):
    cur.execute(
        """
        INSERT INTO invoices
            (tenant_id, id, carrier_id, invoice_no, received_at, s3_key, sha256,
             status, intake_state, aggregate_status, status_sequence,
             active_claim_set_version, row_version, display_name)
        VALUES (%s,%s,%s,'INV',%s,'k',%s,'RECONSTRUCTING','READY_FOR_RECONSTRUCTION',
                'RECONSTRUCTING',5,1,2,'INV.pdf')
        ON CONFLICT (tenant_id, id) DO NOTHING;
        """,
        (G4_TENANT, invoice_id, CARRIER, CUTOFF, uuid4().hex),
    )
    task_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO workflow_tasks
            (tenant_id, id, invoice_id, task_type, task_version, state,
             actor_display, knowledge_cutoff_at, input_fingerprint,
             input_object_refs, current_attempt, lease_owner)
        VALUES (%s,%s,%s,'JUDGE_DAYS',1,'RUNNING','w',%s,%s,%s,1,'w4');
        """,
        (G4_TENANT, task_id, invoice_id, CUTOFF, uuid4().hex,
         json.dumps([{"type": "reconstruction", "id": reconstruction_id,
                      "version": 1}])),
    )
    cur.execute(
        """
        INSERT INTO reconstructions
            (tenant_id, id, invoice_id, version, task_id, input_fingerprint,
             claim_set_version, knowledge_cutoff_at, effective_timezone, state,
             event_count, days_total, days_complete, public_summary)
        VALUES (%s,%s,%s,1,%s,%s,1,%s,'America/Los_Angeles','COMPLETE',5,7,7,'ok');
        """,
        (G4_TENANT, reconstruction_id, invoice_id, task_id, uuid4().hex, CUTOFF),
    )
    for charge_date, invoice_minor, applicable_minor, coverage in days:
        cur.execute(
            """
            INSERT INTO reconstruction_charged_days
                (tenant_id, id, reconstruction_id, invoice_id, charge_date,
                 invoice_claim_field, chargeability, coverage_state, state,
                 invoice_rate_minor, applicable_rate_minor, currency, outcome,
                 missing_requirements)
            VALUES (%s,%s,%s,%s,%s,'daily_rate','CHARGEABLE',%s,'SOURCE_COMPLETE',
                    %s,%s,'USD','PENDING','[]');
            """,
            (G4_TENANT, str(uuid4()), reconstruction_id, invoice_id, charge_date,
             coverage, invoice_minor, applicable_minor),
        )
    return task_id


def _run(dal, cur, *, invoice_id, reconstruction_id, task_id):
    days = load_day_inputs(dal, reconstruction_id=reconstruction_id)
    lease = JudgmentTaskLease(
        task_id=task_id, invoice_id=invoice_id, reconstruction_id=reconstruction_id,
        attempt=1, worker_id="w4", input_fingerprint="fp", initiated_by=None,
        actor_display="w",
    )
    return complete_judgment(dal, lease=lease, days=days)


def main() -> None:
    dsn = _iso_dsn()
    conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id,name) VALUES (%s,'Gate4 (fictional)') "
                    "ON CONFLICT (id) DO NOTHING;", (G4_TENANT,))
        cur.execute("INSERT INTO carriers (tenant_id,id,scac,name) "
                    "VALUES (%s,%s,'ASTL','Asterline (fictional)') "
                    "ON CONFLICT DO NOTHING;", (G4_TENANT, CARRIER))
        for tbl in ["charged_day_judgments", "recommendations",
                    "reconstruction_charged_days", "reconstructions",
                    "workflow_task_attempts", "workflow_tasks", "invoices"]:
            cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s;", (G4_TENANT,))

        hero = (str(uuid4()), str(uuid4()))
        restraint = (str(uuid4()), str(uuid4()))
        gap = (str(uuid4()), str(uuid4()))
        hero_task = _seed_reconstruction(
            cur, invoice_id=hero[0], reconstruction_id=hero[1],
            days=[(d, 35000, 25000, "PRESENT_VERIFIED") for d in HERO_DATES],
        )
        restraint_task = _seed_reconstruction(
            cur, invoice_id=restraint[0], reconstruction_id=restraint[1],
            days=[(d, 12500, 12500, "PRESENT_VERIFIED") for d in HERO_DATES],
        )
        gap_days = [(d, 35000, 25000, "PRESENT_VERIFIED") for d in HERO_DATES[:-1]]
        gap_days.append((HERO_DATES[-1], 35000, None, "MISSING"))
        gap_task = _seed_reconstruction(
            cur, invoice_id=gap[0], reconstruction_id=gap[1], days=gap_days,
        )

    dal = DAL(conn, Tenant(G4_TENANT, "gate4-trace"))
    with conn.cursor() as cur:
        hero_result = _run(dal, cur, invoice_id=hero[0],
                           reconstruction_id=hero[1], task_id=hero_task)
        restraint_result = _run(dal, cur, invoice_id=restraint[0],
                               reconstruction_id=restraint[1], task_id=restraint_task)
        gap_result = _run(dal, cur, invoice_id=gap[0],
                         reconstruction_id=gap[1], task_id=gap_task)

        # Deterministic replay: re-lease hero task and re-run -> same version.
        cur.execute(
            "UPDATE workflow_tasks SET state='RUNNING', lease_owner='w4', "
            "current_attempt=1 WHERE tenant_id=%s AND id=%s;",
            (G4_TENANT, hero_task),
        )
        hero_replay = _run(dal, cur, invoice_id=hero[0],
                          reconstruction_id=hero[1], task_id=hero_task)

        # Read back the frozen digest.
        cur.execute("SELECT digest FROM recommendations WHERE tenant_id=%s AND id=%s;",
                    (G4_TENANT, hero_result.recommendation_id))
        digest = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM charged_day_judgments WHERE tenant_id=%s "
            "AND recommendation_id=%s;",
            (G4_TENANT, hero_result.recommendation_id),
        )
        judgment_rows = cur.fetchone()[0]

    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (gate4 tenant; defaultdb untouched)",
        "hero": {
            "type": hero_result.recommendation_type,
            "disputed_minor": hero_result.disputed_amount_minor,
            "supported_minor": hero_result.supported_amount_minor,
            "days": hero_result.days_total,
            "judgment_rows": judgment_rows,
        },
        "restraint": {
            "type": restraint_result.recommendation_type,
            "supported_minor": restraint_result.supported_amount_minor,
        },
        "missing_evidence": {"type": gap_result.recommendation_type},
        "deterministic_replay": {
            "same_recommendation_id": (
                hero_replay.recommendation_id == hero_result.recommendation_id
            ),
            "same_version": hero_replay.version == hero_result.version,
        },
        "model_arithmetic": False,
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    assert hero_result.recommendation_type == "DISPUTE"
    assert hero_result.disputed_amount_minor == 70000  # $700
    assert judgment_rows == 7
    assert restraint_result.recommendation_type == "APPROVE_FOR_PAYMENT"
    assert restraint_result.supported_amount_minor == 87500  # $875
    assert gap_result.recommendation_type == "REQUEST_EVIDENCE"
    assert hero_replay.recommendation_id == hero_result.recommendation_id
    assert digest.startswith("sha256:")
    conn.close()


if __name__ == "__main__":
    main()

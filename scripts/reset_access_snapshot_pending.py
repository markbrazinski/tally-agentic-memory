"""Reset the retained June-11 terminal-access snapshot to PENDING for a repeatable
acceptance run.

This is a demo-pacing reset of *verification state only* — it flips
shipment_event_memory.source_version_state VERIFIED -> PENDING for the single
retained June-11 snapshot so the next fresh import reconstructs 6/7 (the snapshot
is again invisible to the VERIFIED-only MCP view) before the controlled release
re-verifies it to 7/7. It never inserts, never edits source content, timestamps,
identifiers, or version references — exactly the release/re-hold the worker itself
performs, run in reverse for the next pass. The user authorized a controlled
release for demo pacing; a second-pass insert is prohibited, and this is not one.

Usage:
  TALLY_CRDB_DSN=... python -m scripts.reset_access_snapshot_pending [PUBLIC_REF]
"""

from __future__ import annotations

import os
import sys

import psycopg

PUBLIC_REF = sys.argv[1] if len(sys.argv) > 1 else "SE-INV1048-AX-0611"


def main() -> int:
    dsn = os.environ["TALLY_CRDB_DSN"]
    with psycopg.connect(dsn, connect_timeout=15, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE shipment_event_memory
            SET source_version_state='PENDING'
            WHERE public_ref=%s AND event_type='TERMINAL_ACCESS_SNAPSHOT'
              AND source_version_state='VERIFIED';
            """,
            (PUBLIC_REF,),
        )
        changed = cur.rowcount
        cur.execute(
            "SELECT source_version_state FROM shipment_event_memory WHERE public_ref=%s;",
            (PUBLIC_REF,),
        )
        state = (cur.fetchone() or [None])[0]
    # Read-back assertion: intent (PENDING) == effect.
    assert state == "PENDING", f"reset failed: {PUBLIC_REF} is {state}, expected PENDING"
    print(f"{PUBLIC_REF} -> PENDING (rows changed: {changed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

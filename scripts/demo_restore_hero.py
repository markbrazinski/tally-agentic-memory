"""CLI wrapper: restore INV-1048 to READY_FOR_REVIEW.

The logic lives in ``src.platform.demo_restore`` because the deployed API
imports it — the judge-facing "Restore demo" button calls ``restore_hero()`` at
runtime, and the Docker image copies only ``src/`` and ``contract/``. Keeping it
under ``scripts/`` made the import fail in the container with a 500 while
working fine locally, where the repo root is on the path.

This wrapper is the between-takes command. It reads TALLY_CRDB_DSN and
TALLY_TENANT_ID from the environment and prints a read-back.

Usage:
  AWS_PROFILE=gate5-deployer python -m scripts.demo_restore_hero
"""

from __future__ import annotations

import os

import psycopg

from src.platform.demo_restore import HERO_DISPLAY_NAME, HERO_EVENTS, restore_hero


def main() -> int:
    tenant_id = os.environ["TALLY_TENANT_ID"]
    conn = psycopg.connect(
        os.environ["TALLY_CRDB_DSN"], connect_timeout=20, autocommit=True
    )
    with conn.cursor() as cur:
        result = restore_hero(cur, tenant_id)
    conn.close()
    assert result["aggregate_status"] == "READY_FOR_REVIEW", result
    assert result["recommendation"] == ("DISPUTE", "FROZEN", 70000), result
    assert result["event_count"] == len(HERO_EVENTS), result
    print(
        f"hero restored: {HERO_DISPLAY_NAME} → READY_FOR_REVIEW, "
        f"rec={result['recommendation']}, "
        f"timeline={result['event_count']} events "
        f"(instantly approvable, no pipeline wait)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

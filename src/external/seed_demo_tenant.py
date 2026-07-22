"""One-time idempotent seed: the demo tenant + carriers recording/commit.py needs.

Not a migration (data, not schema) and not story-plane seeding (TDD ss7's
synthetic decisions/policy corpus - Bundle 0's job). This is the minimum
world-plane data (TDD ss7 Phase 1: "direct inserts: entities, not outcomes")
the live capture-to-DB commit path needs to exist at all: one tenant row
and one carriers row per registered capture/sources.py source.

Safe to re-run: every insert is ON CONFLICT DO NOTHING against a unique key
(tenants.name is not unique in the schema, so this script's own idempotency
for the tenant relies on checking by name first, matching how run_seed()
below is written).
"""

from __future__ import annotations

import psycopg

from src.external.db import connect

DEMO_TENANT_NAME = "Meridian Demo"

# One carriers row per capture/sources.py source, keyed by SCAC-like code so
# recording/commit.py can look up carrier_id by a stable string rather than
# a UUID it would have to hardcode. The public entries are fictional fixtures.
DEMO_CARRIERS = (
    {"scac": "NOLU", "name": "Asterline Demo Shipping (fictional)", "lanes": ["NP-SP"]},
    {"scac": "BHMU", "name": "Bluehaven Demo Shipping (fictional)", "lanes": ["NP-SP"]},
    {"scac": "HVTM", "name": "Harborview Demo Terminal (fictional)", "lanes": ["NP-SP"]},
)

# The demo's named user is synthetic. role='approver' is what
# POST /cases/{id}/approve's bearer-auth check requires.
DEMO_USER = {
    "email": "rachel.martinez@meridianhomeandhardware.example",
    "display_name": "Rachel Martinez",
    "title": "Director, Trade Compliance",
    "role": "approver",
}


def run_seed(dsn: str | None = None) -> str:
    """Seed the demo tenant + carriers if they don't already exist.

    Returns the tenant_id (existing or newly created) as a string.

    Uses src.external.db.connect() (not a raw psycopg.connect() call) so
    this gets the same DSN resolution (SSM SecureString first, env var
    fallback), bundled-CA-cert handling, and autocommit=True behavior as
    every other connection in this codebase - a second, independent
    connection helper here previously bypassed both fixes, which is
    exactly how this function kept failing with libpq's default
    ~/.postgresql/root.crt path in the deployed Lambda even after
    db.connect() itself was fixed.
    """
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenants WHERE name = %s;", (DEMO_TENANT_NAME,))
            row = cur.fetchone()
            if row:
                tenant_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id;",
                    (DEMO_TENANT_NAME,),
                )
                tenant_id = cur.fetchone()[0]

            for carrier in DEMO_CARRIERS:
                lanes = psycopg.types.json.Json(carrier["lanes"])
                cur.execute(
                    """
                    INSERT INTO carriers (tenant_id, scac, name, lanes)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, scac) DO NOTHING;
                    """,
                    (tenant_id, carrier["scac"], carrier["name"], lanes),
                )

            cur.execute(
                """
                INSERT INTO users (tenant_id, email, display_name, title, role)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, email) DO NOTHING;
                """,
                (
                    tenant_id,
                    DEMO_USER["email"],
                    DEMO_USER["display_name"],
                    DEMO_USER["title"],
                    DEMO_USER["role"],
                ),
            )
    return str(tenant_id)


if __name__ == "__main__":
    tenant_id = run_seed()
    print(f"demo tenant_id: {tenant_id}")

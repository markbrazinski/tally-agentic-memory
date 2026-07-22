from pathlib import Path


def test_gate5_retention_migration_covers_every_hero_lineage_table() -> None:
    sql = Path("migrations/006_gate5_hero_lineage_retention.sql").read_text(
        encoding="utf-8"
    )
    newly_covered = {
        "tenants",
        "users",
        "carriers",
        "invoices",
        "clerk_runs",
        "findings",
        "ledger_events",
        "contests",
        "query_log",
    }
    already_covered = {
        "tariff_snapshots",
        "tariff_clauses",
        "cases",
        "case_evidence",
    }

    for table in newly_covered:
        assert (
            f"ALTER TABLE {table} CONFIGURE ZONE USING gc.ttlseconds = 7776000;"
            in sql
        )
    for table in already_covered:
        assert f"ALTER TABLE {table} " not in sql
    assert "restore" not in "\n".join(
        line for line in sql.lower().splitlines() if not line.lstrip().startswith("--")
    )

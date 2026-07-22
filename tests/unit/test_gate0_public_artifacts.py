"""Executable checks for the synthetic/public Gate 0 evidence bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "recovery" / "gate-0"


def _load(name: str) -> dict:
    return json.loads((ARTIFACTS / name).read_text())


def test_three_synthetic_exact_versions_match_bytes_hashes_and_sizes():
    report = _load("hash-verification.example.json")

    assert report["classification"].startswith("synthetic")
    assert len(report["samples"]) >= 3
    for sample in report["samples"]:
        body = (ROOT / sample["fixture"]).read_bytes()
        assert len(body) == sample["expected_size"]
        assert hashlib.sha256(body).hexdigest() == sample["expected_sha256"]
        assert sample["version_id"].startswith("fixture-version-")


def test_public_inventory_and_hash_report_bind_the_same_synthetic_versions():
    inventory = _load("capture-inventory.example.json")
    hashes = _load("hash-verification.example.json")

    inventory_versions = {entry["version_id"] for entry in inventory["entries"]}
    hash_versions = {entry["version_id"] for entry in hashes["samples"]}
    assert inventory_versions == hash_versions


def test_sanitized_replay_aggregate_proves_equivalence_and_idempotency():
    counts = _load("row-counts.json")

    assert counts["source"] == counts["isolated_replay_after_first_run"]
    assert counts["isolated_replay_after_first_run"] == counts["isolated_replay_after_second_run"]
    assert set(counts["symmetric_row_differences"].values()) == {0}


def test_sanitized_schema_contains_replay_tables_and_vector_index():
    schema = (ARTIFACTS / "schema.sql").read_text()

    for table in ("recordings", "tariff_snapshots", "schema_migrations"):
        assert f"CREATE TABLE public.{table}" in schema
    assert "VECTOR INDEX" in schema

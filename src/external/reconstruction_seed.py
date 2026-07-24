"""Seed the representative pre-invoice shipment-event memory (Gate 2).

Loads the clearly-fictional representative source package and inserts it into
``shipment_event_memory`` (read by the Managed MCP ``mcp_reconstruction_memory_v1``
view) and ``reconstruction_source_artifacts``. Idempotent: re-running upserts on
public_ref. All rows are DEMO_SCENARIO / representative; nothing here is a live
carrier or terminal feed.

Build-time only. Never invents events beyond the disclosed fixture.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.external.dal import DAL

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "demo" / "INV-1048.reconstruction-events.json"
)


def load_source_package(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or FIXTURE).read_text())


def seed_reconstruction_memory(
    dal: DAL,
    *,
    invoice_id: str,
    package: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Insert the representative source artifacts + shipment-event memory.

    Returns row counts for readback. Source verification state is VERIFIED
    (these are retained representative artifacts); a real deployment binds each
    to an exact S3 version.
    """
    pkg = package or load_source_package()
    tenant_id = dal.tenant.tenant_id
    counts = {"source_artifacts": 0, "shipment_events": 0}

    def _seed(conn):
        with conn.cursor() as cur:
            for artifact in pkg["source_artifacts"]:
                cur.execute(
                    """
                    INSERT INTO reconstruction_source_artifacts
                        (tenant_id, invoice_id, public_ref, source_type,
                         display_name, mime_type, provenance_classification,
                         public_disclosure, adapter_name, s3_bucket_ref_private,
                         s3_object_key_private, s3_version_id_private, sha256,
                         byte_length, verification_state, recorded_at, verified_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'representative-demo-bucket',
                            %s, 'representative-v1', %s, 0, 'VERIFIED',
                            now(), now())
                    ON CONFLICT (tenant_id, public_ref) DO NOTHING;
                    """,
                    (
                        tenant_id, invoice_id, artifact["public_ref"],
                        artifact["source_type"], artifact["display_name"],
                        artifact["mime_type"], pkg["provenance_classification"],
                        pkg["disclosure"], artifact["adapter_name"],
                        f"representative/{artifact['public_ref']}",
                        _sha_placeholder(artifact["public_ref"]),
                    ),
                )
                counts["source_artifacts"] += 1

            for event in pkg["events"]:
                cur.execute(
                    """
                    INSERT INTO shipment_event_memory
                        (tenant_id, public_ref, shipment_ref, container_ref,
                         event_type, source_public_ref, source_version_state,
                         display_anchor, provenance_classification, occurred_at,
                         recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'VERIFIED', %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, public_ref) DO NOTHING;
                    """,
                    (
                        tenant_id, event["public_ref"], pkg["shipment_ref"],
                        pkg["container_ref"], event["event_type"],
                        event["source_public_ref"], event["display_anchor"],
                        pkg["provenance_classification"],
                        _parse(event["occurred_at"]), _parse(event["recorded_at"]),
                    ),
                )
                counts["shipment_events"] += 1
        return counts

    return dal.run_with_retry(_seed)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sha_placeholder(ref: str) -> str:
    import hashlib

    return hashlib.sha256(ref.encode()).hexdigest()

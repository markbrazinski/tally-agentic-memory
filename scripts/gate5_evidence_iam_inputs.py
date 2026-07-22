"""Derive exact S3 object ARNs from the sealed synthetic hero receipt."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from src.external.dal import DAL, Tenant
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-5/evidence-object-arns.private.json")


def _database_dsn(dsn: str, database: str) -> str:
    if not dsn or not database or any(character.isspace() for character in database):
        raise ValueError("database_configuration_invalid")
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise ValueError("database_configuration_invalid")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + quote(database, safe=""), parsed.query, "")
    )


def derive_object_arns(manifest_value: Any) -> tuple[str, ...]:
    manifest = (
        json.loads(manifest_value) if isinstance(manifest_value, str) else manifest_value
    )
    if not isinstance(manifest, Mapping):
        raise ValueError("sealed_manifest_invalid")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("sealed_manifest_invalid")
    arns: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("sealed_manifest_invalid")
        for bucket_field, key_field, version_field in (
            ("s3_bucket", "s3_key", "s3_version_id"),
            ("invoice_s3_bucket", "invoice_s3_key", "invoice_s3_version_id"),
        ):
            bucket = item.get(bucket_field)
            key = item.get(key_field)
            version = item.get(version_field)
            if not all(isinstance(value, str) and value for value in (bucket, key, version)):
                raise ValueError("sealed_manifest_source_binding_invalid")
            if any(value in bucket for value in ("/", "*", "?")) or any(
                value in key for value in ("*", "?")
            ):
                raise ValueError("sealed_manifest_source_binding_invalid")
            arns.add(f"arn:aws:s3:::{bucket}/{key}")
    return tuple(sorted(arns))


def main() -> int:
    stage = "configuration"
    try:
        tenant_id = os.environ["TALLY_TENANT_ID"]
        case_id = os.environ["TALLY_PUBLIC_DEMO_CASE_ID"]
        dsn = _database_dsn(
            os.environ["TALLY_CRDB_DSN"], os.environ["TALLY_MCP_DATABASE"]
        )
        stage = "database_read"
        with DAL.connect(
            Tenant(tenant_id=tenant_id, actor="gate5-iam-inputs"), dsn=dsn
        ) as dal:
            rows = dal.execute(
                "SELECT evidence_manifest FROM cases WHERE tenant_id=%s AND id=%s",
                (case_id,),
                tag="gate5.iam_inputs",
            )
        if len(rows) != 1:
            raise ValueError("sealed_hero_not_found")
        stage = "manifest_validation"
        arns = derive_object_arns(rows[0][0])
        stage = "private_write"
        write_private_json(
            Path(os.environ.get("TALLY_GATE5_EVIDENCE_ARNS_OUTPUT", str(DEFAULT_OUTPUT))),
            {
                "classification": "PRIVATE GATE 5 IAM INPUT",
                "object_arns": list(arns),
                "derived_from_sealed_receipt": True,
            },
        )
        print(f"object_arns={len(arns)}")
        print("passed=true")
        return 0
    except Exception as exc:
        print("object_arns=0")
        print(f"error_stage={stage}")
        print(f"error_class={type(exc).__name__}")
        print("passed=false")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

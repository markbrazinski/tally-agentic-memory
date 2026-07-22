"""Upload Gate 2's committed synthetic sources to a private versioned bucket."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import boto3

from src.external.versioned_source import RetainedObject, S3VersionedSource
from src.platform.private_artifacts import DEFAULT_PRIVATE_ROOT, write_private_json

DEFAULT_BUCKET_ENV = "TALLY_GATE2_BUCKET"
DEFAULT_INVENTORY_PATH = Path("runtime-artifacts/gate-2/source-inventory.json")
TARIFF_UPLOAD_DELAY_SECONDS = 1.05
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gate2"


class S3Client(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str, VersionId: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UploadItem:
    alias: str
    body: bytes
    suffix: str


@dataclass(frozen=True)
class UploadSummary:
    passed: bool
    object_count: int
    versioning_verified: bool
    distinct_observations_verified: bool

    def as_public_dict(self) -> dict[str, bool | int]:
        return {
            "passed": self.passed,
            "object_count": self.object_count,
            "versioning_verified": self.versioning_verified,
            "distinct_observations_verified": self.distinct_observations_verified,
        }


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required bucket environment variable is not set: {name}")
    return value


def load_committed_items() -> tuple[UploadItem, ...]:
    """Read only committed public-safe fixtures; do not add operator data here."""
    clauses = json.loads((FIXTURES_DIR / "synthetic_clauses.json").read_text(encoding="utf-8"))
    documents = clauses.get("documents") if isinstance(clauses, dict) else None
    if not isinstance(documents, list):
        raise ValueError("Gate 2 clause fixture has no documents")
    items: list[UploadItem] = []
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Gate 2 clause fixture contains an invalid document")
        capture_id = document.get("capture_id")
        source_text = document.get("source_text")
        if not isinstance(capture_id, str) or not isinstance(source_text, str):
            raise ValueError("Gate 2 clause fixture lacks a capture ID or source text")
        items.append(UploadItem(f"tariff:{capture_id}", source_text.encode("utf-8"), ".txt"))
    invoice = (FIXTURES_DIR / "northstar-invoice.json").read_bytes()
    if not invoice:
        raise ValueError("Gate 2 invoice fixture is empty")
    items.append(UploadItem("invoice", invoice, ".json"))
    return tuple(items)


def upload_private(
    *,
    bucket: str,
    client: S3Client,
    inventory_path: Path = DEFAULT_INVENTORY_PATH,
    items: tuple[UploadItem, ...] | None = None,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
    sleeper: Callable[[float], None] = time.sleep,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> UploadSummary:
    """Upload, exact-version read back, and privately inventory synthetic fixtures."""
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("bucket is required")
    upload_items = items or load_committed_items()
    if not upload_items:
        raise ValueError("at least one upload item is required")
    prefix = token_factory(24)
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("private prefix generator returned no value")
    source = S3VersionedSource(client)
    inventory: dict[str, dict[str, str]] = {}
    tariff_observed: list[datetime] = []
    tariff_count = sum(item.alias.startswith("tariff:") for item in upload_items)
    invoice_count = sum(item.alias == "invoice" for item in upload_items)
    if invoice_count != 1 or upload_items[-1].alias != "invoice":
        raise ValueError("one invoice item must follow every tariff item")
    tariffs_seen = 0
    for item in upload_items:
        item_token = token_factory(24)
        if not isinstance(item_token, str) or not item_token:
            raise ValueError("private object token generator returned no value")
        key = f"{prefix}/{item_token}{item.suffix}"
        body = item.body
        if item.alias == "invoice":
            body = _invoice_body_after_tariffs(body, tariff_observed)
        response = client.put_object(Bucket=bucket, Key=key, Body=body)
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise RuntimeError("bucket versioning is not confirmed by upload response")
        retained = source.get_exact(bucket=bucket, key=key, version_id=version_id)
        _verify_readback(body, bucket, key, version_id, retained)
        inventory[item.alias] = {"bucket": bucket, "key": key, "version_id": version_id}
        if item.alias.startswith("tariff:"):
            tariff_observed.append(retained.observed_at)
            tariffs_seen += 1
            if tariffs_seen < tariff_count:
                sleeper(TARIFF_UPLOAD_DELAY_SECONDS)
    distinct_observations = len(set(tariff_observed)) == len(tariff_observed)
    if not distinct_observations:
        raise RuntimeError("tariff upload observations must be distinct")
    write_private_json(inventory_path, inventory, private_root=private_root)
    return UploadSummary(True, len(upload_items), True, distinct_observations)


def _invoice_body_after_tariffs(template: bytes, tariff_observed: list[datetime]) -> bytes:
    if not tariff_observed:
        raise ValueError("invoice upload requires observed tariff versions")
    try:
        value = json.loads(template)
    except json.JSONDecodeError as exc:
        raise ValueError("synthetic invoice template must be JSON") from exc
    if not isinstance(value, dict) or value.get("classification") != "synthetic demonstration data":
        raise ValueError("synthetic invoice template classification is required")
    received_at = max(item.astimezone(UTC) for item in tariff_observed) + timedelta(days=1)
    value["received_at"] = received_at.isoformat().replace("+00:00", "Z")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_readback(
    body: bytes, bucket: str, key: str, version_id: str, retained: RetainedObject
) -> None:
    if (
        retained.bucket != bucket
        or retained.key != key
        or retained.version_id != version_id
        or retained.body != body
        or hashlib.sha256(retained.body).digest() != hashlib.sha256(body).digest()
    ):
        raise RuntimeError("exact-version readback did not match upload")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket-env", default=DEFAULT_BUCKET_ENV)
    parser.add_argument("--private-output", type=Path, default=DEFAULT_INVENTORY_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = upload_private(
            bucket=_required_env(args.bucket_env),
            client=boto3.client("s3"),
            inventory_path=args.private_output,
        )
    except Exception:  # noqa: BLE001 - cloud exception details may contain private identifiers
        print(json.dumps({"passed": False, "failure_stage": "private_upload"}, sort_keys=True))
        return 1
    print(json.dumps(summary.as_public_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""No-network tests for the private Gate 2 synthetic-source uploader."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from scripts.gate2_upload_private import UploadItem, load_committed_items, upload_private


class Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value


class FakeS3:
    def __init__(self, *, version_id: str | None = "v1", same_time: bool = False):
        self.version_id = version_id
        self.same_time = same_time
        self.objects: dict[tuple[str, str], tuple[bytes, str, datetime]] = {}
        self.put_calls: list[tuple[str, str, bytes]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes):
        self.put_calls.append((Bucket, Key, Body))
        version = self.version_id
        if version is not None:
            observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
            if not self.same_time:
                observed += timedelta(seconds=len(self.put_calls))
            self.objects[(Key, version)] = (Body, version, observed)
        return {"VersionId": version}

    def get_object(self, *, Bucket: str, Key: str, VersionId: str):
        body, version, observed = self.objects[(Key, VersionId)]
        return {"VersionId": version, "Body": Body(body), "LastModified": observed}


def _items() -> tuple[UploadItem, ...]:
    return (
        UploadItem("tariff:capture-first", b"first", ".txt"),
        UploadItem("tariff:capture-second", b"second", ".txt"),
        UploadItem(
            "invoice",
            b'{"classification":"synthetic demonstration data",'
            b'"received_at":"<assigned-at-private-upload>"}',
            ".json",
        ),
    )


def test_uploads_exact_versions_writes_secure_private_inventory_and_uses_delays(tmp_path):
    client = FakeS3()
    delays: list[float] = []
    output = tmp_path / "private" / "source-inventory.json"
    tokens = iter(["random-private-prefix", "first-token", "second-token", "third-token"])

    summary = upload_private(
        bucket="synthetic-bucket",
        client=client,
        items=_items(),
        inventory_path=output,
        token_factory=lambda _size: next(tokens),
        sleeper=delays.append,
        private_root=tmp_path,
    )

    assert summary.as_public_dict() == {
        "passed": True,
        "object_count": 3,
        "versioning_verified": True,
        "distinct_observations_verified": True,
    }
    assert delays == [1.05]
    assert all(key.startswith("random-private-prefix/") for _, key, _ in client.put_calls)
    assert all("tariff:" not in key and "capture-" not in key for _, key, _ in client.put_calls)
    assert all("invoice" not in key for _, key, _ in client.put_calls)
    inventory = json.loads(output.read_text())
    assert set(inventory) == {"tariff:capture-first", "tariff:capture-second", "invoice"}
    assert inventory["tariff:capture-first"]["key"].endswith(".txt")
    uploaded_invoice = json.loads(client.put_calls[-1][2])
    assert uploaded_invoice["received_at"] == "2026-01-02T00:00:02Z"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_rejects_missing_version_id_before_inventory_write(tmp_path):
    with pytest.raises(RuntimeError, match="versioning"):
        upload_private(
            bucket="synthetic-bucket",
            client=FakeS3(version_id=None),
            items=_items(),
            inventory_path=tmp_path / "source-inventory.json",
            token_factory=lambda _size: "prefix",
            sleeper=lambda _delay: None,
            private_root=tmp_path,
        )


def test_rejects_observation_collision(tmp_path):
    with pytest.raises(RuntimeError, match="observations"):
        upload_private(
            bucket="synthetic-bucket",
            client=FakeS3(same_time=True),
            items=_items(),
            inventory_path=tmp_path / "source-inventory.json",
            token_factory=lambda _size: "prefix",
            sleeper=lambda _delay: None,
            private_root=tmp_path,
        )


def test_refuses_mismatched_returned_version(tmp_path):
    client = FakeS3()
    original = client.get_object

    def mismatch(**kwargs):
        response = original(**kwargs)
        response["VersionId"] = "wrong-version"
        return response

    client.get_object = mismatch  # type: ignore[method-assign]
    with pytest.raises(Exception):
        upload_private(
            bucket="synthetic-bucket",
            client=client,
            items=_items(),
            inventory_path=tmp_path / "source-inventory.json",
            token_factory=lambda _size: "prefix",
            sleeper=lambda _delay: None,
            private_root=tmp_path,
        )


def test_private_inventory_refuses_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("do not overwrite")
    output = tmp_path / "source-inventory.json"
    output.symlink_to(target)

    with pytest.raises(OSError):
        upload_private(
            bucket="synthetic-bucket",
            client=FakeS3(),
            items=_items(),
            inventory_path=output,
            token_factory=lambda _size: "prefix",
            sleeper=lambda _delay: None,
            private_root=tmp_path,
        )
    assert target.read_text() == "do not overwrite"


def test_committed_tariff_inventory_aliases_use_capture_ids():
    items = load_committed_items()

    assert "tariff:capture-northstar-2026-01" in {item.alias for item in items}
    assert not any("clause-northstar-250" in item.alias for item in items)

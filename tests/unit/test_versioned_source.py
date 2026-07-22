from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.external.versioned_source import ExactVersionMismatchError, S3VersionedSource


class Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class Client:
    def __init__(self, *, returned_version="fixture-version-1", body=b"bytes"):
        self.returned_version = returned_version
        self.body = body
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "VersionId": self.returned_version,
            "Body": Body(self.body),
            "LastModified": datetime(2026, 7, 2, 8, tzinfo=UTC),
        }


def test_get_exact_supplies_version_id_and_returns_server_observation_time():
    client = Client()

    result = S3VersionedSource(client).get_exact(
        bucket="example-bucket", key="fixture/tariff.txt", version_id="fixture-version-1"
    )

    assert client.calls == [
        {"Bucket": "example-bucket", "Key": "fixture/tariff.txt", "VersionId": "fixture-version-1"}
    ]
    assert result.body == b"bytes"
    assert result.observed_at == datetime(2026, 7, 2, 8, tzinfo=UTC)


def test_get_exact_rejects_wrong_returned_version():
    client = Client(returned_version="different-version")

    with pytest.raises(ExactVersionMismatchError):
        S3VersionedSource(client).get_exact(
            bucket="example-bucket", key="fixture/tariff.txt", version_id="fixture-version-1"
        )

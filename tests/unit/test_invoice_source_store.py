from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from src.external.invoice_source_store import (
    SourcePersistenceError,
    VersionedInvoiceSourceStore,
)


class FakeVersionedS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.get_calls = []
        self.version_id = "version-1"
        self.head_overrides = {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Key"], self.version_id)] = bytes(kwargs["Body"])
        return {"VersionId": self.version_id}

    def head_object(self, **kwargs):
        body = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        result = {
            "VersionId": kwargs["VersionId"],
            "ContentLength": len(body),
            "Metadata": {"tally-sha256": hashlib.sha256(body).hexdigest()},
            "ChecksumSHA256": self.put_calls[-1]["ChecksumSHA256"],
        }
        result.update(self.head_overrides)
        return result

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"Body": BytesIO(self.objects[(kwargs["Key"], kwargs["VersionId"])])}


def test_preserve_requires_and_verifies_the_exact_returned_version():
    client = FakeVersionedS3()
    store = VersionedInvoiceSourceStore(client, bucket="private-bucket")

    result = store.preserve(source_id="source-1", body=b"%PDF-1.7\ninvoice")

    assert result.version_id_private == "version-1"
    assert client.put_calls[0]["Metadata"]["tally-sha256"] == result.sha256
    assert store.get_exact(result) == b"%PDF-1.7\ninvoice"
    assert client.get_calls == [
        {
            "Bucket": "private-bucket",
            "Key": "invoice-sources/source-1/invoice.pdf",
            "VersionId": "version-1",
        }
    ]


def test_preserve_fails_when_versioning_does_not_return_a_version():
    client = FakeVersionedS3()
    client.version_id = ""

    with pytest.raises(SourcePersistenceError, match="SOURCE_VERSION_MISSING"):
        VersionedInvoiceSourceStore(client, bucket="private-bucket").preserve(
            source_id="source-1",
            body=b"%PDF-1.7\ninvoice",
        )


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"VersionId": "other"}, "SOURCE_VERSION_MISMATCH"),
        ({"ContentLength": 1}, "SOURCE_LENGTH_MISMATCH"),
        ({"Metadata": {"tally-sha256": "bad"}}, "SOURCE_CHECKSUM_MISMATCH"),
        ({"ChecksumSHA256": "bad"}, "SOURCE_CHECKSUM_MISMATCH"),
    ],
)
def test_preserve_fails_closed_on_exact_version_mismatch(override, code):
    client = FakeVersionedS3()
    client.head_overrides = override

    with pytest.raises(SourcePersistenceError, match=code):
        VersionedInvoiceSourceStore(client, bucket="private-bucket").preserve(
            source_id="source-1",
            body=b"%PDF-1.7\ninvoice",
        )


def test_get_exact_never_omits_version_id_or_falls_back_to_latest():
    client = FakeVersionedS3()
    store = VersionedInvoiceSourceStore(client, bucket="private-bucket")
    source = store.preserve(source_id="source-1", body=b"%PDF-1.7\ninvoice")
    client.objects[(source.object_key_private, source.version_id_private)] = b"changed"

    with pytest.raises(SourcePersistenceError, match="SOURCE_CHECKSUM_MISMATCH"):
        store.get_exact(source)
    assert client.get_calls[0]["VersionId"] == source.version_id_private


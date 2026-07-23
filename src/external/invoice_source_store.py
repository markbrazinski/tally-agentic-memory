"""Exact-version S3 preservation for received invoice PDFs."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass


class SourcePersistenceError(RuntimeError):
    """A safe dependency error; provider details stay in private logs."""


@dataclass(frozen=True)
class StoredInvoiceSource:
    bucket_ref_private: str
    object_key_private: str
    version_id_private: str
    sha256: str
    byte_length: int


class VersionedInvoiceSourceStore:
    def __init__(self, s3_client, *, bucket: str, key_prefix: str = "invoice-sources"):
        self._client = s3_client
        self._bucket = bucket
        self._key_prefix = key_prefix.strip("/")

    def preserve(self, *, source_id: str, body: bytes) -> StoredInvoiceSource:
        digest = hashlib.sha256(body).hexdigest()
        checksum_b64 = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        key = f"{self._key_prefix}/{source_id}/invoice.pdf"
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/pdf",
                ChecksumSHA256=checksum_b64,
                Metadata={"tally-sha256": digest},
            )
            version_id = response.get("VersionId")
            if not isinstance(version_id, str) or not version_id.strip():
                raise SourcePersistenceError("SOURCE_VERSION_MISSING")
            head = self._client.head_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
                ChecksumMode="ENABLED",
            )
        except SourcePersistenceError:
            raise
        except Exception as exc:
            raise SourcePersistenceError("SOURCE_PERSISTENCE_UNAVAILABLE") from exc

        if head.get("VersionId") != version_id:
            raise SourcePersistenceError("SOURCE_VERSION_MISMATCH")
        if int(head.get("ContentLength", -1)) != len(body):
            raise SourcePersistenceError("SOURCE_LENGTH_MISMATCH")
        metadata = head.get("Metadata") or {}
        if metadata.get("tally-sha256") != digest:
            raise SourcePersistenceError("SOURCE_CHECKSUM_MISMATCH")
        returned_checksum = head.get("ChecksumSHA256")
        if returned_checksum is not None and returned_checksum != checksum_b64:
            raise SourcePersistenceError("SOURCE_CHECKSUM_MISMATCH")

        return StoredInvoiceSource(
            bucket_ref_private=self._bucket,
            object_key_private=key,
            version_id_private=version_id,
            sha256=digest,
            byte_length=len(body),
        )

    def get_exact(self, source: StoredInvoiceSource) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=source.bucket_ref_private,
                Key=source.object_key_private,
                VersionId=source.version_id_private,
            )
            body = response["Body"].read()
        except Exception as exc:
            raise SourcePersistenceError("SOURCE_VERSION_UNAVAILABLE") from exc
        if len(body) != source.byte_length or hashlib.sha256(body).hexdigest() != source.sha256:
            raise SourcePersistenceError("SOURCE_CHECKSUM_MISMATCH")
        return body


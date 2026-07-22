"""Exact-version object-store reader used by Gate 1 and its verifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ExactVersionMismatchError(Exception):
    pass


@dataclass(frozen=True)
class RetainedObject:
    bucket: str
    key: str
    version_id: str
    body: bytes
    observed_at: datetime

    @property
    def size(self) -> int:
        return len(self.body)


class S3VersionedSource:
    def __init__(self, client: Any):
        self._client = client

    def get_exact(self, *, bucket: str, key: str, version_id: str) -> RetainedObject:
        if not all((bucket, key, version_id)):
            raise ValueError("bucket, key, and version_id are required")
        response = self._client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        returned_version = response.get("VersionId")
        if returned_version != version_id:
            raise ExactVersionMismatchError("object store returned a different version")
        body = response["Body"].read()
        if not isinstance(body, bytes):
            raise TypeError("object body must be bytes")
        observed_at = response.get("LastModified")
        if not isinstance(observed_at, datetime):
            raise ValueError("object response has no server-set LastModified timestamp")
        return RetainedObject(
            bucket=bucket,
            key=key,
            version_id=version_id,
            body=body,
            observed_at=observed_at,
        )

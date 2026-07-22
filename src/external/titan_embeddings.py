"""Safe adapter for Amazon Titan Text Embeddings V2.

Embedding requests have a stable, hashable input representation so a caller can
record provenance without retaining or logging the input text or its vector.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import boto3

MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024
NORMALIZE = True
BEDROCK_REGION = "us-east-1"
UNIT_NORM_TOLERANCE = 1e-3
VECTOR_HASH_VERSION = b"tally.titan-embed-f32be.v1\x00"


class BedrockRuntimeClient(Protocol):
    """Minimal client surface used by this adapter."""

    def invoke_model(self, *, modelId: str, body: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TitanEmbedding:
    """A validated normalized embedding plus non-sensitive request provenance."""

    values: tuple[float, ...]
    input_sha256: str
    embedding_sha256: str


def canonical_embedding_input(text: str) -> str:
    """Canonicalize text deterministically without collapsing meaningful whitespace.

    The request uses Unicode NFC and LF line endings. Empty/whitespace-only input
    is rejected, but leading, trailing, and interior whitespace are otherwise
    preserved so the hash identifies the exact text sent to Titan.
    """
    if not isinstance(text, str):
        raise TypeError("embedding input must be text")
    canonical = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if not canonical.strip():
        raise ValueError("embedding input must not be empty")
    return canonical


def embedding_input_sha256(text: str) -> str:
    """Hash canonical request semantics, never the returned vector.

    SHA-256 is computed over canonical UTF-8 JSON containing the model ID,
    dimensions, normalization flag, and canonical input text. Callers may store
    the resulting hex digest as provenance; this module does not log it.
    """
    payload = {
        "dimensions": DIMENSIONS,
        "inputText": canonical_embedding_input(text),
        "modelId": MODEL_ID,
        "normalize": NORMALIZE,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_vector(value: object) -> tuple[float, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != DIMENSIONS
    ):
        raise ValueError(f"Titan embedding must contain exactly {DIMENSIONS} dimensions")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError("Titan embedding contains a non-numeric value")
    vector = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("Titan embedding contains a non-finite value")
    norm = math.sqrt(math.fsum(item * item for item in vector))
    if norm == 0.0:
        raise ValueError("Titan embedding must not be zero")
    if not math.isclose(norm, 1.0, rel_tol=UNIT_NORM_TOLERANCE, abs_tol=UNIT_NORM_TOLERANCE):
        raise ValueError("Titan embedding must be unit-normalized")
    return vector


def quantize_embedding_float32(values: Sequence[float]) -> tuple[float, ...]:
    """Validate and quantize values to the exact float32 representation stored.

    CockroachDB ``VECTOR`` values are float32. The returned tuple is therefore
    the only representation callers should persist and later hash. Unit norm is
    checked both before and after quantization.
    """
    vector = _validated_vector(values)
    try:
        quantized = tuple(struct.unpack(">f", struct.pack(">f", item))[0] for item in vector)
    except struct.error as exc:
        raise ValueError("Titan embedding cannot be represented as float32") from exc
    return _validated_vector(quantized)


def _embedding_sha256_from_quantized(values: tuple[float, ...]) -> str:
    payload = VECTOR_HASH_VERSION + struct.pack(">I", DIMENSIONS)
    payload += b"".join(struct.pack(">f", item) for item in values)
    return hashlib.sha256(payload).hexdigest()


def embedding_sha256(values: Sequence[float]) -> str:
    """Hash the versioned, big-endian float32 vector representation.

    Use this helper when reloading a vector from CockroachDB. It re-quantizes
    and re-validates the values, so equivalent float32 round trips produce the
    same digest while a changed stored value produces a different digest.
    """
    return _embedding_sha256_from_quantized(quantize_embedding_float32(values))


class TitanTextEmbeddingsV2:
    """Invoke Titan V2 with its fixed 1024-dimension normalized contract."""

    def __init__(self, client: BedrockRuntimeClient | None = None) -> None:
        self._client = client or boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def embed(self, text: str) -> TitanEmbedding:
        canonical = canonical_embedding_input(text)
        body = json.dumps(
            {"inputText": canonical, "dimensions": DIMENSIONS, "normalize": NORMALIZE},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        response = self._client.invoke_model(modelId=MODEL_ID, body=body)
        try:
            payload = json.loads(response["body"].read())
            vector = quantize_embedding_float32(payload["embedding"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Titan embedding response is malformed") from exc
        return TitanEmbedding(
            values=vector,
            input_sha256=embedding_input_sha256(canonical),
            embedding_sha256=_embedding_sha256_from_quantized(vector),
        )

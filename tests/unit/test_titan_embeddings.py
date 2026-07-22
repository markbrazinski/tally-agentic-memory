"""Unit tests for the Titan V2 embedding adapter; all Bedrock calls are fake."""

from __future__ import annotations

import json
import math

import pytest

from src.external.titan_embeddings import (
    DIMENSIONS,
    MODEL_ID,
    TitanTextEmbeddingsV2,
    canonical_embedding_input,
    embedding_input_sha256,
    embedding_sha256,
    quantize_embedding_float32,
)


class FakeStreamingBody:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class FakeBedrockClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId: str, body: str) -> dict:
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        return {"body": FakeStreamingBody(self.payload)}


def _unit_vector() -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[0] = 1.0
    return vector


def test_embed_uses_titan_v2_normalized_1024_dimension_contract():
    client = FakeBedrockClient({"embedding": _unit_vector()})

    result = TitanTextEmbeddingsV2(client=client).embed("Tariff clause\r\n$250/day")

    assert client.calls == [{
        "modelId": MODEL_ID,
        "body": {"inputText": "Tariff clause\n$250/day", "dimensions": 1024, "normalize": True},
    }]
    assert len(result.values) == DIMENSIONS
    assert result.input_sha256 == embedding_input_sha256("Tariff clause\n$250/day")
    assert result.embedding_sha256 == embedding_sha256(result.values)


def test_canonical_input_and_hash_are_stable_and_model_bound():
    assert canonical_embedding_input("caf\u0065\u0301\rline") == "caf\u00e9\nline"
    assert embedding_input_sha256("caf\u0065\u0301\rline") == embedding_input_sha256(
        "caf\u00e9\nline"
    )
    assert len(embedding_input_sha256("tariff")) == 64


@pytest.mark.parametrize(
    "value",
    [
        [],
        [0.0] * DIMENSIONS,
        [float("nan")] + [0.0] * (DIMENSIONS - 1),
        [2.0] + [0.0] * (DIMENSIONS - 1),
    ],
)
def test_embed_rejects_invalid_or_non_normalized_vectors(value: list[float]):
    client = FakeBedrockClient({"embedding": value})

    with pytest.raises(ValueError):
        TitanTextEmbeddingsV2(client=client).embed("synthetic clause")


def test_embed_accepts_finite_unit_vector():
    vector = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2)] + [0.0] * (DIMENSIONS - 2)
    result = TitanTextEmbeddingsV2(
        client=FakeBedrockClient({"embedding": vector})
    ).embed("synthetic")

    assert math.isclose(
        math.sqrt(math.fsum(item * item for item in result.values)), 1.0, abs_tol=1e-3
    )


def test_float32_vector_hash_matches_equivalent_reload_and_detects_change():
    source = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2)] + [0.0] * (DIMENSIONS - 2)
    result = TitanTextEmbeddingsV2(
        client=FakeBedrockClient({"embedding": source})
    ).embed("synthetic")

    reloaded = list(result.values)
    changed = [0.6, 0.8] + [0.0] * (DIMENSIONS - 2)

    assert quantize_embedding_float32(source) == tuple(reloaded)
    assert embedding_sha256(reloaded) == result.embedding_sha256
    assert embedding_sha256(changed) != result.embedding_sha256


def test_embed_rejects_malformed_response_and_empty_input():
    with pytest.raises(ValueError, match="malformed"):
        TitanTextEmbeddingsV2(client=FakeBedrockClient({})).embed("synthetic")
    with pytest.raises(ValueError, match="must not be empty"):
        TitanTextEmbeddingsV2(client=FakeBedrockClient({"embedding": _unit_vector()})).embed(" \n")

"""Minimal bounded Bedrock check for the existing Titan embedding adapter."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import boto3

from src.external.titan_embeddings import BEDROCK_REGION, TitanTextEmbeddingsV2

FIXED_SYNTHETIC_INPUT = (
    "Synthetic Tally memory: the recorded fictional tariff rate was 250 USD per day."
)


@dataclass(frozen=True)
class BedrockCheckReceipt:
    service: str
    model: str
    fixed_input: bool
    normalized_vector_validated: bool
    passed: bool
    error_code: str | None = None


def run_check(factory: Callable[[], Any] | None = None) -> BedrockCheckReceipt:
    try:
        if factory is None:
            profile = os.environ.get("AWS_PROFILE")
            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            client = session.client("bedrock-runtime", region_name=BEDROCK_REGION)
            embedder = TitanTextEmbeddingsV2(client)
        else:
            embedder = factory()
        result = embedder.embed(FIXED_SYNTHETIC_INPUT)
        passed = len(result.values) == 1024
        return BedrockCheckReceipt(
            "Amazon Bedrock",
            "amazon.titan-embed-text-v2:0",
            True,
            passed,
            passed,
            None if passed else "embedding_validation_failed",
        )
    except Exception:
        return BedrockCheckReceipt(
            "Amazon Bedrock",
            "amazon.titan-embed-text-v2:0",
            True,
            False,
            False,
            "bedrock_unavailable",
        )


def main() -> int:
    receipt = run_check()
    print(json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")))
    return 0 if receipt.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

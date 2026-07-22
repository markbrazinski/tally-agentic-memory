from __future__ import annotations

from types import SimpleNamespace

from scripts.gate5_bedrock_check import FIXED_SYNTHETIC_INPUT, run_check


class Embedder:
    def __init__(self, *, error=None):
        self.error = error
        self.seen = []

    def embed(self, text):
        self.seen.append(text)
        if self.error:
            raise self.error
        return SimpleNamespace(values=(0.0,) * 1024)


def test_bounded_fixed_titan_check_passes_without_exposing_vector():
    embedder = Embedder()
    receipt = run_check(lambda: embedder)
    assert receipt.passed is True
    assert embedder.seen == [FIXED_SYNTHETIC_INPUT]
    assert "values" not in receipt.__dict__


def test_bedrock_failure_is_unavailable_not_a_canned_result():
    receipt = run_check(lambda: Embedder(error=RuntimeError("private detail")))
    assert receipt.passed is False
    assert receipt.error_code == "bedrock_unavailable"
    assert "private" not in str(receipt)

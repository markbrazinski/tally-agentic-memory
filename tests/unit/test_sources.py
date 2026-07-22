"""Unit tests for the capture source registry (capture/sources.py).

Pure data validation — no network, no mocking needed.
"""

from urllib.parse import urlparse

from capture.sources import SOURCES


def test_source_keys_are_unique():
    keys = [source.key for source in SOURCES]
    assert len(keys) == len(set(keys)), f"duplicate source keys found: {keys}"


def test_source_urls_parse_as_valid_urls():
    for source in SOURCES:
        parsed = urlparse(source.url)
        assert parsed.scheme in ("http", "https"), (
            f"source {source.key!r} has an invalid URL scheme: {source.url!r}"
        )
        assert parsed.netloc, f"source {source.key!r} has no network location: {source.url!r}"

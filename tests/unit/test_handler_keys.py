"""Unit tests for pure S3-key and manifest logic in capture/handler.py.

No network, no S3, no mocking — these exercise pure functions only, per
CLAUDE.md's "zero network calls in the test suite."
"""

from datetime import datetime, timezone

from capture.handler import (
    build_body_key,
    build_manifest,
    build_manifest_key,
    build_object_prefix,
    infer_extension,
)
from capture.sources import SOURCES


def test_object_prefix_is_date_correct_across_utc_boundary():
    # 23:59 UTC on one day and 00:01 UTC on the next must land in different
    # prefixes — the date string is the caller's responsibility (derived
    # from a server-set UTC clock), this just proves the prefix reflects
    # whatever date string it's given, correctly, on both sides of midnight.
    late_prefix = build_object_prefix("northstar-ocean-demo-tariff", "2026-07-04")
    early_prefix = build_object_prefix("northstar-ocean-demo-tariff", "2026-07-05")

    assert late_prefix == "raw/northstar-ocean-demo-tariff/2026-07-04"
    assert early_prefix == "raw/northstar-ocean-demo-tariff/2026-07-05"
    assert late_prefix != early_prefix


def test_body_and_manifest_keys_share_the_date_scoped_prefix():
    body_key = build_body_key("bluehaven-maritime-demo-tariff", "2026-07-04", "html")
    manifest_key = build_manifest_key("bluehaven-maritime-demo-tariff", "2026-07-04")

    assert body_key == "raw/bluehaven-maritime-demo-tariff/2026-07-04/body.html"
    assert manifest_key == "raw/bluehaven-maritime-demo-tariff/2026-07-04/manifest.json"


def test_infer_extension_known_and_unknown_content_types():
    assert infer_extension("text/html") == "html"
    assert infer_extension("text/html; charset=utf-8") == "html"
    assert infer_extension("application/pdf") == "pdf"
    assert infer_extension("application/octet-stream") == "bin"


def test_build_manifest_is_pure_and_reflects_fixed_capture_time():
    source = SOURCES[0]
    fixed_now = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)

    manifest = build_manifest(
        source=source,
        captured_at=fixed_now,
        status="ok",
        http_status=200,
        headers_subset={"content-type": "text/html"},
        sha256_hex="deadbeef",
        byte_count=42,
    )

    assert manifest["source_key"] == source.key
    assert manifest["url"] == source.url
    assert manifest["status"] == "ok"
    assert manifest["http_status"] == 200
    assert manifest["sha256"] == "deadbeef"
    assert manifest["byte_count"] == 42
    assert manifest["captured_at"] == "2026-07-04T08:00:00+00:00"

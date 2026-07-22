"""Integration tests for capture/verifier.py with a fake S3 client.

Per CLAUDE.md: "All external calls mocked in tests; zero network calls in
the test suite." Reuses the same FakeS3Client style established in
tests/integration/test_capture_handler.py (head_object/get_object with
real-shaped botocore ClientErrors) — this module only needs those two calls
since the verifier is read-only against S3 (it never writes there).

Covers bundle-r.md Session 2's verifier-specific test budget items:
    - verifier flags a missing source
    - verifier passes on complete day
    - one-source-down day records partial status (mixed ok/missing/failed)
"""

import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from capture.handler import build_manifest_key
from capture.sources import SOURCES
from capture.verifier import (
    INVOCATION_CHECK_STARTS,
    STATUS_FAILED,
    STATUS_MANUAL_ONLY,
    STATUS_MISSING,
    STATUS_OK,
    build_summary,
    lambda_handler,
    verify_sources,
)

TODAY = "2026-07-05"
TEST_BUCKET = "tally-demo-recordings-test"
CHECKED_AT = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)

# On/after INVOCATION_CHECK_STARTS, so the new manual_only check applies.
POST_FIX_DAY = "2026-07-06"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeStreamingBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    """Minimal in-memory S3 stand-in covering only what the verifier needs.

    head_object raises a real-shaped 404 ClientError for a missing key
    (matching botocore); get_object does the same, and otherwise returns a
    dict with a Body exposing .read() like the real StreamingBody.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": _FakeStreamingBody(self.objects[Key])}


class FakeCloudWatchClient:
    """Records put_metric_data calls for assertion; never talks to AWS."""

    def __init__(self):
        self.put_metric_data_calls: list[dict] = []

    def put_metric_data(self, **kwargs):
        self.put_metric_data_calls.append(kwargs)


def _seed_manifest(
    s3_client: FakeS3Client, source_key: str, date: str, status: str, invocation: str | None = None
) -> None:
    manifest_key = build_manifest_key(source_key, date)
    body: dict = {"status": status}
    if invocation is not None:
        body["invocation"] = invocation
    s3_client.objects[manifest_key] = json.dumps(body).encode("utf-8")


def test_verifier_passes_on_complete_day_all_sources_ok():
    """All sources have today's manifest with status 'ok' -> all_ok=True."""
    s3_client = FakeS3Client()
    for source in SOURCES:
        _seed_manifest(s3_client, source.key, TODAY, STATUS_OK)

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)

    assert all_ok is True
    assert all(r.manifest_status == STATUS_OK for r in results)

    summary = build_summary(results, all_ok, checked_at=CHECKED_AT)
    assert summary["all_ok"] is True
    assert summary["missing_or_failed"] == []


def test_verifier_flags_a_missing_source():
    """One source has no manifest at all today -> reported in missing_or_failed."""
    s3_client = FakeS3Client()
    missing_source = SOURCES[0]
    other_sources = SOURCES[1:]
    for source in other_sources:
        _seed_manifest(s3_client, source.key, TODAY, STATUS_OK)
    # Deliberately do not seed a manifest for missing_source.

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)

    assert all_ok is False
    by_key = {r.source_key: r.manifest_status for r in results}
    assert by_key[missing_source.key] == STATUS_MISSING
    for source in other_sources:
        assert by_key[source.key] == STATUS_OK


def test_verifier_flags_a_failed_manifest_status_same_as_missing():
    """A manifest exists but status='failed' -> also reported in missing_or_failed.

    A failed capture is just as much a problem as a missing one (task
    spec): status != "ok" must fail verification even though the manifest
    object itself landed in S3.
    """
    s3_client = FakeS3Client()
    failed_source = SOURCES[0]
    other_sources = SOURCES[1:]
    _seed_manifest(s3_client, failed_source.key, TODAY, STATUS_FAILED)
    for source in other_sources:
        _seed_manifest(s3_client, source.key, TODAY, STATUS_OK)

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)

    assert all_ok is False
    by_key = {r.source_key: r.manifest_status for r in results}
    assert by_key[failed_source.key] == STATUS_FAILED

    summary = build_summary(results, all_ok, checked_at=CHECKED_AT)
    assert failed_source.key in summary["missing_or_failed"]


def test_one_source_down_day_records_partial_status():
    """Mixed: 2 of 3 sources ok, 1 missing -> partial status, exact source identified.

    Covers bundle-r.md's explicit test-budget phrase "one-source-down day
    records partial status": the summary must distinguish "some captured,
    one missing" from both "all captured" and "none captured", and must
    name exactly which source is down.
    """
    assert len(SOURCES) == 3, "test assumes the current 3-source registry"
    ok_sources = SOURCES[:2]
    down_source = SOURCES[2]

    s3_client = FakeS3Client()
    for source in ok_sources:
        _seed_manifest(s3_client, source.key, TODAY, STATUS_OK)
    # down_source gets no manifest at all today.

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)
    summary = build_summary(results, all_ok, checked_at=CHECKED_AT)

    assert summary["all_ok"] is False
    assert summary["missing_or_failed"] == [down_source.key]
    # And the two ok sources are correctly NOT flagged.
    status_by_key = {r["source_key"]: r["manifest_status"] for r in summary["results"]}
    for source in ok_sources:
        assert status_by_key[source.key] == STATUS_OK
    assert status_by_key[down_source.key] == STATUS_MISSING


def test_malformed_manifest_json_is_treated_as_failed_not_missing():
    """A manifest object exists but is unparseable JSON -> STATUS_FAILED.

    Distinct from STATUS_MISSING: the capture Lambda (and EventBridge)
    clearly ran and wrote something, so "missing" would misname the
    failure mode; but there's no trustworthy status here either, so it
    must not read as "ok".
    """
    s3_client = FakeS3Client()
    source = SOURCES[0]
    manifest_key = build_manifest_key(source.key, TODAY)
    s3_client.objects[manifest_key] = b"{not valid json"

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)

    by_key = {r.source_key: r.manifest_status for r in results}
    assert by_key[source.key] == STATUS_FAILED
    assert all_ok is False


def test_pre_fix_manual_day_is_not_flagged_even_without_invocation_field():
    """A manual (or field-less) manifest before INVOCATION_CHECK_STARTS is still STATUS_OK.

    July 4-5, 2026 manifests are legitimately manual - no schedule had
    fired yet, or predate the "invocation" field entirely (see
    scripts/annotate_manual_invocations.py). Bundle R addendum: disclose,
    never alarm on history. TODAY here is 2026-07-05, before
    INVOCATION_CHECK_STARTS, so a manual invocation must NOT flip this to
    manual_only.
    """
    assert TODAY < INVOCATION_CHECK_STARTS
    s3_client = FakeS3Client()
    for source in SOURCES:
        _seed_manifest(s3_client, source.key, TODAY, STATUS_OK, invocation="manual")

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=TODAY)

    assert all_ok is True
    assert all(r.manifest_status == STATUS_OK for r in results)


def test_post_fix_manual_only_day_is_flagged_not_silently_ok():
    """A status=ok manifest first-written manually on/after the fix date -> manual_only.

    This is the exact failure mode from the Bundle R addendum incident:
    the schedule fires (real, unattended) but finds the day already
    claimed by an earlier manual invoke, so it no-ops and the day never
    gets genuine unattended evidence. A soak day must not silently read
    as clean just because status=="ok".
    """
    s3_client = FakeS3Client()
    for source in SOURCES:
        _seed_manifest(s3_client, source.key, POST_FIX_DAY, STATUS_OK, invocation="manual")

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=POST_FIX_DAY)

    assert all_ok is False
    assert all(r.manifest_status == STATUS_MANUAL_ONLY for r in results)

    summary = build_summary(results, all_ok, checked_at=CHECKED_AT)
    for source in SOURCES:
        assert source.key in summary["missing_or_failed"]


def test_post_fix_scheduled_day_is_ok():
    """A status=ok manifest first-written by the schedule, post-fix -> STATUS_OK.

    The clean case: the 08:00 UTC schedule actually won the write race,
    same as an ordinary healthy day.
    """
    s3_client = FakeS3Client()
    for source in SOURCES:
        _seed_manifest(s3_client, source.key, POST_FIX_DAY, STATUS_OK, invocation="scheduled")

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=POST_FIX_DAY)

    assert all_ok is True
    assert all(r.manifest_status == STATUS_OK for r in results)


def test_manual_reinvoke_after_clean_scheduled_capture_stays_ok():
    """A LATER manual re-invoke must not un-flag a day the schedule already won.

    Precedence rule (Bundle R addendum #2, explicit design note): idempotency
    means whichever invocation writes the manifest FIRST owns its
    "invocation" field for that day - capture_one_source's manifest_exists
    check makes any later invoke (manual or scheduled) hit the retry no-op
    path and never rewrite the manifest. So a manual re-invoke of an
    already-scheduled-and-captured day is fine: the manifest on S3 still
    says invocation="scheduled" because that's genuinely who wrote it.
    This test seeds exactly that already-resolved state directly (no
    no-op path to simulate here - the manifest content IS the source of
    truth the verifier reads).
    """
    s3_client = FakeS3Client()
    for source in SOURCES:
        _seed_manifest(s3_client, source.key, POST_FIX_DAY, STATUS_OK, invocation="scheduled")

    results, all_ok = verify_sources(s3_client, TEST_BUCKET, today=POST_FIX_DAY)

    assert all_ok is True
    assert all(r.manifest_status == STATUS_OK for r in results)


def test_post_fix_failed_manifest_is_failed_not_manual_only():
    """A status=failed manifest stays STATUS_FAILED regardless of invocation.

    The manual_only check only applies to status=="ok" manifests - a
    failed capture is already flagged for the right reason and must not
    be relabeled.
    """
    s3_client = FakeS3Client()
    source = SOURCES[0]
    _seed_manifest(s3_client, source.key, POST_FIX_DAY, STATUS_FAILED, invocation="manual")

    results, _all_ok = verify_sources(s3_client, TEST_BUCKET, today=POST_FIX_DAY)

    by_key = {r.source_key: r.manifest_status for r in results}
    assert by_key[source.key] == STATUS_FAILED


def test_manifest_exists_reraises_non_404_client_errors_via_manifest_exists():
    """A transient/non-404 S3 error during the existence check must propagate.

    Same narrow-ClientError discipline as capture.handler.manifest_exists
    (which this module reuses directly) — a throttling or permissions
    error must never be silently reported as "missing".
    """

    class ThrottlingS3Client:
        def head_object(self, Bucket, Key):
            raise _client_error("SlowDown", "HeadObject")

    import pytest

    with pytest.raises(ClientError):
        verify_sources(ThrottlingS3Client(), TEST_BUCKET, today=TODAY)


def test_lambda_handler_emits_cloudwatch_metric_zero_when_all_ok(monkeypatch):
    """lambda_handler puts a ExampleRecoveryGap=0 datapoint on a complete day."""
    fake_s3 = FakeS3Client()
    fake_cloudwatch = FakeCloudWatchClient()
    for source in SOURCES:
        _seed_manifest(fake_s3, source.key, "2026-07-05", STATUS_OK)

    class FixedDatetime:
        @staticmethod
        def now(tz):
            import datetime

            return datetime.datetime(2026, 7, 5, 10, 0, 0, tzinfo=tz)

    class FakeBoto3Module:
        @staticmethod
        def client(service_name):
            if service_name == "s3":
                return fake_s3
            if service_name == "cloudwatch":
                return fake_cloudwatch
            raise AssertionError(f"unexpected service {service_name}")

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3Module())
    monkeypatch.setattr("capture.verifier.datetime", FixedDatetime)
    monkeypatch.setenv("TALLY_BUCKET", TEST_BUCKET)

    summary = lambda_handler({}, None)

    assert summary["all_ok"] is True
    assert summary["missing_or_failed"] == []
    assert len(fake_cloudwatch.put_metric_data_calls) == 1
    call = fake_cloudwatch.put_metric_data_calls[0]
    assert call["Namespace"] == "ExampleTally/Capture"
    assert call["MetricData"][0]["MetricName"] == "ExampleRecoveryGap"
    assert call["MetricData"][0]["Value"] == 0.0


def test_lambda_handler_emits_cloudwatch_metric_one_when_a_source_is_missing(monkeypatch):
    """lambda_handler puts a ExampleRecoveryGap=1 datapoint when a source is missing."""
    fake_s3 = FakeS3Client()
    fake_cloudwatch = FakeCloudWatchClient()
    # Only seed the first source; leave the rest missing.
    _seed_manifest(fake_s3, SOURCES[0].key, "2026-07-05", STATUS_OK)

    class FixedDatetime:
        @staticmethod
        def now(tz):
            import datetime

            return datetime.datetime(2026, 7, 5, 10, 0, 0, tzinfo=tz)

    class FakeBoto3Module:
        @staticmethod
        def client(service_name):
            if service_name == "s3":
                return fake_s3
            if service_name == "cloudwatch":
                return fake_cloudwatch
            raise AssertionError(f"unexpected service {service_name}")

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3Module())
    monkeypatch.setattr("capture.verifier.datetime", FixedDatetime)
    monkeypatch.setenv("TALLY_BUCKET", TEST_BUCKET)

    summary = lambda_handler({}, None)

    assert summary["all_ok"] is False
    assert len(summary["missing_or_failed"]) == len(SOURCES) - 1
    call = fake_cloudwatch.put_metric_data_calls[0]
    assert call["MetricData"][0]["Value"] == 1.0

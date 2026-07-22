"""Unit tests for the pure logic in capture/tick.py (the `make tick` CLI).

Only the pure functions (date computation, table formatting, the s3_key ->
source_key join-key recovery, and fetch_db_recording_statuses against a
fake cursor) are tested here — no network, no S3, no real DB, per
CLAUDE.md's "zero network calls in the test suite." The boto3/psycopg
fetch/loop glue in run()/main()/try_fetch_db_statuses is thin I/O and is
exercised via the real-cluster smoke test instead, not unit tests.
"""

import json
from datetime import date, datetime, timezone

from capture.tick import (
    NO_MANIFEST,
    SOAK_CLEAN,
    SOAK_NOT_CLEAN,
    count_invocations_by_mode,
    fetch_db_recording_statuses,
    format_soak_summary,
    format_tick_table,
    format_tick_table_with_db,
    last_n_utc_dates,
    soak_day_verdict,
    source_key_from_s3_key,
)


def test_last_n_utc_dates_is_newest_first_and_utc_bound():
    fixed_now = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)

    dates = last_n_utc_dates(3, today=fixed_now)

    assert dates == ["2026-07-04", "2026-07-03", "2026-07-02"]


def test_last_n_utc_dates_crosses_month_boundary_correctly():
    fixed_now = datetime(2026, 8, 1, 0, 30, 0, tzinfo=timezone.utc)

    dates = last_n_utc_dates(3, today=fixed_now)

    assert dates == ["2026-08-01", "2026-07-31", "2026-07-30"]


def test_last_n_utc_dates_respects_n():
    fixed_now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

    assert last_n_utc_dates(1, today=fixed_now) == ["2026-07-04"]
    assert len(last_n_utc_dates(5, today=fixed_now)) == 5


def test_format_tick_table_includes_header_and_all_rows():
    rows = [
        ("northstar-ocean-demo-tariff", "2026-07-04", "ok"),
        ("northstar-ocean-demo-tariff", "2026-07-03", "ok"),
        ("bluehaven-maritime-demo-tariff", "2026-07-04", NO_MANIFEST),
    ]

    table = format_tick_table(rows)
    lines = table.splitlines()

    assert lines[0].startswith("source_key")
    assert "date" in lines[0] and "status" in lines[0]
    # separator line between header and body
    assert set(lines[1].replace("|", "").replace("+", "").strip()) <= {"-"}
    assert len(lines) == 2 + len(rows)
    for source_key, dt, status in rows:
        assert any(source_key in line and dt in line and status in line for line in lines[2:])


def test_format_tick_table_columns_align_to_widest_cell():
    rows = [
        ("a", "2026-07-04", "ok"),
        ("a-much-longer-source-key", "2026-07-03", NO_MANIFEST),
    ]

    table = format_tick_table(rows)
    lines = table.splitlines()

    # Every row line (including header/separator) should be the same length
    # once padded — proves column widths are computed from the widest cell.
    body_lines = lines[2:]
    assert len({len(line) for line in body_lines}) == 1


# ---------------------------------------------------------------------------
# format_tick_table_with_db: the new 4-column S3+DB view (Bundle R Session 3)
# ---------------------------------------------------------------------------


def test_format_tick_table_with_db_includes_header_and_all_rows():
    rows = [
        ("northstar-ocean-demo-tariff", "2026-07-04", "ok", "COMMITTED"),
        ("northstar-ocean-demo-tariff", "2026-07-03", "ok", "no db row"),
        ("bluehaven-maritime-demo-tariff", "2026-07-04", NO_MANIFEST, "db unavailable"),
    ]

    table = format_tick_table_with_db(rows)
    lines = table.splitlines()

    assert lines[0].startswith("source_key")
    assert "date" in lines[0] and "s3_status" in lines[0] and "db_status" in lines[0]
    assert set(lines[1].replace("|", "").replace("+", "").strip()) <= {"-"}
    assert len(lines) == 2 + len(rows)
    for source_key, dt, s3_status, db_status in rows:
        assert any(
            source_key in line and dt in line and s3_status in line and db_status in line
            for line in lines[2:]
        )


def test_format_tick_table_with_db_columns_align_to_widest_cell():
    rows = [
        ("a", "2026-07-04", "ok", "COMMITTED"),
        ("a-much-longer-source-key", "2026-07-03", NO_MANIFEST, "FAILED"),
    ]

    table = format_tick_table_with_db(rows)
    lines = table.splitlines()

    body_lines = lines[2:]
    assert len({len(line) for line in body_lines}) == 1


def test_format_tick_table_unchanged_by_new_4column_function():
    """format_tick_table's own 3-column signature/behavior is untouched -
    existing callers/tests keep working with zero changes."""
    rows = [("northstar-ocean-demo-tariff", "2026-07-04", "ok")]
    table = format_tick_table(rows)
    assert "s3_status" not in table
    assert "db_status" not in table
    header_cells = [cell.strip() for cell in table.splitlines()[0].split(" | ")]
    assert header_cells == ["source_key", "date", "status"]


# ---------------------------------------------------------------------------
# source_key_from_s3_key: recovering the join key across every recording status
# ---------------------------------------------------------------------------


def test_source_key_from_s3_key_committed_shape_body_key():
    s3_key = "raw/northstar-ocean-demo-tariff/2026-07-04/body.html"
    assert source_key_from_s3_key(s3_key) == "northstar-ocean-demo-tariff"


def test_source_key_from_s3_key_failed_shape_manifest_key():
    """FAILED/SKIPPED recordings rows carry the manifest_key itself (no
    body_key exists), but source_key is still the second path segment."""
    s3_key = "raw/bluehaven-maritime-demo-tariff/2026-07-04/manifest.json"
    assert source_key_from_s3_key(s3_key) == "bluehaven-maritime-demo-tariff"


def test_source_key_from_s3_key_none_when_missing():
    assert source_key_from_s3_key(None) is None
    assert source_key_from_s3_key("") is None


def test_source_key_from_s3_key_none_when_malformed():
    assert source_key_from_s3_key("not-a-valid-key") is None
    assert source_key_from_s3_key("unexpected/prefix/thing") is None


# ---------------------------------------------------------------------------
# count_invocations_by_mode: pure tally over raw CloudWatch Logs messages
# (Bundle R addendum #2 - the direct fix for "every signal measured intent,
# none measured effect")
# ---------------------------------------------------------------------------


def test_count_invocations_by_mode_tallies_scheduled_and_manual():
    messages = [
        json.dumps({"event": "capture_ok", "invocation": "scheduled"}),
        json.dumps({"event": "capture_ok", "invocation": "scheduled"}),
        json.dumps({"event": "capture_skipped", "invocation": "manual"}),
    ]

    counts = count_invocations_by_mode(messages)

    assert counts == {"scheduled": 2, "manual": 1}


def test_count_invocations_by_mode_ignores_non_json_lines():
    """Raw Lambda platform lines (INIT_START/START/END/REPORT) are plain
    text, not JSON - expected noise in the stream, not an error."""
    messages = [
        "START RequestId: abc Version: $LATEST",
        json.dumps({"event": "capture_ok", "invocation": "scheduled"}),
        "REPORT RequestId: abc Duration: 123 ms",
    ]

    counts = count_invocations_by_mode(messages)

    assert counts == {"scheduled": 1}


def test_count_invocations_by_mode_handles_real_lambda_log_line_prefix():
    """The real CloudWatch Logs message is never bare JSON - Python's
    logging module prepends "[LEVEL]\\t<timestamp>\\t<request-id>\\t"
    ahead of the json.dumps(...) payload (see _log_event in
    capture/handler.py and capture/verifier.py). A naive json.loads on
    the whole message always fails, even for a well-formed event - this
    is the exact bug found live on 2026-07-06: make tick's soak section
    showed "(none)" for a day that had genuinely fired, because every
    real log line was silently skipped, not because there was nothing to
    count.
    """
    payload = json.dumps(
        {"event": "capture_ok", "source_key": "northstar-ocean-demo-tariff", "invocation": "scheduled"}
    )
    messages = [
        f'[INFO]\t2026-07-06T08:00:26.276Z\t956a4b60-8039-4e24-91f6-e6b7ac4e7730\t{payload}\n'
    ]

    counts = count_invocations_by_mode(messages)

    assert counts == {"scheduled": 1}


def test_count_invocations_by_mode_ignores_json_lines_without_invocation_field():
    """capture_run_summary doesn't carry "invocation" itself - must not
    crash or miscount, just skip."""
    messages = [
        json.dumps({"event": "capture_run_summary", "results": []}),
        json.dumps({"event": "capture_ok", "invocation": "manual"}),
    ]

    counts = count_invocations_by_mode(messages)

    assert counts == {"manual": 1}


def test_count_invocations_by_mode_empty_input_returns_empty_dict():
    assert count_invocations_by_mode([]) == {}


# ---------------------------------------------------------------------------
# soak_day_verdict: delegates to capture.verifier.verify_sources so make tick
# and the verifier Lambda can never silently disagree
# ---------------------------------------------------------------------------


class _FakeS3ClientForSoak:
    """Minimal S3 stand-in matching the shape capture.verifier needs
    (head_object + get_object), same convention as
    tests/integration/test_verifier.py's own FakeS3Client."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, Bucket: str, Key: str):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket: str, Key: str):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "NoSuchKey"}}, "GetObject")

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[Key])}


def _seed_soak_manifest(s3_client, source_key, date_str, status, invocation=None):
    from capture.handler import build_manifest_key

    body = {"status": status}
    if invocation is not None:
        body["invocation"] = invocation
    s3_client.objects[build_manifest_key(source_key, date_str)] = json.dumps(body).encode()


def test_soak_day_verdict_clean_when_all_sources_scheduled_ok():
    from capture.sources import SOURCES

    s3_client = _FakeS3ClientForSoak()
    for source in SOURCES:
        _seed_soak_manifest(s3_client, source.key, "2026-07-06", "ok", invocation="scheduled")

    assert soak_day_verdict(s3_client, "tally-demo-recordings-test", date="2026-07-06") == SOAK_CLEAN


def test_soak_day_verdict_not_clean_when_manual_only_post_fix():
    """Delegates the exact manual_only precedence rule to verify_sources -
    a manual-first-write day on/after INVOCATION_CHECK_STARTS is not clean."""
    from capture.sources import SOURCES

    s3_client = _FakeS3ClientForSoak()
    for source in SOURCES:
        _seed_soak_manifest(s3_client, source.key, "2026-07-06", "ok", invocation="manual")

    assert soak_day_verdict(s3_client, "tally-demo-recordings-test", date="2026-07-06") == SOAK_NOT_CLEAN


def test_soak_day_verdict_not_clean_when_a_source_is_missing():
    from capture.sources import SOURCES

    s3_client = _FakeS3ClientForSoak()
    for source in SOURCES[1:]:
        _seed_soak_manifest(s3_client, source.key, "2026-07-06", "ok", invocation="scheduled")
    # SOURCES[0] deliberately left with no manifest.

    assert soak_day_verdict(s3_client, "tally-demo-recordings-test", date="2026-07-06") == SOAK_NOT_CLEAN


def test_soak_day_verdict_pre_fix_manual_day_still_clean():
    """A legitimately-manual day before INVOCATION_CHECK_STARTS is clean -
    disclosure, not a false alarm on known-manual history."""
    from capture.sources import SOURCES

    s3_client = _FakeS3ClientForSoak()
    for source in SOURCES:
        _seed_soak_manifest(s3_client, source.key, "2026-07-05", "ok", invocation="manual")

    assert soak_day_verdict(s3_client, "tally-demo-recordings-test", date="2026-07-05") == SOAK_CLEAN


# ---------------------------------------------------------------------------
# format_soak_summary: pure formatting of the soak-day section
# ---------------------------------------------------------------------------


def test_format_soak_summary_includes_each_date_verdict():
    soak_rows = [("2026-07-06", SOAK_CLEAN), ("2026-07-05", SOAK_NOT_CLEAN)]

    output = format_soak_summary(soak_rows, invocation_counts=None)

    assert "2026-07-06" in output and SOAK_CLEAN in output
    assert "2026-07-05" in output and SOAK_NOT_CLEAN in output


def test_format_soak_summary_shows_logs_unavailable_when_none():
    output = format_soak_summary([("2026-07-06", SOAK_CLEAN)], invocation_counts=None)

    assert "logs unavailable" in output


def test_format_soak_summary_annotates_pre_fix_dates_as_not_counting():
    """A pre-INVOCATION_CHECK_STARTS date must never read as a plain
    soak-streak contributor, even when its verdict is SOAK_CLEAN - clean
    there means "no data-integrity problem", not "counts toward the
    streak"."""
    soak_rows = [("2026-07-05", SOAK_CLEAN)]

    output = format_soak_summary(soak_rows, invocation_counts=None)

    assert "2026-07-05" in output
    assert "does not count toward soak streak" in output


def test_format_soak_summary_does_not_annotate_post_fix_dates():
    soak_rows = [("2026-07-06", SOAK_CLEAN)]

    output = format_soak_summary(soak_rows, invocation_counts=None)

    assert "does not count toward soak streak" not in output


def test_format_soak_summary_shows_invocation_counts_per_log_group():
    counts = {
        "/aws/lambda/example-archive-worker": {"scheduled": 3, "manual": 0},
        "/aws/lambda/example-audit-worker": {"scheduled": 1},
    }

    output = format_soak_summary([("2026-07-06", SOAK_CLEAN)], invocation_counts=counts)

    assert "/aws/lambda/example-archive-worker" in output
    assert "scheduled=3" in output
    assert "manual=0" in output
    assert "/aws/lambda/example-audit-worker" in output
    assert "scheduled=1" in output


# ---------------------------------------------------------------------------
# fetch_db_recording_statuses: query shape + lookup building, via a fake cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    def cursor(self):
        return self._cursor


def test_fetch_db_recording_statuses_builds_lookup_by_source_key_and_date():
    rows = [
        (date(2026, 7, 4), "COMMITTED", "raw/northstar-ocean-demo-tariff/2026-07-04/body.html"),
        (date(2026, 7, 3), "FAILED", "raw/bluehaven-maritime-demo-tariff/2026-07-03/manifest.json"),
    ]
    conn = _FakeConn(rows)

    lookup = fetch_db_recording_statuses(
        conn, tenant_id="tenant-1", dates=["2026-07-04", "2026-07-03", "2026-07-02"]
    )

    assert lookup[("northstar-ocean-demo-tariff", "2026-07-04")] == "COMMITTED"
    assert lookup[("bluehaven-maritime-demo-tariff", "2026-07-03")] == "FAILED"
    assert ("harborview-terminal-demo-tariff", "2026-07-02") not in lookup


def test_fetch_db_recording_statuses_tenant_scoped_and_date_bounded():
    conn = _FakeConn([])

    fetch_db_recording_statuses(conn, tenant_id="tenant-xyz", dates=["2026-07-04"])

    sql, params = conn._cursor.executed
    assert "tenant_id" in sql
    assert params["tenant_id"] == "tenant-xyz"
    assert params["run_dates"] == [date(2026, 7, 4)]


def test_fetch_db_recording_statuses_skips_rows_with_unrecoverable_s3_key():
    """A malformed/legacy s3_key shouldn't raise or corrupt the lookup -
    it's just silently unmapped (degrade gracefully, same spirit as the
    rest of this tool)."""
    rows = [(date(2026, 7, 4), "COMMITTED", None)]
    conn = _FakeConn(rows)

    lookup = fetch_db_recording_statuses(conn, tenant_id="tenant-1", dates=["2026-07-04"])

    assert lookup == {}

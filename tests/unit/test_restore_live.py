"""Unit tests for restore_live.py — Bundle R Session 3's replay entry point.

Per CLAUDE.md: "All external calls mocked in tests; zero network calls in
the test suite." Reuses tests/unit/test_commit.py's FakeConnection (a
psycopg-3-shaped in-memory stand-in for the tariff_snapshots/recordings
tables) and FakeManifestS3Client (an in-memory S3 stand-in keyed by
manifest_key), rather than rebuilding either — same fakes, same ON
CONFLICT DO NOTHING semantics that make idempotency provable here without
a real cluster.

Covers docs/bundle-r.md Session 3's test budget item "replay idempotency
(run twice -> same rows)" at the restore_live.py script level (commit.py's
own unit tests already cover it at the single-source/single-day level;
this file proves it holds across the full multi-day replay_range walk),
plus the task's own explicit test list:
    - replay walks the correct date range (July 4 through an injected
      fixed "today", clock-independent)
    - a date with no manifests for any source produces no commits, no error
    - two full replay runs over the same range produce identical final
      row-count state (idempotency)
"""

from __future__ import annotations

from datetime import date

from capture.handler import build_manifest_key
from capture.sources import SOURCES
from recording.commit import STATUS_COMMITTED, STATUS_FAILED, STATUS_SKIPPED
from restore_live import FIRST_CAPTURE_DATE, _parse_args, daterange, replay_range
from tests.unit.test_commit import (
    CARRIER_ID_BY_SCAC,
    TENANT_ID,
    FakeConnection,
    FakeManifestS3Client,
    _failed_manifest,
    _ok_manifest,
)

NORTHSTAR_SOURCE = next(s for s in SOURCES if s.key == "northstar-ocean-demo-tariff")
BLUEHAVEN_SOURCE = next(s for s in SOURCES if s.key == "bluehaven-maritime-demo-tariff")
HVTM_SOURCE = next(s for s in SOURCES if s.key == "harborview-terminal-demo-tariff")

TEST_BUCKET = "tally-demo-recordings-test"


# ---------------------------------------------------------------------------
# daterange: pure date-walk helper
# ---------------------------------------------------------------------------


def test_daterange_walks_first_capture_date_through_injected_today_inclusive():
    """The real default range: July 4, 2026 (FIRST_CAPTURE_DATE) through an
    injected fixed "today" — clock-independent, and inclusive of both ends.
    """
    fixed_today = date(2026, 7, 6)

    dates = daterange(FIRST_CAPTURE_DATE, fixed_today)

    assert dates == [date(2026, 7, 4), date(2026, 7, 5), date(2026, 7, 6)]


def test_daterange_single_day_when_start_equals_end():
    assert daterange(date(2026, 7, 4), date(2026, 7, 4)) == [date(2026, 7, 4)]


def test_daterange_empty_when_start_after_end():
    """Defensive: an inverted range (e.g. a bad --end-date override) must
    not crash or produce a nonsensical walk — just nothing to do."""
    assert daterange(date(2026, 7, 6), date(2026, 7, 4)) == []


def test_parse_args_accepts_secret_safe_isolated_replay_dsn_env_name():
    args = _parse_args(["--dsn-env-var", "TALLY_REPLAY_CRDB_DSN"])

    assert args.dsn_env_var == "TALLY_REPLAY_CRDB_DSN"


# ---------------------------------------------------------------------------
# replay_range: core replay loop against fakes
# ---------------------------------------------------------------------------


def _seed_full_day(s3_client: FakeManifestS3Client, run_date: date) -> None:
    """Seed all three sources' manifests for one day: 2 ok, 1 failed."""
    date_str = run_date.isoformat()
    s3_client._manifests_by_key[build_manifest_key(NORTHSTAR_SOURCE.key, date_str)] = _ok_manifest(
        NORTHSTAR_SOURCE, captured_at=f"{date_str}T08:00:14+00:00"
    )
    s3_client._manifests_by_key[build_manifest_key(BLUEHAVEN_SOURCE.key, date_str)] = _ok_manifest(
        BLUEHAVEN_SOURCE, captured_at=f"{date_str}T08:00:14+00:00"
    )
    s3_client._manifests_by_key[build_manifest_key(HVTM_SOURCE.key, date_str)] = _failed_manifest(
        HVTM_SOURCE, captured_at=f"{date_str}T08:00:14+00:00"
    )


def test_replay_range_walks_every_day_in_the_range():
    """A 3-day range with a fully-captured day each day produces 3 sources
    worth of results per day = 9 total, in date order."""
    conn = FakeConnection()
    s3_client = FakeManifestS3Client({})
    start_date = date(2026, 7, 4)
    end_date = date(2026, 7, 6)
    for offset in range(3):
        _seed_full_day(s3_client, date(2026, 7, 4 + offset))

    results = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=start_date,
        end_date=end_date,
    )

    assert len(results) == 9  # 3 sources * 3 days
    by_status = [r.recording_status for r in results]
    assert by_status.count(STATUS_COMMITTED) == 6  # 2 ok sources * 3 days
    assert by_status.count(STATUS_FAILED) == 3  # 1 failed source * 3 days
    assert by_status.count(STATUS_SKIPPED) == 0

    # DB-side: 3 days * (2 tariff_snapshots + 3 recordings rows) each.
    assert len(conn.tariff_snapshots) == 6
    assert len(conn.recordings) == 9


def test_replay_range_day_with_no_manifests_for_any_source_produces_no_commits():
    """A date with zero manifests anywhere (e.g. a future date, or a real
    gap) must not error and must produce no CommitResults for that day —
    falls out of commit_day's own contract, no extra handling needed here.
    """
    conn = FakeConnection()
    s3_client = FakeManifestS3Client({})
    # Seed July 4 fully, but leave July 5 and July 6 completely empty.
    _seed_full_day(s3_client, date(2026, 7, 4))

    results = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=date(2026, 7, 4),
        end_date=date(2026, 7, 6),
    )

    # Only July 4's 3 sources produced results; July 5/6 contributed nothing.
    assert len(results) == 3
    assert len(conn.recordings) == 3


def test_replay_range_empty_s3_produces_empty_results_no_error():
    """Nothing captured anywhere yet: replay_range must return [] cleanly,
    not raise — the honest state for a brand-new cluster before capture
    has run even once."""
    conn = FakeConnection()
    s3_client = FakeManifestS3Client({})

    results = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=date(2026, 7, 4),
        end_date=date(2026, 7, 4),
    )

    assert results == []
    assert conn.recordings == []
    assert conn.tariff_snapshots == []


# ---------------------------------------------------------------------------
# Idempotency: run the full replay twice, same DB/S3 fakes, same row counts.
# ---------------------------------------------------------------------------


def test_replay_range_run_twice_produces_identical_final_row_counts():
    """The explicit test budget item: "replay idempotency (run twice ->
    same rows)", exercised at the full restore_live.py replay_range level
    (not just commit_source_day's own single-source unit test) across a
    multi-day, multi-source, mixed-status range.
    """
    conn = FakeConnection()
    s3_client = FakeManifestS3Client({})
    start_date = date(2026, 7, 4)
    end_date = date(2026, 7, 6)
    for offset in range(3):
        _seed_full_day(s3_client, date(2026, 7, 4 + offset))

    first_run = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=start_date,
        end_date=end_date,
    )
    snapshots_after_first = len(conn.tariff_snapshots)
    recordings_after_first = len(conn.recordings)

    second_run = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=start_date,
        end_date=end_date,
    )

    # Same source-day result count both times...
    assert len(first_run) == len(second_run) == 9
    # ...but the second run's rows_written must be 0 everywhere it landed
    # a snapshot the first time (ON CONFLICT DO NOTHING - ok is committed
    # again with rows_written=0, since nothing new was inserted).
    first_ok_count = sum(1 for r in first_run if r.recording_status == STATUS_COMMITTED)
    second_ok_count = sum(1 for r in second_run if r.recording_status == STATUS_COMMITTED)
    assert first_ok_count == second_ok_count == 6
    assert sum(r.rows_written for r in second_run) == 0

    # The DB's actual row counts must not have grown at all on the re-run.
    assert len(conn.tariff_snapshots) == snapshots_after_first
    assert len(conn.recordings) == recordings_after_first
    assert len(conn.tariff_snapshots) == 6
    assert len(conn.recordings) == 9


def test_replay_range_run_twice_over_a_gappy_range_still_idempotent():
    """Idempotency must also hold when some days in the range have no
    manifests at all (a realistic replay-from-birth scenario: most of the
    range is empty except the days that have actually happened)."""
    conn = FakeConnection()
    s3_client = FakeManifestS3Client({})
    _seed_full_day(s3_client, date(2026, 7, 4))
    # July 5 and July 6 intentionally left empty (haven't happened yet).

    first_run = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=date(2026, 7, 4),
        end_date=date(2026, 7, 6),
    )
    second_run = replay_range(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket=TEST_BUCKET,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
        start_date=date(2026, 7, 4),
        end_date=date(2026, 7, 6),
    )

    assert len(first_run) == len(second_run) == 3
    assert len(conn.recordings) == 3
    assert len(conn.tariff_snapshots) == 2

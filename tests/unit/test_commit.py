"""Unit tests for recording/commit.py's pure functions and DB commit logic.

Per CLAUDE.md: "All external calls mocked in tests; zero network calls in
the test suite" — this extends to the DAL layer here too. Rather than
hitting a real CockroachDB cluster, these tests use a lightweight fake
connection/cursor pair that implements just enough of psycopg 3's surface
(`.transaction()` context manager, `.cursor()` context manager, `.execute`,
`.fetchone`) for src.external.db.run_with_retry and commit.py's own SQL
calls to run against, matching the spirit of
tests/integration/test_capture_handler.py's FakeS3Client for SQL instead
of S3.

Judgment call (see session report for full reasoning): fully mocked, not
against a real test DB. CLAUDE.md's "zero network calls in the test suite"
rule is written without a DAL carve-out, and a real-DB integration test
would need a live reachable CockroachDB cluster wired into CI/local runs
for a hackathon project — that's real infrastructure coupling this bundle
doesn't need in order to prove commit.py's OWN logic (row-shape derivation,
status mapping, tenant scoping, conflict handling wiring). The fake
connection below exercises the exact SQL text and parameter binding
commit.py sends, which is what a reviewer or judge can inspect directly
(CLAUDE.md: "the UI prints the SQL; hiding it breaks the product" — same
spirit: the SQL itself is the testable contract, not a live cluster's
behavior). A real-cluster smoke test belongs to restore_live.py's own
integration story (it already runs against docs/smoke-results.md's real
cluster) or a separate, explicitly-flagged DB-integration suite — not
commit.py's unit tests.

Covers bundle-r.md Session 3 test budget items relevant to commit.py:
    - captured_at fidelity to S3 timestamp
    - FAILED S3 day -> FAILED DB row
    - unchanged-day commit
    - partial-day (2 of 3 sources) representation
    - tenant scoping on every insert

Replay idempotency and retention-config-assertion are noted as
out-of-scope here (see bottom of file) - they belong to restore_live.py's
own test suite per the task's own scoping guidance.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from capture.sources import SOURCES
from recording.commit import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    build_recording_row,
    build_tariff_snapshot_row,
    carrier_scac_for_source,
    commit_day,
    commit_source_day,
    parse_captured_at,
)

TENANT_ID = "10000000-0000-4000-8000-000000000002"
CARRIER_ID_BY_SCAC = {
    "NOLU": "11111111-1111-1111-1111-111111111111",
    "BHMU": "22222222-2222-2222-2222-222222222222",
    "HVTM": "33333333-3333-3333-3333-333333333333",
}

NORTHSTAR_SOURCE = next(s for s in SOURCES if s.key == "northstar-ocean-demo-tariff")
BLUEHAVEN_SOURCE = next(s for s in SOURCES if s.key == "bluehaven-maritime-demo-tariff")
HVTM_SOURCE = next(s for s in SOURCES if s.key == "harborview-terminal-demo-tariff")

CAPTURED_AT_ISO = "2026-07-04T08:00:14+00:00"


def _ok_manifest(source, *, captured_at: str = CAPTURED_AT_ISO, sha256_prev_delta="changed"):
    return {
        "source_key": source.key,
        "lane_label": source.lane_label,
        "url": source.url,
        "status": "ok",
        "http_status": 200,
        "headers": {"content-type": source.expected_content_type},
        "sha256": "abc123deadbeef",
        "byte_count": 1234,
        "captured_at": captured_at,
        "body_key": f"raw/{source.key}/2026-07-04/body.html",
        "sha256_prev_delta": sha256_prev_delta,
    }


def _failed_manifest(
    source, *, captured_at: str = CAPTURED_AT_ISO, error="503 Service Unavailable"
):
    return {
        "source_key": source.key,
        "lane_label": source.lane_label,
        "url": source.url,
        "status": "failed",
        "http_status": 503,
        "headers": {},
        "sha256": None,
        "byte_count": None,
        "captured_at": captured_at,
        "error": error,
    }


def _skipped_manifest(source, *, captured_at: str = CAPTURED_AT_ISO):
    return {
        "source_key": source.key,
        "status": "skipped",
        "reason": "already captured today",
    } | ({"captured_at": captured_at} if captured_at else {})


# ---------------------------------------------------------------------------
# Fakes: a minimal psycopg-3-shaped connection/cursor, matching the FakeS3Client
# pattern's spirit but for SQL. Enough surface for src.external.db.run_with_retry
# and commit.py's own cursor().execute()/.fetchone() calls to run against.
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql: str, params: dict | tuple | None = None):
        self._conn.executed.append((sql.strip(), params))
        self._last_result = self._conn.dispatch(sql, params)
        return self

    def fetchone(self):
        return self._last_result


class FakeTransactionCtx:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    """In-memory stand-in for the two tables commit.py writes to.

    Tracks every executed statement (for tenant-scoping assertions) and
    maintains just enough state to honor ON CONFLICT DO NOTHING semantics
    for the two unique indexes this module relies on, and the linking
    UPDATE.
    """

    def __init__(self):
        self.executed: list[tuple[str, dict | tuple | None]] = []
        self.tariff_snapshots: list[dict] = []
        self.recordings: list[dict] = []
        self._next_id = 1

    def _new_id(self) -> str:
        val = f"00000000-0000-0000-0000-{self._next_id:012d}"
        self._next_id += 1
        return val

    def transaction(self):
        return FakeTransactionCtx(self)

    def cursor(self):
        return FakeCursor(self)

    def dispatch(self, sql: str, params):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO tariff_snapshots"):
            key = (params["tenant_id"], params["carrier_id"], params["lane"], params["captured_at"])
            for row in self.tariff_snapshots:
                if (
                    row["tenant_id"],
                    row["carrier_id"],
                    row["lane"],
                    row["captured_at"],
                ) == key:
                    return None  # ON CONFLICT DO NOTHING - no RETURNING row
            new_id = self._new_id()
            row = dict(params)
            row["id"] = new_id
            row["recording_id"] = None
            self.tariff_snapshots.append(row)
            return (new_id,)
        if normalized.startswith("INSERT INTO recordings"):
            key = (
                params["tenant_id"],
                params["run_date"],
                params["target"],
                params["carrier_id"],
                params["lane"],
                params["terminal_code"],
            )
            for row in self.recordings:
                if (
                    row["tenant_id"],
                    row["run_date"],
                    row["target"],
                    row["carrier_id"],
                    row["lane"],
                    row["terminal_code"],
                ) == key:
                    return None
            new_id = self._new_id()
            row = dict(params)
            row["id"] = new_id
            self.recordings.append(row)
            return (new_id,)
        if normalized.startswith("UPDATE tariff_snapshots"):
            for row in self.tariff_snapshots:
                if row["tenant_id"] == params["tenant_id"] and row["id"] == params["snapshot_id"]:
                    row["recording_id"] = params["recording_id"]
            return None
        raise AssertionError(f"unexpected SQL dispatched: {normalized[:80]!r}")


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


def test_parse_captured_at_matches_manifest_isoformat_exactly():
    manifest = _ok_manifest(NORTHSTAR_SOURCE, captured_at="2026-04-03T08:00:14+00:00")
    parsed = parse_captured_at(manifest)
    assert parsed == datetime(2026, 4, 3, 8, 0, 14, tzinfo=timezone.utc)


def test_carrier_scac_for_source_covers_all_registered_sources():
    assert carrier_scac_for_source("northstar-ocean-demo-tariff") == "NOLU"
    assert carrier_scac_for_source("bluehaven-maritime-demo-tariff") == "BHMU"
    assert carrier_scac_for_source("harborview-terminal-demo-tariff") == "HVTM"


def test_carrier_scac_for_source_raises_on_unknown_key():
    with pytest.raises(KeyError):
        carrier_scac_for_source("not-a-real-source")


def test_build_tariff_snapshot_row_captured_at_fidelity_to_manifest():
    """captured_at MUST come from the manifest's own field, never now()."""
    manifest = _ok_manifest(NORTHSTAR_SOURCE, captured_at="2026-04-03T08:00:14+00:00")
    row = build_tariff_snapshot_row(
        tenant_id=TENANT_ID,
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
    )
    assert row["captured_at"] == datetime(2026, 4, 3, 8, 0, 14, tzinfo=timezone.utc)
    assert row["effective_date"] == date(2026, 4, 3)
    assert row["version_label"] == "v2026-04-03"


def test_build_tariff_snapshot_row_has_no_extraction_or_rate_fields():
    """Lock 3: doc_text placeholder, headline_rate NULL - no extraction/parsing."""
    manifest = _ok_manifest(NORTHSTAR_SOURCE)
    row = build_tariff_snapshot_row(
        tenant_id=TENANT_ID,
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
    )
    assert row["doc_text"] == ""
    assert row["headline_rate"] is None
    assert row["doc_sha256"] == manifest["sha256"]
    assert row["s3_key"] == manifest["body_key"]
    assert row["source_url"] == manifest["url"]


def test_build_tariff_snapshot_row_s3_key_none_when_no_body_key():
    """Failed-shaped manifests never reach this function, but a manifest
    missing body_key (defensive) should surface s3_key=None, not KeyError."""
    manifest = _ok_manifest(NORTHSTAR_SOURCE)
    del manifest["body_key"]
    row = build_tariff_snapshot_row(
        tenant_id=TENANT_ID,
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
    )
    assert row["s3_key"] is None


def test_build_recording_row_ok_manifest_maps_to_committed():
    manifest = _ok_manifest(NORTHSTAR_SOURCE)
    row = build_recording_row(
        tenant_id=TENANT_ID,
        run_date=date(2026, 7, 4),
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        lane=NORTHSTAR_SOURCE.lane_label,
        manifest=manifest,
        manifest_key="raw/northstar-ocean-demo-tariff/2026-07-04/manifest.json",
        rows_written=1,
    )
    assert row["status"] == STATUS_COMMITTED
    assert row["rows_written"] == 1
    assert row["error"] is None
    assert row["started_at"] == parse_captured_at(manifest)


def test_build_recording_row_reads_invocation_from_manifest():
    """Bundle R addendum: invocation ("scheduled"|"manual") is disclosure,
    read straight from the manifest's own field, never invented here."""
    manifest = _ok_manifest(NORTHSTAR_SOURCE)
    manifest["invocation"] = "scheduled"
    row = build_recording_row(
        tenant_id=TENANT_ID,
        run_date=date(2026, 7, 4),
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        lane=NORTHSTAR_SOURCE.lane_label,
        manifest=manifest,
        manifest_key="raw/northstar-ocean-demo-tariff/2026-07-04/manifest.json",
        rows_written=1,
    )
    assert row["invocation"] == "scheduled"


def test_build_recording_row_defaults_invocation_to_manual_when_absent_from_manifest():
    """A manifest written before the invocation field existed (July 4-5,
    2026, pre-Bundle-R-addendum) has no "invocation" key at all - this must
    default to "manual", which is also the historically accurate value for
    those specific days (see scripts/annotate_manual_invocations.py)."""
    manifest = _ok_manifest(NORTHSTAR_SOURCE)
    assert "invocation" not in manifest
    row = build_recording_row(
        tenant_id=TENANT_ID,
        run_date=date(2026, 7, 4),
        carrier_id=CARRIER_ID_BY_SCAC["NOLU"],
        lane=NORTHSTAR_SOURCE.lane_label,
        manifest=manifest,
        manifest_key="raw/northstar-ocean-demo-tariff/2026-07-04/manifest.json",
        rows_written=1,
    )
    assert row["invocation"] == "manual"


def test_build_recording_row_failed_manifest_maps_to_failed():
    manifest = _failed_manifest(NORTHSTAR_SOURCE, error="503 Service Unavailable")
    row = build_recording_row(
        tenant_id=TENANT_ID,
        run_date=date(2026, 7, 4),
        carrier_id=None,
        lane=None,
        manifest=manifest,
        manifest_key="raw/northstar-ocean-demo-tariff/2026-07-04/manifest.json",
        rows_written=0,
    )
    assert row["status"] == STATUS_FAILED
    assert row["rows_written"] == 0
    assert row["error"] == "503 Service Unavailable"


def test_build_recording_row_skipped_manifest_maps_to_skipped():
    manifest = _skipped_manifest(NORTHSTAR_SOURCE)
    row = build_recording_row(
        tenant_id=TENANT_ID,
        run_date=date(2026, 7, 4),
        carrier_id=None,
        lane=None,
        manifest=manifest,
        manifest_key="raw/northstar-ocean-demo-tariff/2026-07-04/manifest.json",
        rows_written=0,
    )
    assert row["status"] == STATUS_SKIPPED


# ---------------------------------------------------------------------------
# commit_source_day: DB-facing tests via FakeConnection
# ---------------------------------------------------------------------------


def test_commit_source_day_ok_writes_linked_snapshot_and_recording():
    conn = FakeConnection()
    manifest = _ok_manifest(NORTHSTAR_SOURCE, captured_at="2026-04-03T08:00:14+00:00")

    result = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert result.recording_status == STATUS_COMMITTED
    assert result.rows_written == 1
    assert len(conn.tariff_snapshots) == 1
    assert len(conn.recordings) == 1

    snapshot = conn.tariff_snapshots[0]
    recording = conn.recordings[0]
    # Linking: the snapshot's recording_id must point at the recording just inserted.
    assert snapshot["recording_id"] == recording["id"]
    assert result.recording_id == recording["id"]
    assert result.tariff_snapshot_id == snapshot["id"]

    # captured_at fidelity: exactly the manifest's own value, not now().
    assert snapshot["captured_at"] == datetime(2026, 4, 3, 8, 0, 14, tzinfo=timezone.utc)


def test_commit_source_day_captured_at_fidelity_to_s3_timestamp():
    """The defining assertion for the 'captured_at fidelity to S3 timestamp'
    budget item: an arbitrary, distinctive manifest captured_at must survive
    unmodified into both tables' timestamp columns."""
    conn = FakeConnection()
    distinctive_ts = "2026-04-03T08:00:14+00:00"
    manifest = _ok_manifest(BLUEHAVEN_SOURCE, captured_at=distinctive_ts)

    commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=BLUEHAVEN_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    expected = datetime.fromisoformat(distinctive_ts)
    assert conn.tariff_snapshots[0]["captured_at"] == expected
    assert conn.recordings[0]["started_at"] == expected


def test_commit_source_day_failed_manifest_writes_failed_recording_no_snapshot():
    """FAILED S3 day -> FAILED DB row (the hollow tick exists from birth)."""
    conn = FakeConnection()
    manifest = _failed_manifest(NORTHSTAR_SOURCE)

    result = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert result.recording_status == STATUS_FAILED
    assert result.rows_written == 0
    assert result.tariff_snapshot_id is None
    assert len(conn.tariff_snapshots) == 0
    assert len(conn.recordings) == 1
    assert conn.recordings[0]["status"] == STATUS_FAILED
    assert conn.recordings[0]["error"] == manifest["error"]
    assert conn.recordings[0]["carrier_id"] is None


def test_commit_source_day_skipped_manifest_writes_skipped_recording():
    conn = FakeConnection()
    manifest = _skipped_manifest(NORTHSTAR_SOURCE)

    result = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert result.recording_status == STATUS_SKIPPED
    assert len(conn.tariff_snapshots) == 0
    assert len(conn.recordings) == 1
    assert conn.recordings[0]["status"] == STATUS_SKIPPED


def test_commit_source_day_unchanged_hash_still_commits_its_own_row():
    """Unchanged-day commit: an unchanged tariff observed today is still a
    distinct observation and gets its own tariff_snapshots row, with
    doc_sha256 literally equal to the (simulated) prior day's hash."""
    conn = FakeConnection()
    shared_sha = "same-hash-as-yesterday"

    yesterday_manifest = _ok_manifest(
        NORTHSTAR_SOURCE, captured_at="2026-07-03T08:00:00+00:00", sha256_prev_delta="changed"
    )
    yesterday_manifest["sha256"] = shared_sha
    today_manifest = _ok_manifest(
        NORTHSTAR_SOURCE, captured_at="2026-07-04T08:00:00+00:00", sha256_prev_delta="unchanged"
    )
    today_manifest["sha256"] = shared_sha

    commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=yesterday_manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )
    result_today = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=today_manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert result_today.recording_status == STATUS_COMMITTED
    assert result_today.rows_written == 1
    # Two distinct rows, same doc_sha256 - "commit the day, point at prior content".
    assert len(conn.tariff_snapshots) == 2
    hashes = {row["doc_sha256"] for row in conn.tariff_snapshots}
    assert hashes == {shared_sha}
    captured_ats = {row["captured_at"] for row in conn.tariff_snapshots}
    assert len(captured_ats) == 2  # distinct observation days


def test_commit_source_day_idempotent_rerun_produces_same_rows():
    """Bonus coverage (primary ownership noted as restore_live.py's, but
    cheap to assert here too since commit_source_day is the unit under
    test): committing the identical manifest twice must not duplicate rows."""
    conn = FakeConnection()
    manifest = _ok_manifest(NORTHSTAR_SOURCE)

    first = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )
    second = commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert len(conn.tariff_snapshots) == 1
    assert len(conn.recordings) == 1
    assert first.tariff_snapshot_id == conn.tariff_snapshots[0]["id"]
    # Second run hit ON CONFLICT DO NOTHING - no row identity to report.
    assert second.rows_written == 0
    assert second.tariff_snapshot_id is None


def test_commit_source_day_tenant_scoping_on_every_insert():
    """Every INSERT's bound params must carry the correct tenant_id -
    never a default, never omitted."""
    conn = FakeConnection()
    manifest = _ok_manifest(NORTHSTAR_SOURCE)

    commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    insert_statements = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO tariff_snapshots")
        or sql.startswith("INSERT INTO recordings")
    ]
    assert len(insert_statements) == 2
    for sql, params in insert_statements:
        assert "tenant_id" in params
        assert params["tenant_id"] == TENANT_ID
    # And the rows actually landed with the right tenant_id.
    assert conn.tariff_snapshots[0]["tenant_id"] == TENANT_ID
    assert conn.recordings[0]["tenant_id"] == TENANT_ID


def test_commit_source_day_tenant_scoping_on_failed_day_recording_insert():
    """Tenant scoping must hold even on the FAILED-day path (only one insert)."""
    conn = FakeConnection()
    manifest = _failed_manifest(NORTHSTAR_SOURCE)

    commit_source_day(
        conn,
        tenant_id=TENANT_ID,
        source=NORTHSTAR_SOURCE,
        manifest=manifest,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    insert_statements = [
        (sql, params) for sql, params in conn.executed if sql.startswith("INSERT INTO recordings")
    ]
    assert len(insert_statements) == 1
    assert insert_statements[0][1]["tenant_id"] == TENANT_ID
    assert conn.recordings[0]["tenant_id"] == TENANT_ID


# ---------------------------------------------------------------------------
# commit_day: partial-day representation
# ---------------------------------------------------------------------------


class FakeManifestS3Client:
    """Minimal S3 stand-in: only get_object, keyed by manifest_key."""

    def __init__(self, manifests_by_key: dict[str, dict]):
        self._manifests_by_key = manifests_by_key

    def get_object(self, Bucket, Key):
        import json

        from botocore.exceptions import ClientError

        if Key not in self._manifests_by_key:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")

        class _Body:
            def __init__(self, data: bytes):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(json.dumps(self._manifests_by_key[Key]).encode("utf-8"))}


def test_commit_day_partial_day_two_of_three_sources_represented():
    """2 of 3 sources captured -> exactly 2 results, not 3 and not an error
    for the missing one."""
    conn = FakeConnection()
    run_date = date(2026, 7, 4)

    from capture.handler import build_manifest_key

    northstar_key = build_manifest_key(NORTHSTAR_SOURCE.key, run_date.isoformat())
    bluehaven_key = build_manifest_key(BLUEHAVEN_SOURCE.key, run_date.isoformat())
    # harborview-terminal-demo-tariff has NO manifest at all for this day - genuinely missing.

    manifests_by_key = {
        northstar_key: _ok_manifest(NORTHSTAR_SOURCE),
        bluehaven_key: _failed_manifest(BLUEHAVEN_SOURCE),
    }
    s3_client = FakeManifestS3Client(manifests_by_key)

    results = commit_day(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket="tally-demo-recordings-test",
        run_date=run_date,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert len(results) == 2
    by_source = {r.source_key: r for r in results}
    assert by_source[NORTHSTAR_SOURCE.key].recording_status == STATUS_COMMITTED
    assert by_source[BLUEHAVEN_SOURCE.key].recording_status == STATUS_FAILED
    assert HVTM_SOURCE.key not in by_source

    # DB-side: exactly 2 recordings rows, 1 tariff_snapshots row.
    assert len(conn.recordings) == 2
    assert len(conn.tariff_snapshots) == 1


def test_commit_day_all_three_sources_present_produces_three_results():
    conn = FakeConnection()
    run_date = date(2026, 7, 4)

    from capture.handler import build_manifest_key

    manifests_by_key = {
        build_manifest_key(NORTHSTAR_SOURCE.key, run_date.isoformat()): _ok_manifest(NORTHSTAR_SOURCE),
        build_manifest_key(BLUEHAVEN_SOURCE.key, run_date.isoformat()): _ok_manifest(BLUEHAVEN_SOURCE),
        build_manifest_key(HVTM_SOURCE.key, run_date.isoformat()): _ok_manifest(HVTM_SOURCE),
    }
    s3_client = FakeManifestS3Client(manifests_by_key)

    results = commit_day(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket="tally-demo-recordings-test",
        run_date=run_date,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert len(results) == 3
    assert all(r.recording_status == STATUS_COMMITTED for r in results)
    assert len(conn.tariff_snapshots) == 3


def test_commit_day_zero_of_three_sources_produces_no_results_no_error():
    """No manifests at all for the day - genuinely nothing happened yet;
    commit_day must not raise or fabricate rows."""
    conn = FakeConnection()
    run_date = date(2026, 7, 4)
    s3_client = FakeManifestS3Client({})

    results = commit_day(
        conn,
        s3_client,
        tenant_id=TENANT_ID,
        bucket="tally-demo-recordings-test",
        run_date=run_date,
        carrier_id_by_scac=CARRIER_ID_BY_SCAC,
    )

    assert results == []
    assert conn.recordings == []
    assert conn.tariff_snapshots == []


# ---------------------------------------------------------------------------
# Out of scope here (per task guidance, belongs to restore_live.py's suite):
#   - full replay-idempotency across a multi-day walk (covered minimally
#     above at the single-day/single-source level; the multi-day replay
#     loop itself is restore_live.py's own code, not commit.py's)
#   - retention config assertion matching smoke-results.md (that's a
#     migration/cluster-config assertion, not commit.py logic)
# ---------------------------------------------------------------------------

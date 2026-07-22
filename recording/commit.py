"""The commit path: S3 manifests -> committed CockroachDB rows.

Per docs/bundle-r.md Session 3: "Build the commit path as a library
(recording/commit.py) callable from two entry points: the Lambda (daily,
going forward) and restore_live.py (replay...)." This module is that
library. It does not itself decide when to run, or how to iterate multiple
days — callers (the Lambda's phase 2, or restore_live.py's replay loop)
own that; this module commits one source-day, or all registered
sources' one day, given a manifest already read from S3.

Design decision already made (carried forward, do not relitigate): all
three capture/sources.py sources commit into tariff_snapshots, not
terminal_snapshots — the demo terminal's content is a tariff/rate
document. terminal_snapshots is schema-ready but unused until a real
terminal gate-status source exists.

Scope lock (bundle-r.md lock 3): "No LLM, no embeddings, no Bedrock in
this bundle... clause extraction/embedding remains B2 scope." Snapshot
rows here carry raw-pointer + hash only:
    - doc_text: no extraction exists yet, so this is always "" (the
      column is NOT NULL per migrations/001a_recording_tables.sql, so an
      empty string is the honest placeholder, not invented content).
    - headline_rate: always NULL, no rate parsing in this bundle.
    - version_label / effective_date: no real carrier-version or
      effective-date parsing exists yet either; both are derived from the
      manifest's own captured_at date as a raw-pointer-level placeholder,
      not a parsed field.

Timestamp domain discipline (CLAUDE.md: "story-domain columns... are
DATA; system HLC timestamps are PROOF"):
    - captured_at (tariff_snapshots) and started_at (recordings) are the
      OBSERVATION domain: sourced ONLY from the manifest's own
      "captured_at" field (the S3/manifest's server-set timestamp per
      Bundle R lock 2), never from a client-supplied value or a
      commit-time now(). This is what the "captured_at fidelity to S3
      timestamp" test budget item asserts.
    - committed_at (both tables) is the SYSTEM domain: the actual moment
      of DB commit. Left to each column's DEFAULT now() rather than
      passed explicitly, since there is no meaningful "commit moment"
      available before the INSERT itself runs.

Atomicity: one tariff_snapshots row and its linked recordings row land
together or not at all, via one db.run_with_retry-wrapped transaction.
Ordering choice: insert tariff_snapshots FIRST (without recording_id),
then recordings, then UPDATE tariff_snapshots.recording_id — this ordering
was picked (over the reverse) because tariff_snapshots is the row that
matters even if linking fails outright (raw-pointer-first per Bundle R's
overall philosophy), and because ON CONFLICT DO NOTHING on the first
insert (idempotent replay) needs to short-circuit the rest of the
function cleanly, which reads more naturally as "insert snapshot; if it
was already there, stop" than juggling two RETURNING ids up front.

Idempotency (test budget: "replay idempotency (run twice -> same rows)"):
both inserts use ON CONFLICT ... DO NOTHING against the tables' existing
unique indexes (tariff_snap_day_idx, recordings_day_idx). DO NOTHING
(rather than DO UPDATE) was chosen because "run twice -> same rows"
reads most literally as a true no-op: nothing about a day's committed
row should change on a re-run, and DO NOTHING is simpler and has no
risk of silently overwriting a field with a slightly different
re-derived value on replay.

Tenant scoping (test budget: "tenant scoping on every insert"): every
INSERT below binds tenant_id explicitly in its VALUES list — never a
column default (no such default exists in the schema either) and never
inferred implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from typing import Any

from psycopg import Connection

from capture.handler import build_manifest_key
from capture.sources import SOURCES, Source
from src.external.db import run_with_retry

# source_key -> carrier SCAC. Doesn't exist anywhere else yet (per task
# spec): capture/sources.py's Source dataclass has no carrier field, so
# this is the one place that bridges capture's source registry to
# src/external/seed_demo_tenant.py's DEMO_CARRIERS scacs.
SOURCE_KEY_TO_SCAC: dict[str, str] = {
    "northstar-ocean-demo-tariff": "NOLU",
    "bluehaven-maritime-demo-tariff": "BHMU",
    "harborview-terminal-demo-tariff": "HVTM",
}

RECORDING_TARGET_TARIFF = "tariff"

STATUS_COMMITTED = "COMMITTED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"

# manifest["status"] (capture/handler.py) -> recordings.status (this table).
# "ok" commits a real snapshot row; "failed" and "skipped" both still get a
# recordings row (a hollow tick exists from birth), but skipped-in-S3 (the
# 09:00 retry no-op, meaning a manifest already existed and today's job
# just re-observed that) maps to SKIPPED, not FAILED, since nothing about
# the source's own fetch failed.
_MANIFEST_STATUS_TO_RECORDING_STATUS = {
    "ok": STATUS_COMMITTED,
    "failed": STATUS_FAILED,
    "skipped": STATUS_SKIPPED,
}


@dataclass(frozen=True)
class CommitResult:
    """Outcome of committing one source's one day. Pure data, for tests/callers."""

    source_key: str
    recording_status: str  # COMMITTED | FAILED | SKIPPED
    rows_written: int
    recording_id: str | None
    tariff_snapshot_id: str | None


def carrier_scac_for_source(source_key: str) -> str:
    """Look up the carrier SCAC for a registered source key.

    Raises:
        KeyError: if source_key has no entry in SOURCE_KEY_TO_SCAC — this
            is a registry bug (a source in capture/sources.py with no
            carrier mapping here), not a data-quality issue, so it should
            surface loudly rather than be swallowed.
    """
    return SOURCE_KEY_TO_SCAC[source_key]


def parse_captured_at(manifest: dict[str, Any]) -> datetime:
    """Parse the manifest's own captured_at field into a datetime.

    This is the ONLY place commit.py should ever read a timestamp destined
    for tariff_snapshots.captured_at / recordings.started_at — it must
    come from the manifest (the S3/observation domain), never from
    datetime.now() at commit time. Matches capture/handler.build_manifest's
    `captured_at.isoformat()` serialization exactly (fromisoformat is its
    exact inverse for the isoformat strings that function produces).
    """
    return datetime.fromisoformat(manifest["captured_at"])


def build_tariff_snapshot_row(
    *,
    tenant_id: str,
    carrier_id: str,
    source: Source,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Pure function: manifest + context -> the tariff_snapshots row-shape dict.

    Only called for an "ok" manifest (the caller is responsible for that
    branch) — an "ok" manifest is guaranteed to carry body_key and sha256
    per capture/handler.py's build_manifest contract.

    doc_text is always "" (no extraction in this bundle, see module
    docstring); headline_rate is always None (no rate parsing).
    version_label / effective_date are raw-pointer-level placeholders
    derived from captured_at's own date — no real carrier-version or
    effective-date parsing exists yet, that's later-bundle scope.
    """
    captured_at = parse_captured_at(manifest)
    captured_date = captured_at.date()
    return {
        "tenant_id": tenant_id,
        "carrier_id": carrier_id,
        "lane": source.lane_label,
        "version_label": f"v{captured_date.isoformat()}",
        "effective_date": captured_date,
        "captured_at": captured_at,
        "source_url": manifest["url"],
        "s3_key": manifest.get("body_key"),
        "doc_sha256": manifest["sha256"],
        "doc_text": "",  # no clause extraction in this bundle - lock 3
        "headline_rate": None,  # no rate parsing in this bundle
    }


def build_recording_row(
    *,
    tenant_id: str,
    run_date: date_type,
    carrier_id: str | None,
    lane: str | None,
    manifest: dict[str, Any],
    manifest_key: str,
    rows_written: int,
) -> dict[str, Any]:
    """Pure function: manifest + context -> the recordings row-shape dict.

    Handles all three manifest statuses ("ok" -> COMMITTED, "failed" ->
    FAILED, "skipped" -> SKIPPED). started_at is a proxy: there's no
    separate "when did the DB commit job start" timestamp available from
    just a manifest, so captured_at is reused as the most honest available
    value (still observation-domain, not invented).

    s3_key on the recordings row points at the manifest object itself
    (falling back to body_key when present) — this is the row's own audit
    pointer to what was read to produce it, distinct from
    tariff_snapshots.s3_key which points at the raw body.

    invocation ("scheduled" | "manual", migration 001b) is read straight
    from the manifest's own "invocation" field (capture/handler.py's
    build_manifest already disclosed it there) - defaults to "manual" if
    absent (a manifest written before this field existed). This is
    disclosure, not a commit-success signal: a manual day still commits
    normally, per Bundle R's raw-bytes-first discipline (lock 1).
    """
    manifest_status = manifest.get("status", "failed")
    recording_status = _MANIFEST_STATUS_TO_RECORDING_STATUS.get(manifest_status, STATUS_FAILED)
    captured_at = parse_captured_at(manifest)
    return {
        "tenant_id": tenant_id,
        "run_date": run_date,
        "target": RECORDING_TARGET_TARIFF,
        "carrier_id": carrier_id,
        "lane": lane,
        "terminal_code": None,
        "status": recording_status,
        "rows_written": rows_written,
        "s3_key": manifest.get("body_key") or manifest_key,
        "error": manifest.get("error"),
        "started_at": captured_at,
        "invocation": manifest.get("invocation", "manual"),
    }


_INSERT_TARIFF_SNAPSHOT_SQL = """
    INSERT INTO tariff_snapshots (
        tenant_id, carrier_id, lane, version_label, effective_date,
        captured_at, source_url, s3_key, doc_sha256, doc_text, headline_rate
    )
    VALUES (%(tenant_id)s, %(carrier_id)s, %(lane)s, %(version_label)s, %(effective_date)s,
            %(captured_at)s, %(source_url)s, %(s3_key)s, %(doc_sha256)s, %(doc_text)s,
            %(headline_rate)s)
    ON CONFLICT (tenant_id, carrier_id, lane, captured_at) DO NOTHING
    RETURNING id;
"""

_INSERT_RECORDING_SQL = """
    INSERT INTO recordings (
        tenant_id, run_date, target, carrier_id, lane, terminal_code,
        status, rows_written, s3_key, error, started_at, invocation
    )
    VALUES (%(tenant_id)s, %(run_date)s, %(target)s, %(carrier_id)s, %(lane)s, %(terminal_code)s,
            %(status)s, %(rows_written)s, %(s3_key)s, %(error)s, %(started_at)s, %(invocation)s)
    ON CONFLICT ON CONSTRAINT recordings_day_idx DO NOTHING
    RETURNING id;
"""

_LINK_SNAPSHOT_TO_RECORDING_SQL = """
    UPDATE tariff_snapshots
    SET recording_id = %(recording_id)s
    WHERE tenant_id = %(tenant_id)s AND id = %(snapshot_id)s;
"""


def commit_source_day(
    conn: Connection,
    *,
    tenant_id: str,
    source: Source,
    manifest: dict[str, Any],
    carrier_id_by_scac: dict[str, str],
    run_date: date_type | None = None,
) -> CommitResult:
    """Commit one source's one day to the DB, given its already-read manifest.

    `manifest` is the parsed manifest.json dict (capture/handler.py's
    shape: source_key, lane_label, url, status, http_status, headers,
    sha256, byte_count, captured_at, and status-specific fields body_key/
    sha256_prev_delta [ok] or error [failed]).

    `carrier_id_by_scac` maps SCAC -> carrier_id (UUID string), e.g. as
    looked up from the carriers table once per run by the caller — this
    function does not query it itself, keeping DB reads/writes explicit
    and the function's dependencies fully visible in its signature.

    `run_date` defaults to the manifest's own captured_at date if not
    given (the normal case); callers replaying historical days may pass it
    explicitly if they have it from the S3 key already, but it must always
    agree with captured_at's date in practice.

    Wrapped in one transaction via db.run_with_retry: the tariff_snapshots
    insert (only on "ok"), the recordings insert, and the linking UPDATE
    either all land or none do.

    Idempotent: re-running with the same manifest for the same day
    produces the same rows (ON CONFLICT DO NOTHING on both inserts) rather
    than duplicates or an error.
    """
    captured_at = parse_captured_at(manifest)
    effective_run_date = run_date or captured_at.date()
    manifest_status = manifest.get("status", "failed")
    manifest_key = build_manifest_key(source.key, effective_run_date.isoformat())

    carrier_id: str | None = None
    if manifest_status == "ok":
        scac = carrier_scac_for_source(source.key)
        carrier_id = carrier_id_by_scac[scac]

    def _do_commit(conn: Connection) -> CommitResult:
        tariff_snapshot_id: str | None = None
        rows_written = 0

        if manifest_status == "ok":
            snapshot_row = build_tariff_snapshot_row(
                tenant_id=tenant_id,
                carrier_id=carrier_id,
                source=source,
                manifest=manifest,
            )
            with conn.cursor() as cur:
                cur.execute(_INSERT_TARIFF_SNAPSHOT_SQL, snapshot_row)
                inserted = cur.fetchone()
                if inserted is not None:
                    tariff_snapshot_id = str(inserted[0])
                    rows_written = 1

        recording_row = build_recording_row(
            tenant_id=tenant_id,
            run_date=effective_run_date,
            carrier_id=carrier_id,
            lane=source.lane_label if manifest_status == "ok" else None,
            manifest=manifest,
            manifest_key=manifest_key,
            rows_written=rows_written,
        )
        with conn.cursor() as cur:
            cur.execute(_INSERT_RECORDING_SQL, recording_row)
            inserted = cur.fetchone()
            recording_id = str(inserted[0]) if inserted is not None else None

        if tariff_snapshot_id is not None and recording_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    _LINK_SNAPSHOT_TO_RECORDING_SQL,
                    {
                        "recording_id": recording_id,
                        "tenant_id": tenant_id,
                        "snapshot_id": tariff_snapshot_id,
                    },
                )

        return CommitResult(
            source_key=source.key,
            recording_status=_MANIFEST_STATUS_TO_RECORDING_STATUS.get(
                manifest_status, STATUS_FAILED
            ),
            rows_written=rows_written,
            recording_id=recording_id,
            tariff_snapshot_id=tariff_snapshot_id,
        )

    return run_with_retry(conn, _do_commit)


def read_manifest(s3_client: Any, bucket: str, manifest_key: str) -> dict[str, Any] | None:
    """Read and parse one manifest.json from S3.

    Returns None if the object doesn't exist at all (a genuinely missing
    day — capture never ran, distinct from a FAILED day where capture ran
    and recorded a failure). Any other S3 error propagates: a transient
    error here must not be silently mistaken for "no capture happened".
    """
    import json

    from botocore.exceptions import ClientError

    try:
        response = s3_client.get_object(Bucket=bucket, Key=manifest_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise

    body = response["Body"].read()
    return json.loads(body)


def commit_day(
    conn: Connection,
    s3_client: Any,
    *,
    tenant_id: str,
    bucket: str,
    run_date: date_type,
    carrier_id_by_scac: dict[str, str],
    sources: tuple[Source, ...] = SOURCES,
) -> list[CommitResult]:
    """Commit one calendar day across all (or a given subset of) sources.

    Loops over `sources` independently, like capture/handler.lambda_handler
    does over SOURCES: a source with no manifest at all for this day is
    genuinely missing (capture never ran) and is skipped from the result
    list entirely — NOT represented as a FAILED or SKIPPED recordings row,
    since there is nothing to commit and fabricating a row would imply a
    commit attempt that never happened. A source whose manifest DOES exist
    or "failed" or "skipped" per Bundle R's own capture semantics gets a
    real recordings row via commit_source_day per that manifest's status.

    This is what gives partial-day representation (test budget: "2 of 3
    sources"): a day with manifests for 2 of 3 sources produces results
    for exactly those 2, regardless of their status.
    """
    results: list[CommitResult] = []
    for source in sources:
        manifest_key = build_manifest_key(source.key, run_date.isoformat())
        manifest = read_manifest(s3_client, bucket, manifest_key)
        if manifest is None:
            continue  # genuinely missing day - capture never ran, nothing to commit
        result = commit_source_day(
            conn,
            tenant_id=tenant_id,
            source=source,
            manifest=manifest,
            carrier_id_by_scac=carrier_id_by_scac,
            run_date=run_date,
        )
        results.append(result)
    return results


def load_carrier_id_by_scac(conn: Connection, tenant_id: str) -> dict[str, str]:
    """Look up every carrier's id by SCAC for one tenant.

    Small convenience so callers (Lambda phase 2, restore_live.py) don't
    each hand-roll this query; not used by commit_source_day/commit_day
    themselves, which take the mapping as a plain dict so their own tests
    never need a real cursor.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT scac, id FROM carriers WHERE tenant_id = %s;", (tenant_id,))
        return {scac: str(carrier_id) for scac, carrier_id in cur.fetchall()}

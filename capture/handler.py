"""Lambda capture handler for Tally's Bundle R.

Fetches each registered source (capture/sources.py) once per invocation and
archives raw bytes + a manifest to S3. This module is deliberately dumb:
no parsing, no DB writes (see docs/bundle-r.md "Out of scope" per session).
Those land in later sessions.

As of Session 2 (docs/bundle-r.md "Unattended & Honest"), it also does two
things beyond raw archiving: (1) emits structured JSON log lines (groundwork
for TDD §9's CloudWatch embedded-metric-format discipline), and (2) computes
content-hash change detection at capture time — each successful capture's
manifest gets `sha256_prev_delta: "changed"|"unchanged"` by comparing against
a per-source rolling `raw/{source_key}/latest.json` pointer object.

As of Session 3 (docs/bundle-r.md "The Database Learns to Remember"),
`lambda_handler` also runs a phase 2, AFTER phase 1's S3 capture loop
completes: it attempts to commit today's day to CockroachDB via
`recording.commit.commit_day`. Per the bundle's own pre-flight recommendation
("same function, phase 2 wrapped in try") and Lock 4 ("Capture code must
never block on, or be coupled to, database availability"), phase 2 is wrapped
in one broad try/except that swallows ANY exception — a missing
TALLY_CRDB_DSN, an unreachable cluster, a commit_day internal error, anything
— so a DB outage can never fail this Lambda invocation or take down the S3
capture that phase 1 already durably committed. The outcome (success or the
caught exception) is logged via `_log_event` and also surfaced in the return
value under `"db_commit"` for operator visibility — additive only, so it
never changes the meaning of the pre-existing `captured_at`/`results` keys.

Design for testability (CLAUDE.md: "All external calls mocked in tests;
zero network calls in the test suite"):
    - Pure functions (`build_object_prefix`, `infer_extension`,
      `build_manifest`) take plain values in, plain values out — no I/O.
    - I/O functions (`fetch_source`, `capture_one_source`) take an injected
      httpx client and S3 client rather than constructing their own, so
      tests can pass in fakes/mocks.
    - `lambda_handler` is the only place that constructs real clients, and
      it's the only function AWS ever calls directly.

Session locks in play (docs/bundle-r.md):
    - Lock 1: a day COUNTS if bytes landed in S3, even on non-200 — a
      failed fetch still gets a manifest with status "failed".
    - Lock 2: server-set timestamps only. `capture_one_source` takes the
      capture date/timestamp as an argument from the caller, which always
      derives it from `datetime.now(timezone.utc)` — never from anything
      client- or response-supplied.
    - Lock 4: capture (phase 1, S3) never touches a database, and the
      database commit (phase 2, Session 3) must never be able to block on
      or fail the capture — phase 2 is entirely best-effort.
    - Lock 5: one request per source per run, honest User-Agent.

Log payload discipline (TDD §9, quoted): "prompts/PDF text are not
logged... hashes and token counts are." This module's equivalent: raw
response bodies (HTML/PDF text) are never logged — only hashes, byte
counts, HTTP status codes, and short error strings.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import httpx

from capture.sources import SOURCES, Source

logger = logging.getLogger("tally.capture")
logger.setLevel(logging.INFO)

USER_AGENT = "TallyRecorder/0.1 (+https://github.com/markbrazinski/tally-agent)"

DEFAULT_BUCKET = "tally-demo-recordings"
REQUEST_TIMEOUT_SECONDS = 20.0

# Minimal, deliberately small content-type -> extension map. Anything not
# listed here falls back to ".bin" — Session 1 only registers text/html and
# application/pdf sources, so this is intentionally not exhaustive.
_CONTENT_TYPE_EXTENSIONS = {
    "text/html": "html",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "application/json": "json",
}


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of attempting to capture one source. Pure data, for tests."""

    source_key: str
    status: str  # "ok" | "failed" | "skipped"
    manifest: dict[str, Any]


def _log_event(level: int, event: str, **fields: Any) -> None:
    """Emit one structured JSON log line with a consistent field shape.

    Groundwork for TDD §9's "structured JSON logs... embedded metric
    format" discipline (docs/bundle-r.md Session 2 "Polish absorbed":
    "Lambda structured JSON logging (embedded-metric format groundwork
    for §9)"). This is deliberately NOT full CloudWatch EMF (no
    _aws/metric-directive block) - just a consistent, parseable JSON
    payload every call site in this module emits, so a future metric
    filter (or the verifier Lambda's example recovery-gap alarm) can grep
    on `"event": "capture_failed"` etc. without a schema migration later.

    Payload discipline (TDD §9, quoted): "prompts/PDF text are not
    logged... hashes and token counts are." This module's equivalent:
    never pass raw response body/HTML/PDF text as a field here - only
    hashes, byte counts, status codes, and short error strings. Callers
    are responsible for not doing that; this helper just centralizes the
    json.dumps so every line has the same shape.
    """
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload))


def _already_captured_result(
    source_key: str, manifest_key: str, invocation: str = "manual"
) -> CaptureResult:
    """The "skipped" outcome: today's manifest already exists for this source.

    Reached two ways: manifest_exists() found it up front (the normal
    09:00 retry no-op case), or a concurrent invocation won the race to
    write it first (ManifestAlreadyWritten from _write_manifest) - both
    mean the same thing to the caller, so they share one result shape.
    """
    _log_event(
        logging.INFO,
        "capture_skipped",
        source_key=source_key,
        status="skipped",
        invocation=invocation,
        manifest_key=manifest_key,
        reason="already_captured",
    )
    return CaptureResult(
        source_key=source_key,
        status="skipped",
        manifest={
            "source_key": source_key,
            "status": "skipped",
            "invocation": invocation,
            "reason": "already captured today",
        },
    )


def infer_extension(content_type: str) -> str:
    """Map a Content-Type (expected or actual) to a file extension.

    Keeps this simple per the task spec: strips any `;charset=...` suffix,
    looks up a small known table, defaults to "bin".
    """
    base = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(base, "bin")


def build_object_prefix(source_key: str, capture_date: str) -> str:
    """Build the S3 key prefix for one source on one UTC calendar date.

    `capture_date` must already be a "YYYY-MM-DD" string — callers derive it
    from a server-set UTC clock (see lock 2), never from client input. This
    function is pure: same inputs always produce the same output, so it's
    testable across UTC date boundaries without touching a clock.
    """
    return f"raw/{source_key}/{capture_date}"


def build_body_key(source_key: str, capture_date: str, ext: str) -> str:
    """Full S3 key for the raw body object."""
    return f"{build_object_prefix(source_key, capture_date)}/body.{ext}"


def build_manifest_key(source_key: str, capture_date: str) -> str:
    """Full S3 key for the manifest object."""
    return f"{build_object_prefix(source_key, capture_date)}/manifest.json"


def build_latest_key(source_key: str) -> str:
    """S3 key for a source's rolling `latest.json` pointer.

    Deliberately NOT date-scoped: this is a sibling of the
    `raw/{source_key}/{date}/` prefixes, not inside one — one pointer per
    source, overwritten on every successful capture (bundle-r.md Session
    2: "per-source latest.json pointer object").
    """
    return f"raw/{source_key}/latest.json"


def build_manifest(
    *,
    source: Source,
    captured_at: datetime,
    status: str,
    invocation: str = "manual",
    http_status: int | None = None,
    headers_subset: dict[str, str] | None = None,
    sha256_hex: str | None = None,
    byte_count: int | None = None,
) -> dict[str, Any]:
    """Assemble the manifest dict written alongside the raw body.

    Pure function: given a fixed `captured_at` and fetch outcome, always
    produces the same dict. `status` is "ok" or "failed" (lock 1: a failed
    fetch still gets recorded, never silently dropped).

    `invocation` is "scheduled" | "manual" - provenance, not a capture
    outcome. EventBridge Scheduler is configured (deploy.sh) to pass an
    explicit Input `{"invocation": "scheduled"}` to the Lambda; any other
    trigger (a bare `aws lambda invoke`, the AWS console's "Test" button,
    a local dev invocation) has no such key in `event` and defaults to
    "manual" here. This disclosure exists per the Bundle R addendum: a
    manually-invoked day still COUNTS as a recorded day (raw-bytes-first,
    lock 1) - it is disclosed, never hidden or deleted, so the coverage
    tile's honesty holds even for days a human kicked off by hand.
    """
    return {
        "source_key": source.key,
        "lane_label": source.lane_label,
        "url": source.url,
        "status": status,
        "invocation": invocation,
        "http_status": http_status,
        "headers": headers_subset or {},
        "sha256": sha256_hex,
        "byte_count": byte_count,
        "captured_at": captured_at.isoformat(),
    }


def _headers_subset(headers: httpx.Headers) -> dict[str, str]:
    """Pull a small, deliberate subset of response headers for the manifest.

    Per task spec: don't dump all headers, just enough to be useful.
    """
    subset = {}
    for key in ("content-type", "content-length", "last-modified", "etag"):
        value = headers.get(key)
        if value is not None:
            subset[key] = value
    return subset


def manifest_exists(s3_client: Any, bucket: str, manifest_key: str) -> bool:
    """Check whether a manifest object already exists at this key.

    Used to implement the 09:00 UTC retry no-op rule (bundle-r.md test #4):
    if today's manifest already exists for a source, skip re-fetching it.
    Uses head_object rather than get_object to avoid pulling the body.

    Only a confirmed 404/NoSuchKey response means "does not exist". Any
    other failure (throttling, a permissions hiccup, a network blip) is
    re-raised rather than silently treated as "not found" - swallowing
    those would let a transient error trigger a duplicate live fetch
    against the external source, which is exactly what the retry no-op
    exists to prevent (lock 5: one request per source per run).

    This check is also inherently racy against a concurrent invocation
    (manual invoke overlapping a scheduled run): it can only rule out the
    common case cheaply. `_write_manifest`'s conditional put (IfNoneMatch)
    is what actually makes the write itself safe under that race.
    """
    from botocore.exceptions import ClientError

    try:
        s3_client.head_object(Bucket=bucket, Key=manifest_key)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _read_latest_pointer_sha256(s3_client: Any, bucket: str, source_key: str) -> str | None:
    """Read the previous sha256 from a source's `latest.json`, or None.

    None means "treat this as if there were no prior capture" and covers
    three cases identically, all per bundle-r.md Session 2's own test
    budget ("malformed prior latest.json doesn't kill the run"):
        - the object doesn't exist yet (first-ever capture for this source)
        - a confirmed 404/NoSuchKey (same as above, different error shape)
        - the object exists but is unparseable JSON, or parses but has no
          usable "sha256" field (corrupt/foreign object at this key)

    Only a confirmed "not found" is treated as "doesn't exist" — any other
    ClientError (throttling, permissions) propagates, matching the same
    discipline as manifest_exists() and for the same reason: swallowing a
    transient error here would silently mislabel a real prior capture as
    "no history", which would wrongly mark a genuinely-unchanged day as
    "changed".
    """
    from botocore.exceptions import ClientError

    latest_key = build_latest_key(source_key)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=latest_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise

    try:
        body = response["Body"].read()
        pointer = json.loads(body)
        sha256_hex = pointer.get("sha256")
        return sha256_hex if isinstance(sha256_hex, str) else None
    except (ValueError, AttributeError, KeyError, TypeError):
        # Malformed prior latest.json (bad JSON, unexpected shape, or a
        # fake in tests without a real streaming Body) must not crash the
        # run - treat it exactly like "no prior pointer".
        _log_event(
            logging.WARNING,
            "capture_latest_pointer_malformed",
            source_key=source_key,
            status="ok",
            latest_key=latest_key,
        )
        return None


def _write_latest_pointer(
    s3_client: Any,
    bucket: str,
    source_key: str,
    *,
    sha256_hex: str,
    captured_at: datetime,
    manifest_key: str,
) -> None:
    """Overwrite the rolling `latest.json` pointer for this source.

    Unlike `_write_manifest`, this is NOT conditional (no IfNoneMatch) -
    it's supposed to be overwritten every successful day so the next
    day's capture can compare against it; it's a rolling pointer, not an
    immutable historical record like the dated manifests.
    """
    pointer = {
        "sha256": sha256_hex,
        "captured_at": captured_at.isoformat(),
        "manifest_key": manifest_key,
    }
    s3_client.put_object(
        Bucket=bucket,
        Key=build_latest_key(source_key),
        Body=json.dumps(pointer, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def fetch_source(http_client: httpx.Client, source: Source) -> httpx.Response:
    """Perform the single polite GET for one source (lock 5: one request).

    Raises whatever httpx raises on network failure; the caller
    (`capture_one_source`) is responsible for catching it so one source's
    failure never aborts the run (lock: per-source isolation).
    """
    return http_client.get(
        source.url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def capture_one_source(
    *,
    source: Source,
    http_client: httpx.Client,
    s3_client: Any,
    bucket: str,
    now: datetime,
    invocation: str = "manual",
) -> CaptureResult:
    """Fetch, hash, and archive exactly one source. Never raises.

    `now` must be a server-set UTC datetime supplied by the caller (lock 2)
    so this function stays pure with respect to time and is testable across
    UTC date boundaries without mocking a clock inside it.

    `invocation` is "scheduled" | "manual" provenance, threaded through to
    every manifest this call produces or observes (see build_manifest's
    docstring) - a Bundle R addendum disclosure requirement, not a capture
    outcome.

    Any exception during fetch or S3 write is caught here and turned into a
    "failed" CaptureResult — this is what gives per-source isolation at the
    handler level (one source's crash never stops the loop).
    """
    capture_date = now.strftime("%Y-%m-%d")
    manifest_key = build_manifest_key(source.key, capture_date)

    if manifest_exists(s3_client, bucket, manifest_key):
        return _already_captured_result(source.key, manifest_key, invocation)

    try:
        response = fetch_source(http_client, source)
    except Exception as exc:  # noqa: BLE001 - intentional: isolate any fetch failure
        _log_event(
            logging.WARNING,
            "capture_failed",
            source_key=source.key,
            status="failed",
            invocation=invocation,
            http_status=None,
            error=str(exc),
            captured_at=now.isoformat(),
        )
        manifest = build_manifest(
            source=source,
            captured_at=now,
            status="failed",
            invocation=invocation,
            http_status=None,
        )
        manifest["error"] = str(exc)
        try:
            _write_manifest(s3_client, bucket, manifest_key, manifest)
        except ManifestAlreadyWritten:
            return _already_captured_result(source.key, manifest_key, invocation)
        return CaptureResult(source_key=source.key, status="failed", manifest=manifest)

    body = response.content
    digest = sha256(body).hexdigest()
    byte_count = len(body)
    headers_subset = _headers_subset(response.headers)

    if response.status_code != 200:
        _log_event(
            logging.WARNING,
            "capture_failed",
            source_key=source.key,
            status="failed",
            invocation=invocation,
            http_status=response.status_code,
            bytes=byte_count,
            captured_at=now.isoformat(),
        )
        manifest = build_manifest(
            source=source,
            captured_at=now,
            status="failed",
            invocation=invocation,
            http_status=response.status_code,
            headers_subset=headers_subset,
            sha256_hex=digest,
            byte_count=byte_count,
        )
        try:
            _write_manifest(s3_client, bucket, manifest_key, manifest)
        except ManifestAlreadyWritten:
            return _already_captured_result(source.key, manifest_key, invocation)
        return CaptureResult(source_key=source.key, status="failed", manifest=manifest)

    content_type = response.headers.get("content-type", source.expected_content_type)
    ext = infer_extension(content_type)
    body_key = build_body_key(source.key, capture_date, ext)

    body_written = False
    try:
        s3_client.put_object(Bucket=bucket, Key=body_key, Body=body)
        body_written = True
        manifest = build_manifest(
            source=source,
            captured_at=now,
            status="ok",
            invocation=invocation,
            http_status=response.status_code,
            headers_subset=headers_subset,
            sha256_hex=digest,
            byte_count=byte_count,
        )
        manifest["body_key"] = body_key
        # Change-detection at capture time (bundle-r.md Session 2: "store
        # sha256_prev_delta... it's free to compute now"). Only meaningful
        # for a genuinely captured body, so this only runs on the "ok"
        # path - a non-200 or fetch-exception day never reaches here.
        # Read the PREVIOUS pointer before overwriting it, so the compare
        # is against yesterday's (or whenever-last-successful's) hash.
        previous_sha256 = _read_latest_pointer_sha256(s3_client, bucket, source.key)
        manifest["sha256_prev_delta"] = "unchanged" if previous_sha256 == digest else "changed"
        _write_manifest(s3_client, bucket, manifest_key, manifest)
        _write_latest_pointer(
            s3_client,
            bucket,
            source.key,
            sha256_hex=digest,
            captured_at=now,
            manifest_key=manifest_key,
        )
        _log_event(
            logging.INFO,
            "capture_ok",
            source_key=source.key,
            status="ok",
            invocation=invocation,
            http_status=response.status_code,
            bytes=byte_count,
            sha256_prev_delta=manifest["sha256_prev_delta"],
            captured_at=now.isoformat(),
        )
        return CaptureResult(source_key=source.key, status="ok", manifest=manifest)
    except ManifestAlreadyWritten:
        # The body write above already landed (harmless even if a racing
        # invocation's body write also lands - same bytes, S3 versioning
        # keeps both) but a concurrent invocation won the manifest write.
        # Treat this as the retry no-op case, not a failure.
        return _already_captured_result(source.key, manifest_key, invocation)
    except Exception as exc:  # noqa: BLE001 - intentional: isolate any S3 failure
        _log_event(
            logging.WARNING,
            "capture_failed",
            source_key=source.key,
            status="failed",
            invocation=invocation,
            http_status=response.status_code,
            bytes=byte_count,
            error=str(exc),
            captured_at=now.isoformat(),
        )
        manifest = build_manifest(
            source=source,
            captured_at=now,
            status="failed",
            invocation=invocation,
            http_status=response.status_code,
            headers_subset=headers_subset,
            sha256_hex=digest,
            byte_count=byte_count,
        )
        manifest["error"] = str(exc)
        if body_written:
            # The body itself landed in S3 even though the manifest write
            # that follows it failed - keep the pointer so the bytes are
            # never orphaned (lock 1: a day COUNTS if bytes landed in S3).
            manifest["body_key"] = body_key
        # Best-effort: still try to record that this happened. If this
        # write also fails, we've logged above and move on (per-source
        # isolation must hold even when S3 itself is unhappy).
        try:
            _write_manifest(s3_client, bucket, manifest_key, manifest)
        except ManifestAlreadyWritten:
            return _already_captured_result(source.key, manifest_key, invocation)
        except Exception as manifest_exc:  # noqa: BLE001
            _log_event(
                logging.ERROR,
                "capture_failed",
                source_key=source.key,
                status="failed",
                invocation=invocation,
                error=str(manifest_exc),
                captured_at=now.isoformat(),
            )
        return CaptureResult(source_key=source.key, status="failed", manifest=manifest)


class ManifestAlreadyWritten(Exception):
    """Raised when a concurrent invocation already wrote today's manifest.

    Surfaces the manifest_exists()-then-write TOCTOU gap as a distinct,
    handleable outcome instead of either a silent double-write or an
    unhandled S3 error (lock 5: one request per source per run, even
    under overlapping invocations - e.g. a manual invoke racing the
    scheduled run).
    """


def _write_manifest(
    s3_client: Any, bucket: str, manifest_key: str, manifest: dict[str, Any]
) -> None:
    """Write manifest.json, but only if nothing is already at this key.

    Uses S3's IfNoneMatch="*" precondition so the existence-check-then-write
    sequence is atomic at the point that actually matters: two overlapping
    invocations can both pass manifest_exists() (a TOCTOU gap that check
    alone can't close), but only one of their manifest writes will win here
    - the other gets a precondition failure, raised as ManifestAlreadyWritten
    so the caller can treat it as "someone else already captured today"
    rather than a real failure.
    """
    from botocore.exceptions import ClientError

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("PreconditionFailed", "412"):
            raise ManifestAlreadyWritten(manifest_key) from exc
        raise


def _remaining_time_ms(context: Any) -> float:
    """Milliseconds left in this Lambda invocation, or +inf if unknown.

    `context` is the real AWS Lambda context object in production (which
    provides get_remaining_time_in_millis()); tests pass None, and outside
    a real Lambda invocation there is no deadline to guard against.
    """
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return float("inf")
    return float(getter())


def _commit_today_to_db(
    *, s3_client: Any, bucket: str, run_date: datetime, invocation: str = "manual"
) -> dict[str, Any]:
    """Phase 2: best-effort DB commit of today's S3 captures. Never raises.

    Per docs/bundle-r.md Session 3's own pre-flight recommendation ("same
    function, phase 2 wrapped in try") and Lock 4 ("Capture code must never
    block on, or be coupled to, database availability"): this function
    catches ANY exception from any step below — seeding the demo tenant,
    opening the DB connection (including `get_dsn()` raising RuntimeError
    when TALLY_CRDB_DSN is unset), loading the carrier map, or
    `commit_day` itself — and turns it into a `{"status": "failed", ...}`
    result instead of propagating. `lambda_handler` calls this AFTER
    phase 1's S3 loop has already completed, so a DB outage can never lose
    or retroactively fail the S3 capture that already succeeded.

    `s3_client` is the same real client phase 1 already built — reused here
    (not re-constructed) so `commit_day` reads manifests via one client per
    invocation, same as phase 1's capture loop.

    Returns a plain dict describing the outcome, suitable both for the
    structured log (`_log_event`) and for the Lambda's own return value
    (`db_commit` key) — never a raised exception, by construction.
    """
    run_date_date = run_date.date()
    conn = None
    try:
        # Imported lazily, matching lambda_handler's existing lazy-import
        # style for the boto3/os pair below (keeps this branch's cost
        # invisible to any test path that doesn't exercise it, and avoids a
        # hard import-time dependency on psycopg for callers that only care
        # about phase 1). MUST be inside this try block, not before it: if
        # psycopg itself fails to import (e.g. a platform-mismatched binary
        # wheel bundled for the wrong architecture), that ImportError must
        # be caught here too, not propagate past this function - an import
        # failure is still a DB-availability problem Lock 4 says must never
        # break phase 1's already-succeeded S3 capture.
        from recording.commit import commit_day, load_carrier_id_by_scac
        from src.external import db as db_module
        from src.external.seed_demo_tenant import run_seed

        tenant_id = run_seed()
        conn = db_module.connect()
        carrier_id_by_scac = load_carrier_id_by_scac(conn, tenant_id)
        results = commit_day(
            conn,
            s3_client,
            tenant_id=tenant_id,
            bucket=bucket,
            run_date=run_date_date,
            carrier_id_by_scac=carrier_id_by_scac,
        )
        return {
            "status": "ok",
            "run_date": run_date_date.isoformat(),
            "invocation": invocation,
            "results": [
                {
                    "source_key": r.source_key,
                    "recording_status": r.recording_status,
                    "rows_written": r.rows_written,
                }
                for r in results
            ],
        }
    except Exception as exc:  # noqa: BLE001 - intentional: Lock 4, DB must never block capture
        return {
            "status": "failed",
            "run_date": run_date_date.isoformat(),
            "invocation": invocation,
            "error": str(exc),
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise out of phase 2 either
                pass


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint. Loops over every registered source.

    Constructs the real S3 client and httpx client here (the only place
    this module does so) and delegates everything else to
    `capture_one_source`, so tests exercise that function directly with
    injected fakes instead of mocking through the Lambda entrypoint.

    Per-source isolation: `capture_one_source` never raises, so a single
    source's failure cannot abort the loop over the remaining sources.

    Sources are fetched sequentially, each bounded by
    REQUEST_TIMEOUT_SECONDS; with enough registered sources this can
    exceed the Lambda's own configured timeout (deploy.sh sizes that
    timeout to comfortably exceed REQUEST_TIMEOUT_SECONDS * len(SOURCES),
    but this is a second, cheap line of defense). Before starting a new
    source's fetch, check the time actually remaining in this invocation;
    if there isn't enough left for a full-timeout attempt plus a margin
    for the S3 writes, skip it with an explicit "failed" manifest instead
    of letting AWS hard-kill the invocation mid-fetch, which would leave
    later sources with no record for the day at all (not even "failed").

    `event["invocation"]` disclosure (Bundle R addendum): EventBridge
    Scheduler is configured (deploy.sh) to pass an explicit Input
    `{"invocation": "scheduled"}` on both the 08:00 and 09:00 targets. Any
    other trigger - a bare `aws lambda invoke`, the console's "Test"
    button, a local dev call - has no such key and defaults to "manual".
    This is provenance disclosure, not a capture-success signal: a
    manually-invoked day still COUNTS as recorded (lock 1) and is never
    hidden, just labeled honestly in every manifest this invocation writes.
    """
    import os

    import boto3  # available in the Lambda runtime by default; see report

    bucket = os.environ.get("TALLY_BUCKET", DEFAULT_BUCKET)
    now = datetime.now(timezone.utc)
    invocation = event.get("invocation", "manual") if isinstance(event, dict) else "manual"

    s3_client = boto3.client("s3")
    results: list[CaptureResult] = []
    # Margin for the S3 head/put calls that follow a fetch, on top of the
    # fetch's own timeout budget.
    margin_ms = 5_000
    required_ms = (REQUEST_TIMEOUT_SECONDS * 1000) + margin_ms

    with httpx.Client() as http_client:
        for source in SOURCES:
            if _remaining_time_ms(context) < required_ms:
                _log_event(
                    logging.ERROR,
                    "insufficient_time",
                    source_key=source.key,
                    status="failed",
                    invocation=invocation,
                    required_ms=required_ms,
                    captured_at=now.isoformat(),
                )
                manifest = build_manifest(
                    source=source,
                    captured_at=now,
                    status="failed",
                    invocation=invocation,
                )
                manifest["error"] = "insufficient Lambda time remaining for this source"
                results.append(
                    CaptureResult(source_key=source.key, status="failed", manifest=manifest)
                )
                continue
            try:
                result = capture_one_source(
                    source=source,
                    http_client=http_client,
                    s3_client=s3_client,
                    bucket=bucket,
                    now=now,
                    invocation=invocation,
                )
            except Exception as exc:  # noqa: BLE001 - last-resort per-source isolation
                _log_event(
                    logging.ERROR,
                    "capture_failed",
                    source_key=source.key,
                    status="failed",
                    invocation=invocation,
                    error=str(exc),
                    captured_at=now.isoformat(),
                )
                result = CaptureResult(
                    source_key=source.key, status="failed", manifest={"error": str(exc)}
                )
            results.append(result)

    summary = {
        "event": "capture_run_summary",
        "captured_at": now.isoformat(),
        "results": [{"source_key": r.source_key, "status": r.status} for r in results],
    }
    logger.info(json.dumps(summary))

    # Phase 2 (Session 3): best-effort DB commit of today's day, entirely
    # downstream of phase 1's S3 capture above. Lock 4: this must never be
    # able to fail the invocation or affect phase 1's already-succeeded S3
    # results, so `_commit_today_to_db` catches everything internally and
    # this call site adds no try/except of its own beyond trusting that
    # contract - there is deliberately no code path here that could turn a
    # DB problem into a capture failure.
    db_commit = _commit_today_to_db(
        s3_client=s3_client, bucket=bucket, run_date=now, invocation=invocation
    )
    _log_event(
        logging.INFO if db_commit["status"] == "ok" else logging.WARNING,
        "db_commit_result",
        **db_commit,
    )

    return {
        "captured_at": summary["captured_at"],
        "results": summary["results"],
        "db_commit": db_commit,
    }

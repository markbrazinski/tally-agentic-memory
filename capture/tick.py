"""`make tick` — operator CLI: last 3 days' manifest status per source.

Dev-only operator tool, NOT Lambda code: it imports boto3 directly at module
scope (fine here — it never gets packaged into the Lambda zip; only
capture/handler.py is deployed, and that module imports boto3 lazily inside
lambda_handler for that exact reason).

Usage:
    AWS_PROFILE=example-profile python -m capture.tick
    # or, if AWS_PROFILE is already exported in your shell:
    python -m capture.tick

Respects:
    AWS_PROFILE   - which AWS CLI/boto3 profile to use (defaults to "tally"
                    if unset, so a bare `make tick` still hits the right
                    account without the operator having to remember to set it)
    TALLY_BUCKET  - bucket to read manifests from (defaults to "tally-demo-recordings",
                    matching capture/handler.py's DEFAULT_BUCKET)
    TALLY_CRDB_DSN - CockroachDB connection string (see src.external.db).
                    Bundle R Session 3 extends this tool to show DB
                    (`recordings` table) state alongside S3 manifest state.
                    Unset or unreachable DB is handled gracefully (see below).

Read-only: only ever calls S3 GetObject, CloudWatch Logs FilterLogEvents,
and read-only SQL SELECTs. Never writes anything.

S3-first, DB-second (Bundle R lock 4, "capture must never be coupled to
database availability"): this tool's own reason for existing is to make S3
state visible, and that must keep working even when the DB doesn't. So the
DB lookup is best-effort — any failure to reach the DB (DSN unset, cluster
unreachable, tenant not yet seeded, etc.) degrades to a "db unavailable"
note per row rather than aborting the whole command. An operator convenience
tool crashing because a downstream consumer (the DB) is down would invert
the whole point of raw-bytes-first design.

Soak-day section (Bundle R addendum #2, 2026-07-06): this is the tool that
answers "did today fire on its own, correctly?" in one command instead of a
manual CloudWatch Logs audit. Per the addendum's own diagnosis of the July 5
incident — "every signal being watched measured intent (schedule ENABLED,
Lambda Active, code committed), and none measured effect" — the missing
signal was always available in CloudWatch Logs, just never checked as part
of the daily ritual. Folded into `make tick` itself (not a second command)
so there is exactly one ritual, matching CLAUDE.md's "first minute of every
session is `make tick`" lock: two rituals is how one gets skipped. Same
graceful-degradation discipline as the DB lookup: CloudWatch Logs access is
best-effort and never aborts the command if unreachable.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from capture.handler import DEFAULT_BUCKET, build_manifest_key
from capture.sources import SOURCES
from capture.verifier import INVOCATION_CHECK_STARTS, verify_sources

DEFAULT_PROFILE = "tally"
NUM_DAYS = 3

NO_MANIFEST = "no manifest"

# db_status sentinels (capture/tick.py's own vocabulary, distinct from
# recording/commit.py's STATUS_COMMITTED/FAILED/SKIPPED strings, which are
# passed through as-is when a recordings row exists).
NO_DB_ROW = "no db row"
DB_UNAVAILABLE = "db unavailable"

# Soak-day vocabulary (Bundle R addendum #2, 2026-07-06). Distinct from
# db_status/s3_status sentinels above because a soak day's verdict is about
# the RUN, not any one source: "clean" only if verify_sources (the same
# function the verifier Lambda itself calls) says every source is
# STATUS_OK for that date.
SOAK_CLEAN = "clean"
SOAK_NOT_CLEAN = "not clean"
SOAK_LOGS_UNAVAILABLE = "logs unavailable"

LOG_GROUP_CAPTURE = "/aws/lambda/example-archive-worker"
LOG_GROUP_VERIFIER = "/aws/lambda/example-audit-worker"


def last_n_utc_dates(n: int, *, today: datetime | None = None) -> list[str]:
    """Return the last `n` UTC calendar dates as "YYYY-MM-DD" strings.

    Ordered newest-first (today, yesterday, day-before, ...). `today`
    defaults to datetime.now(timezone.utc); tests can inject a fixed value
    to stay clock-independent, matching the pattern in capture/handler.py.
    """
    anchor = today or datetime.now(timezone.utc)
    return [(anchor - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(n)]


def format_tick_table(rows: list[tuple[str, str, str]]) -> str:
    """Format (source_key, date, status) rows as a simple aligned table.

    Pure formatting logic, kept separate from the S3 fetch loop so it's
    unit-testable without mocking boto3.

    Kept as-is (3 columns, S3-only) for backward compatibility: existing
    callers/tests depend on this exact signature. See
    format_tick_table_with_db for the new 4-column S3+DB view.
    """
    header = ("source_key", "date", "status")
    all_rows = [header, *rows]
    widths = [max(len(row[i]) for row in all_rows) for i in range(3)]

    def fmt_row(row: tuple[str, str, str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt_row(header), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def format_tick_table_with_db(rows: list[tuple[str, str, str, str]]) -> str:
    """Format (source_key, date, s3_status, db_status) rows as an aligned table.

    New in Bundle R Session 3: shows S3 manifest status and CockroachDB
    `recordings` row status side by side, so an operator can see at a
    glance if S3 captured something but the DB commit didn't happen (the
    only direction that's architecturally possible — S3-then-DB is the
    only path per Bundle R lock 4; the reverse can't occur).

    A separate function rather than generalizing format_tick_table's
    signature: tests/unit/test_tick.py asserts the exact 3-tuple signature
    of format_tick_table, and this tool has exactly two callers (S3-only
    smoke-checks and the new S3+DB view) that each want a fixed, known
    column count - a variadic/generic version would need a runtime column
    count that neither caller actually wants, at the cost of every existing
    test (and any other future caller) needing to know or check the width
    of the tuple it's building. Keeping both is a few dozen duplicated
    lines in trade for zero migration risk to the existing, working
    3-column tool and its tests.
    """
    header = ("source_key", "date", "s3_status", "db_status")
    all_rows = [header, *rows]
    widths = [max(len(row[i]) for row in all_rows) for i in range(4)]

    def fmt_row(row: tuple[str, str, str, str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt_row(header), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def fetch_manifest_status(s3_client, bucket: str, manifest_key: str) -> str:
    """Fetch one manifest.json and return its "status" field.

    Returns NO_MANIFEST if the object doesn't exist (404/NoSuchKey). Any
    other error is re-raised so the caller can report it distinctly rather
    than silently mislabeling a real failure as "no manifest".
    """
    import json

    from botocore.exceptions import ClientError

    try:
        response = s3_client.get_object(Bucket=bucket, Key=manifest_key)
        body = response["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return NO_MANIFEST
        raise

    try:
        manifest = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return "malformed manifest"

    return str(manifest.get("status", "unknown"))


def source_key_from_s3_key(s3_key: str | None) -> str | None:
    """Recover a recordings row's source_key from its own s3_key column.

    recordings.s3_key (recording/commit.py's build_recording_row) is always
    either the manifest's body_key (COMMITTED: "raw/{source_key}/{date}/
    body.{ext}") or, when no body_key exists (FAILED/SKIPPED manifests),
    the manifest_key itself ("raw/{source_key}/{date}/manifest.json") - see
    capture/handler.build_manifest_key / build_object_prefix. Either shape
    has source_key as the second "/"-delimited segment, which makes this
    the one join key that survives every recording status, unlike
    carrier_id/lane (both NULL on FAILED rows per build_recording_row, so
    they can't be used to recover which *source* failed).

    Returns None if s3_key is missing or doesn't have the expected shape
    (defensive - a malformed/legacy row should not raise here, just fail to
    match, same "degrade gracefully" spirit as the rest of this tool).
    """
    if not s3_key:
        return None
    parts = s3_key.split("/")
    if len(parts) < 2 or parts[0] != "raw":
        return None
    return parts[1]


_FETCH_RECORDINGS_SQL = """
    SELECT run_date, status, s3_key
    FROM recordings
    WHERE tenant_id = %(tenant_id)s
      AND target = 'tariff'
      AND run_date = ANY(%(run_dates)s)
"""


def fetch_db_recording_statuses(
    conn, *, tenant_id: str, dates: list[str]
) -> dict[tuple[str, str], str]:
    """Query `recordings` for the given tenant/date range, keyed by (source_key, date).

    Returns a dict mapping (source_key, "YYYY-MM-DD") -> recordings.status
    (COMMITTED | FAILED | SKIPPED, recording/commit.py's own vocabulary,
    passed through unchanged). A day/source with no recordings row at all
    is simply absent from the dict - the caller renders NO_DB_ROW for those,
    matching fetch_manifest_status's NO_MANIFEST convention on the S3 side.

    Read-only: one SELECT, no writes. Tenant-scoped per this tool's own
    read (CLAUDE.md: every query is tenant-scoped, never implicit).
    """
    from datetime import date as date_type

    run_dates = [date_type.fromisoformat(d) for d in dates]
    with conn.cursor() as cur:
        cur.execute(_FETCH_RECORDINGS_SQL, {"tenant_id": tenant_id, "run_dates": run_dates})
        results = cur.fetchall()

    lookup: dict[tuple[str, str], str] = {}
    for run_date, status, s3_key in results:
        source_key = source_key_from_s3_key(s3_key)
        if source_key is None:
            continue
        date_str = run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date)
        lookup[(source_key, date_str)] = status
    return lookup


def _resolve_tenant_id() -> str:
    """Look up the demo tenant_id, seeding it if it doesn't exist yet.

    Reuses src.external.seed_demo_tenant.run_seed(), which is idempotent
    (per its own docstring: "safe to re-run... ON CONFLICT DO NOTHING") -
    this tool doesn't invent its own tenant lookup/creation logic.
    """
    from src.external.seed_demo_tenant import run_seed

    return run_seed()


def count_invocations_by_mode(log_messages: list[str]) -> dict[str, int]:
    """Tally `"invocation"` field values across a batch of structured log lines.

    Pure function: takes plain strings (already fetched from CloudWatch
    Logs by the caller), returns a plain dict - e.g. {"scheduled": 3,
    "manual": 1}.

    A structured log line is never the whole message on its own: Python's
    `logging` module (via `_log_event`/`logger.info(json.dumps(...))` in
    capture/handler.py and capture/verifier.py) prepends
    "[LEVEL]\\t<iso-timestamp>\\t<request-id>\\t" ahead of the JSON payload,
    and CloudWatch's own transport can add further framing - so a naive
    `json.loads(message)` on the full line always fails, even for a
    perfectly well-formed event. This function instead finds the first
    "{" in the message and attempts to parse from there onward - the JSON
    payload is always the trailing part of the line, by construction of
    every call site that emits one.

    Lines with no "{" at all (INIT_START/START/END/REPORT - the Lambda
    platform's own plain-text lines, expected noise in the stream), lines
    whose trailing "{...}" doesn't parse, or valid JSON without an
    "invocation" field (e.g. capture_run_summary, which doesn't carry the
    field itself) are silently skipped, not counted as an error: this is a
    best-effort tally over a noisy log stream, not a strict schema check.

    This is the direct answer to the Bundle R addendum #2 diagnosis:
    "every signal being watched measured intent... none measured effect."
    An `invocation` count of {"scheduled": N, "manual": 0} for today is
    the first time this pipeline's own tooling reads the EFFECT (what the
    logs actually say happened) rather than the intent (schedule ENABLED,
    Lambda Active).
    """
    counts: dict[str, int] = {}
    for message in log_messages:
        brace_index = message.find("{")
        if brace_index == -1:
            continue
        try:
            payload = json.loads(message[brace_index:])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        invocation = payload.get("invocation")
        if isinstance(invocation, str):
            counts[invocation] = counts.get(invocation, 0) + 1
    return counts


def _fetch_log_messages(
    logs_client: Any, *, log_group: str, start_time_ms: int, end_time_ms: int
) -> list[str]:
    """Fetch every log message in a CloudWatch Logs group within a time window.

    Paginates via nextToken (FilterLogEvents caps each page at 1MB/10k
    events - three Lambdas' worth of one day's structured logs is always
    far under that, but paginating defensively costs nothing). A missing
    log group (the Lambda has never been invoked at all, e.g. a fresh
    account) raises ResourceNotFoundException - the caller treats this the
    same as "logs unavailable", not a hard error, since "never invoked
    yet" is itself useful information for an operator to see, not a crash.
    """
    messages: list[str] = []
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
    }
    while True:
        response = logs_client.filter_log_events(**kwargs)
        messages.extend(event["message"] for event in response.get("events", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return messages


def try_fetch_invocation_counts_for_today(
    logs_client: Any, *, today: str
) -> tuple[dict[str, dict[str, int]] | None, str | None]:
    """Best-effort CloudWatch Logs invocation tally for both Lambdas, today only.

    Returns (counts_by_log_group, error_note) - same one-meaningful-value
    contract as try_fetch_db_statuses. `today` must be a "YYYY-MM-DD" UTC
    calendar-date string (mirrors every other server-set-clock discipline
    in this pipeline); the window checked is that full UTC calendar day,
    which comfortably covers 08:00/09:00/10:00 UTC's known schedule times
    with margin on both ends.

    Any failure (no AWS credentials, log group doesn't exist yet, network
    error, CloudWatch Logs throttling) degrades to "logs unavailable" for
    the caller to print as a note, exactly like the DB lookup's own
    degradation - an operator's first command of the session must never
    hard-crash because one of three data sources (S3, DB, logs) is
    temporarily unreachable.
    """
    from datetime import date as date_type

    try:
        day = date_type.fromisoformat(today)
        start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        counts_by_group: dict[str, dict[str, int]] = {}
        for log_group in (LOG_GROUP_CAPTURE, LOG_GROUP_VERIFIER):
            messages = _fetch_log_messages(
                logs_client, log_group=log_group, start_time_ms=start_ms, end_time_ms=end_ms
            )
            counts_by_group[log_group] = count_invocations_by_mode(messages)
        return counts_by_group, None
    except Exception as exc:  # noqa: BLE001 - see docstring: any logs failure degrades gracefully
        return None, f"{type(exc).__name__}: {exc}"


def soak_day_verdict(s3_client: Any, bucket: str, *, date: str) -> str:
    """Is `date` a clean, unattended soak day? Reuses the verifier's own logic.

    Delegates to capture.verifier.verify_sources - the exact function the
    10:00 UTC verifier Lambda calls - so `make tick` and the verifier can
    never silently disagree about what counts as clean. A day is
    SOAK_CLEAN only if every registered source's manifest is STATUS_OK
    per verify_sources, which itself already encodes the manual_only
    precedence rule (Bundle R addendum #2): missing, failed, AND
    manual-first-write days are all "not clean", pre-INVOCATION_CHECK_STARTS
    days are always exempt from the manual_only check.
    """
    _results, all_ok = verify_sources(s3_client, bucket, today=date)
    return SOAK_CLEAN if all_ok else SOAK_NOT_CLEAN


def format_soak_summary(
    soak_rows: list[tuple[str, str]],
    invocation_counts: dict[str, dict[str, int]] | None,
) -> str:
    """Format the soak-day section: per-date verdict + today's raw invocation counts.

    Pure formatting, kept separate from the fetch/verify calls so it's
    unit-testable without mocking boto3 (same split as
    format_tick_table_with_db vs. run()). `soak_rows` is newest-first
    (date, verdict) pairs; `invocation_counts` is None when CloudWatch
    Logs couldn't be reached (printed as "unavailable" per row instead).

    A date before INVOCATION_CHECK_STARTS gets an explicit annotation even
    when its verdict is SOAK_CLEAN: "clean" here means "no data-integrity
    problem, nothing to alarm on" (the verifier's own definition), which is
    NOT the same claim as "counts toward the unattended soak streak" - a
    pre-fix date is always disclosed history, never a contributor to the
    3-consecutive-day close criterion, regardless of its verdict. Spelling
    this out inline is what stops "clean" from being misread as "this was
    a good soak day" the way a bare table cell would invite.
    """
    lines = ["", "-- soak-day check (Bundle R addendum #2) --"]
    for date, verdict in soak_rows:
        if date < INVOCATION_CHECK_STARTS:
            lines.append(f"  {date}: {verdict} (pre-fix, does not count toward soak streak)")
        else:
            lines.append(f"  {date}: {verdict}")
    lines.append("")
    if invocation_counts is None:
        lines.append("  today's invocation counts: logs unavailable")
    else:
        for log_group, counts in invocation_counts.items():
            counts_str = ", ".join(f"{mode}={n}" for mode, n in sorted(counts.items())) or "(none)"
            lines.append(f"  {log_group}: {counts_str}")
    return "\n".join(lines)


def try_fetch_db_statuses(dates: list[str]) -> tuple[dict[tuple[str, str], str] | None, str | None]:
    """Best-effort DB lookup. Returns (lookup, error_note).

    Exactly one of the two return values is meaningful: on success,
    lookup is a dict (possibly empty) and error_note is None; on any
    failure, lookup is None and error_note describes what went wrong, in
    one line suitable for printing to the operator.

    Deliberately broad exception handling (contrast with
    fetch_manifest_status's narrow ClientError-only catch): this is the
    one graceful-degradation boundary in the whole tool, per Bundle R lock
    4's spirit ("capture code must never block on, or be coupled to,
    database availability") extended here to "S3 visibility must never be
    blocked by DB availability either". Anything from a missing driver
    import, an unset DSN (src.external.db.get_dsn raises RuntimeError), a
    DNS/connection failure, to an auth error should all fall back to the
    same "show S3 only" behavior rather than crash an operator's first
    command of the session.
    """
    try:
        from src.external.db import connect

        tenant_id = _resolve_tenant_id()
        with connect() as conn:
            lookup = fetch_db_recording_statuses(conn, tenant_id=tenant_id, dates=dates)
        return lookup, None
    except Exception as exc:  # noqa: BLE001 - see docstring: any DB failure degrades gracefully
        return None, f"{type(exc).__name__}: {exc}"


def run(*, bucket: str, profile: str | None, today: datetime | None = None) -> int:
    """Build and print the tick table. Returns a process exit code."""
    import boto3
    from botocore.exceptions import BotoCoreError, NoCredentialsError

    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        s3_client = session.client("s3")
    except (BotoCoreError, NoCredentialsError) as exc:
        print(f"error: could not create S3 client: {exc}", file=sys.stderr)
        return 1

    dates = last_n_utc_dates(NUM_DAYS, today=today)

    if not SOURCES:
        print("no registered sources")
        return 0

    db_lookup, db_error = try_fetch_db_statuses(dates)

    rows: list[tuple[str, str, str, str]] = []
    for source in SOURCES:
        for date in dates:
            manifest_key = build_manifest_key(source.key, date)
            try:
                s3_status = fetch_manifest_status(s3_client, bucket, manifest_key)
            except Exception as exc:  # noqa: BLE001 - never stack-trace at the operator
                s3_status = f"error: {exc}"

            if db_lookup is None:
                db_status = DB_UNAVAILABLE
            else:
                db_status = db_lookup.get((source.key, date), NO_DB_ROW)

            rows.append((source.key, date, s3_status, db_status))

    print(format_tick_table_with_db(rows))
    if db_error is not None:
        print(
            f"\nnote: DB state could not be checked ({db_error}); showing S3-only view. "
            "S3 is the durable source of truth (Bundle R lock 4) - this does not "
            "indicate a capture problem.",
            file=sys.stderr,
        )

    soak_rows = [(date, soak_day_verdict(s3_client, bucket, date=date)) for date in dates]
    logs_client = session.client("logs")
    invocation_counts, logs_error = try_fetch_invocation_counts_for_today(
        logs_client, today=dates[0]
    )
    print(format_soak_summary(soak_rows, invocation_counts))
    if logs_error is not None:
        print(
            f"\nnote: CloudWatch Logs could not be checked ({logs_error}); soak verdicts above "
            "are S3-only (still authoritative per verify_sources, just without today's raw "
            "invocation-count corroboration).",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    bucket = os.environ.get("TALLY_BUCKET", DEFAULT_BUCKET)
    profile = os.environ.get("AWS_PROFILE", DEFAULT_PROFILE)
    return run(bucket=bucket, profile=profile)


if __name__ == "__main__":
    sys.exit(main())

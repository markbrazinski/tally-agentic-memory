"""restore_live.py — Bundle R Session 3 replay entry point.

Per docs/bundle-r.md Session 3: "restore_live.py (replay: walk S3 from
July 4, commit each day with captured_at = the S3 server timestamp,
source='live')." This is the second of `recording/commit.py`'s two entry
points (the other is the daily Lambda phase-2 commit, going forward); this
one walks EVERY calendar day from the first real capture day (July 4, 2026)
through today and calls `recording.commit.commit_day` for each, so the
recordings table catches up to whatever S3 already holds.

Design (matches capture/tick.py's own script shape):
    - `replay_range` is the pure-ish core: given an already-open DB
      connection, an already-constructed S3 client, and a plain date
      range, it just loops and calls commit_day per day. No environment
      reads, no client construction, no "what is today" logic — that's
      all `main()`'s job, so tests can drive this function directly with
      injected fakes (see tests/unit/test_restore_live.py).
    - `main()` wraps it: reads TALLY_CRDB_DSN via src.external.db.get_dsn
      (indirectly, via connect()), constructs a real boto3 S3 client
      (default credential chain / AWS_PROFILE, same pattern as
      capture/tick.py and capture/handler.lambda_handler), resolves the
      tenant_id via src.external.seed_demo_tenant.run_seed() (idempotent
      — safe to call even though the tenant already exists; this keeps
      the script self-sufficient rather than hardcoding a UUID that would
      silently go stale if the tenant were ever recreated), loads the
      carrier map via recording.commit.load_carrier_id_by_scac, and
      determines the date range (July 4, 2026 through
      datetime.now(timezone.utc).date(), inclusive — never hardcode the
      end date).

Idempotency (test budget: "replay idempotency (run twice -> same rows)")
is inherited entirely from commit_day/commit_source_day's own ON CONFLICT
DO NOTHING behavior (already verified against the real cluster per the
foundation work in recording/commit.py) — this module adds no additional
state of its own, so running the full script twice is safe by
construction. tests/unit/test_restore_live.py still proves this
end-to-end (full script-level replay_range run twice -> identical row
counts) using mocked S3+DB fakes, per the task's own test budget item.

A day with no manifest for any of the 3 sources (a genuine gap, or a
future date that hasn't happened yet) produces an empty result list for
that date via commit_day's own contract — no extra logic is needed here
to special-case it.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import Connection

from recording.commit import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    CommitResult,
    commit_day,
    load_carrier_id_by_scac,
)
from src.external.db import connect

# The actual first capture day, per docs/bundle-r.md's own history:
# "manual invoke this afternoon = day 1 = July 4" (Session 1, 2026-07-04).
# Not derived from anything dynamic — this is a fixed historical fact about
# when the capture pipeline was born, same spirit as recording/commit.py's
# own fixed SOURCE_KEY_TO_SCAC mapping.
FIRST_CAPTURE_DATE = date_type(2026, 7, 4)

DEFAULT_BUCKET = "tally-demo-recordings"


def daterange(start_date: date_type, end_date: date_type) -> list[date_type]:
    """Every calendar date from start_date through end_date, inclusive.

    Pure function, no I/O. Returns an empty list if start_date > end_date
    (defensive — shouldn't happen in normal use since end_date is always
    "today" or later, but a caller-supplied --end-date override could get
    this wrong, and an empty walk is a cleaner failure mode than a
    confusing negative-range crash).
    """
    if start_date > end_date:
        return []
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def replay_range(
    conn: Connection,
    s3_client: Any,
    *,
    tenant_id: str,
    bucket: str,
    carrier_id_by_scac: dict[str, str],
    start_date: date_type,
    end_date: date_type,
) -> list[CommitResult]:
    """Core replay loop: commit every day in [start_date, end_date] to the DB.

    Pure-ish: takes an already-open connection and an already-constructed
    S3 client, so tests can inject fakes without touching real
    environment/credentials. Delegates all per-day logic to
    recording.commit.commit_day, which already handles: a day with no
    manifest for any source produces no CommitResults (nothing to
    replay); a day with a manifest for only some sources produces results
    for exactly those; idempotent re-commits are a no-op via ON CONFLICT
    DO NOTHING.

    Returns the flat list of every CommitResult across every day walked,
    in date order (oldest first) — callers that want a summary (as
    `main()` does) tally this list themselves rather than this function
    printing anything, keeping it silent and testable.
    """
    results: list[CommitResult] = []
    for run_date in daterange(start_date, end_date):
        day_results = commit_day(
            conn,
            s3_client,
            tenant_id=tenant_id,
            bucket=bucket,
            run_date=run_date,
            carrier_id_by_scac=carrier_id_by_scac,
        )
        results.extend(day_results)
    return results


def _print_day_line(run_date: date_type, day_results: list[CommitResult]) -> None:
    """Print one line per calendar day walked, as replay progresses.

    `day_results` is empty for a genuinely-missing day (no manifest for any
    source) — reported as "no manifests" rather than 0/0/0, so a future
    date or a real gap in S3 reads honestly instead of looking like a
    silent zero-source success.
    """
    if not day_results:
        print(f"{run_date.isoformat()}: no manifests (nothing to replay)")
        return
    by_status = Counter(r.recording_status for r in day_results)
    print(
        f"{run_date.isoformat()}: "
        f"COMMITTED={by_status.get(STATUS_COMMITTED, 0)} "
        f"FAILED={by_status.get(STATUS_FAILED, 0)} "
        f"SKIPPED={by_status.get(STATUS_SKIPPED, 0)} "
        f"(sources seen: {len(day_results)})"
    )


def _print_summary(results: list[CommitResult], start_date: date_type, end_date: date_type) -> None:
    """Print final totals across the whole replay range."""
    by_status = Counter(r.recording_status for r in results)
    print(f"--- replay summary: {start_date.isoformat()} .. {end_date.isoformat()} ---")
    print(f"total source-day commit attempts: {len(results)}")
    print(f"  COMMITTED: {by_status.get(STATUS_COMMITTED, 0)}")
    print(f"  FAILED:    {by_status.get(STATUS_FAILED, 0)}")
    print(f"  SKIPPED:   {by_status.get(STATUS_SKIPPED, 0)}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay S3 capture manifests into committed CockroachDB recordings/"
            "tariff_snapshots rows, from the first capture day through today."
        )
    )
    parser.add_argument(
        "--start-date",
        type=date_type.fromisoformat,
        default=None,
        help=(
            "Override the replay start date (YYYY-MM-DD). Defaults to the real "
            f"first capture day, {FIRST_CAPTURE_DATE.isoformat()}. Useful for "
            "testing/debugging a narrower range without waiting on real S3 history."
        ),
    )
    parser.add_argument(
        "--end-date",
        type=date_type.fromisoformat,
        default=None,
        help=(
            "Override the replay end date (YYYY-MM-DD), inclusive. Defaults to "
            "today (UTC)."
        ),
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=f"S3 bucket to read manifests from (default: {DEFAULT_BUCKET}).",
    )
    parser.add_argument(
        "--dsn-env-var",
        default=None,
        help=(
            "Read an explicit target DSN from this environment variable. "
            "This is intended for isolated recovery replays and avoids placing "
            "credentials in process arguments."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = _parse_args(argv)

    start_date = args.start_date or FIRST_CAPTURE_DATE
    end_date = args.end_date or datetime.now(timezone.utc).date()
    dsn = None
    if args.dsn_env_var:
        dsn = os.environ.get(args.dsn_env_var)
        if not dsn:
            raise RuntimeError(
                f"replay DSN environment variable {args.dsn_env_var!r} is not set"
            )

    from src.external.seed_demo_tenant import run_seed

    tenant_id = run_seed(dsn=dsn)

    conn = connect(dsn)
    try:
        carrier_id_by_scac = load_carrier_id_by_scac(conn, tenant_id)
        s3_client = boto3.client("s3")

        print(f"tenant_id: {tenant_id}")
        results: list[CommitResult] = []
        # Walk one day at a time (rather than one replay_range call for the
        # whole span) purely so progress can be printed as it goes, per the
        # task spec's "print/log a summary as it goes: which date, how many
        # sources committed...". replay_range itself stays span-agnostic and
        # silent so tests can call it with any range in one shot.
        for run_date in daterange(start_date, end_date):
            day_results = replay_range(
                conn,
                s3_client,
                tenant_id=tenant_id,
                bucket=args.bucket,
                carrier_id_by_scac=carrier_id_by_scac,
                start_date=run_date,
                end_date=run_date,
            )
            _print_day_line(run_date, day_results)
            results.extend(day_results)
    finally:
        conn.close()

    _print_summary(results, start_date, end_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())

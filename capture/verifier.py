"""Verifier Lambda for Tally's Bundle R, Session 2.

docs/bundle-r.md Session 2 ("Unattended & Honest"): "Replace the crude alarm
with the real shape: a scheduled verifier Lambda at 10:00 UTC that checks
'does today's manifest exist for every registered source?' and alarms
ExampleRecoveryGap if not — this catches the failure mode where EventBridge
itself misfires, which a Lambda-error alarm cannot see."

This module is deliberately a READ + REPORT job, not a write job (per session
scope): for each registered source, it checks whether today's manifest exists
in S3 and, if it does, reads its `status` field. It never writes to S3 (the
capture Lambda in capture/handler.py owns all S3 writes). Its one write of
any kind is a CloudWatch `PutMetricData` call reporting whether today counts
as fully captured.

Division of responsibility (detect-and-report vs. alarm-and-notify):
    - This Lambda's job: decide, per source, "ok" | "failed" | "missing",
      and emit one CloudWatch metric datapoint (`ExampleRecoveryGap`, 0 or 1,
      namespace `ExampleTally/Capture`) summarizing the day.
    - CloudWatch Alarm + SNS topic (created via deploy_verifier.sh, NOT by
      this Lambda at runtime) watch that metric and fire the actual email
      notification. This mirrors how the S3 bucket and IAM execution role
      are created by IaC/CLI outside application code, not by handler.py.

Design for testability (same discipline as capture/handler.py):
    - `verify_sources` is pure with respect to I/O: it takes an injected S3
      client and a `today` string, and returns a plain dict — no boto3
      CloudWatch call happens inside it.
    - `lambda_handler` is the thin AWS entrypoint: constructs the real S3 and
      CloudWatch clients, calls `verify_sources`, emits the metric, and
      returns the summary dict.

_log_event reuse: this module imports `_log_event` directly from
capture.handler rather than duplicating it. Both modules are part of the
same `capture` package (sibling files, not a cross-package boundary), the
helper is pure and side-effect-free (json.dumps + logger.log, no shared
mutable state), and the whole point of Session 2's "polish absorbed"
groundwork is that every call site in this pipeline emits the SAME
structured JSON shape so a future metric filter can grep one pattern across
both Lambdas. Duplicating it would let the two shapes drift silently.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from capture.handler import DEFAULT_BUCKET, _log_event, build_manifest_key, manifest_exists
from capture.sources import SOURCES

logger = logging.getLogger("tally.verifier")
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "ExampleTally/Capture"
METRIC_NAME = "ExampleRecoveryGap"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"
STATUS_MANUAL_ONLY = "manual_only"

INVOCATION_SCHEDULED = "scheduled"

# First UTC calendar date the "invocation" field can be trusted to mean
# anything (Bundle R addendum, 2026-07-05: schedules were created
# 02:19:57Z that day, but Target.Input wasn't genuinely live until
# ~11:55 UTC that same day, per deploy.sh's read-back assertion added for
# exactly this incident). Manifests dated before this are legitimately
# manual (real capture, no schedule had fired yet) or predate the field
# entirely (scripts/annotate_manual_invocations.py's retroactive patch) -
# neither is an alarm condition. Only this date onward is far enough past
# the fix that a manual-only day is worth flagging as a soak-day risk.
INVOCATION_CHECK_STARTS = "2026-07-06"


@dataclass(frozen=True)
class SourceVerification:
    """Outcome of checking one source's manifest for one UTC calendar date."""

    source_key: str
    manifest_status: str  # "ok" | "failed" | "missing" | "manual_only"


def _read_manifest(s3_client: Any, bucket: str, manifest_key: str) -> dict[str, Any] | None:
    """Return a manifest's parsed JSON body, or None if it doesn't exist.

    Uses the same narrow-ClientError discipline as capture.handler and
    capture.tick: `manifest_exists` (imported, not reimplemented) does the
    existence check via head_object; only if it confirms existence do we
    pay for a get_object to read the body. Any error other than a confirmed
    404/NoSuchKey propagates rather than being silently treated as
    "missing" — a transient S3 error must not be misreported as a missed
    day (or, just as bad, silently reported as an "ok" day).

    Returns an empty dict (not None) if the object exists but is
    unparseable — distinct from "doesn't exist" so callers can still tell
    "landed but untrustworthy" from "never landed" (capture_failed vs
    EventBridge-never-fired).
    """
    if not manifest_exists(s3_client, bucket, manifest_key):
        return None

    response = s3_client.get_object(Bucket=bucket, Key=manifest_key)
    try:
        body = response["Body"].read()
        manifest = json.loads(body)
        return manifest if isinstance(manifest, dict) else {}
    except (ValueError, AttributeError, KeyError, TypeError):
        return {}


def _resolve_manifest_status(manifest: dict[str, Any] | None, *, today: str) -> str:
    """Turn a manifest body into one of the four verifier status strings.

    A manifest that exists but has no usable "status" field is treated as
    STATUS_FAILED — it landed, but it isn't trustworthy evidence of a
    successful capture, so it must not read as "missing" (which would
    misname the failure mode: EventBridge/the capture Lambda DID run) nor
    as "ok" (there's nothing here to trust).

    STATUS_MANUAL_ONLY (Bundle R addendum #2, this session): a status="ok"
    manifest whose FIRST write was a manual invoke, on a date on/after
    INVOCATION_CHECK_STARTS. This is the exact failure mode that let July
    5 look clean in every signal except log content: the schedule fires
    (unattended, real) but finds the day already claimed by an earlier
    manual run, so it no-ops and the day never actually gets unattended
    evidence written. Idempotency (capture_one_source's manifest_exists
    check) means whichever invocation writes first "owns" the manifest's
    invocation field for that day — a later manual re-invoke after a clean
    scheduled capture cannot flip this back to manual_only, because it
    hits the retry no-op path and never rewrites the manifest. So reading
    the manifest's own invocation field IS reading "who wrote first",
    with no separate bookkeeping needed.
    """
    if manifest is None:
        return STATUS_MISSING

    status = manifest.get("status")
    if not isinstance(status, str):
        return STATUS_FAILED
    if status != STATUS_OK:
        return status

    invocation = manifest.get("invocation")
    if today >= INVOCATION_CHECK_STARTS and invocation != INVOCATION_SCHEDULED:
        return STATUS_MANUAL_ONLY
    return STATUS_OK


def verify_sources(
    s3_client: Any, bucket: str, *, today: str
) -> tuple[list[SourceVerification], bool]:
    """Check every registered source's manifest for `today`.

    `today` must already be a "YYYY-MM-DD" UTC calendar-date string (server-
    set clock, caller's responsibility — mirrors capture.handler's lock 2
    discipline). Pure with respect to I/O beyond the injected `s3_client`,
    so tests can pass in the same FakeS3Client style used for the capture
    handler's tests.

    Returns (results, all_ok). all_ok is True only if every source's
    manifest exists, its status is "ok", AND (on/after
    INVOCATION_CHECK_STARTS) it was first written by a scheduled
    invocation — a "failed" or "manual_only" manifest is just as much a
    soak-day miss as no manifest at all.
    """
    results: list[SourceVerification] = []
    for source in SOURCES:
        manifest_key = build_manifest_key(source.key, today)
        manifest = _read_manifest(s3_client, bucket, manifest_key)
        status = _resolve_manifest_status(manifest, today=today)
        results.append(SourceVerification(source_key=source.key, manifest_status=status))

    all_ok = all(r.manifest_status == STATUS_OK for r in results)
    return results, all_ok


def build_summary(results: list[SourceVerification], all_ok: bool, checked_at: datetime) -> dict:
    """Assemble the handler's return-value dict. Pure formatting."""
    missing_or_failed = [r.source_key for r in results if r.manifest_status != STATUS_OK]
    return {
        "checked_at": checked_at.isoformat(),
        "all_ok": all_ok,
        "missing_or_failed": missing_or_failed,
        "results": [
            {"source_key": r.source_key, "manifest_status": r.manifest_status} for r in results
        ],
    }


def _put_snapshot_missed_day_metric(cloudwatch_client: Any, *, all_ok: bool) -> None:
    """Emit one CloudWatch metric datapoint: 1 if any source missed/failed, else 0.

    This is the Lambda's entire "alarm" responsibility — it reports a
    number. The CloudWatch Alarm that watches this metric (threshold >= 1)
    and the SNS topic it notifies are created by deploy_verifier.sh, not
    here (see module docstring: detect-and-report vs. alarm-and-notify).
    """
    cloudwatch_client.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": METRIC_NAME,
                "Value": 0.0 if all_ok else 1.0,
                "Unit": "Count",
            }
        ],
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint for the 10:00 UTC verifier.

    Constructs the real S3 and CloudWatch clients here (the only place this
    module does so, mirroring capture.handler.lambda_handler) and delegates
    the actual check to `verify_sources`, so tests exercise that function
    directly with injected fakes.

    `event["invocation"]` disclosure (Bundle R addendum, mirrors
    capture.handler.lambda_handler): deploy_verifier.sh configures the
    10:00 UTC schedule's Target.Input as `{"invocation": "scheduled"}`; any
    other trigger defaults to "manual". Logged (not written to any
    manifest - the verifier never writes to S3) so a same-day CloudWatch
    Logs check can distinguish "the safety net fired on its own schedule"
    from "a human invoked it by hand" - the exact gap flagged in the
    Bundle R addendum: this verifier is the alarm for missed unattended
    days, so its OWN unattended operation must be independently provable,
    not just assumed from the schedule existing.
    """
    import os

    import boto3  # available in the Lambda runtime by default; lazy per capture.handler's pattern

    bucket = os.environ.get("TALLY_BUCKET", DEFAULT_BUCKET)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    invocation = event.get("invocation", "manual") if isinstance(event, dict) else "manual"

    s3_client = boto3.client("s3")
    results, all_ok = verify_sources(s3_client, bucket, today=today)
    summary = build_summary(results, all_ok, now)

    _log_event(
        logging.INFO if all_ok else logging.WARNING,
        "verification_result",
        checked_at=summary["checked_at"],
        all_ok=all_ok,
        invocation=invocation,
        missing_or_failed=summary["missing_or_failed"],
        results=summary["results"],
    )

    cloudwatch_client = boto3.client("cloudwatch")
    _put_snapshot_missed_day_metric(cloudwatch_client, all_ok=all_ok)

    return {**summary, "invocation": invocation}

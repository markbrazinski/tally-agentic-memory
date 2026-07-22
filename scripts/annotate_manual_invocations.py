"""One-off retroactive annotation: mark pre-addendum manifests as manual.

Bundle R addendum (2026-07-05): every manifest must disclose whether it
was produced by a scheduled (unattended) or manual (human-invoked) Lambda
run. The July 4-5, 2026 manifests predate this field entirely - they were
written before capture/handler.py's build_manifest gained the "invocation"
key, and before the EventBridge schedules that make "scheduled" possible
even existed. Every one of them was, in fact, a manual invoke run from a
local shell during development.

This script is a deliberate, one-time historical correction, not a new
capture: it does NOT re-fetch anything from the live carrier sites, does
NOT touch body.{ext} objects, and does NOT change status/sha256/
byte_count/captured_at - only ADDS the "invocation": "manual" key to each
existing manifest if it's absent. This is disclosure, never deletion or
fabrication (raw-bytes-first / lock 1: "we disclose the invocation mode,
we don't delete history").

Deliberately bypasses the normal IfNoneMatch="*" conditional write (which
exists to prevent a NEW day's capture from silently overwriting a prior
one) since this is an explicit, reviewed, one-time metadata patch of
already-known objects, run once and not part of any Lambda's normal path.

Usage:
    python3 scripts/annotate_manual_invocations.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

from capture.sources import SOURCES

BUCKET = "tally-demo-recordings"
KNOWN_MANUAL_DATES = ("2026-07-04", "2026-07-05")


def annotate(*, dry_run: bool) -> int:
    s3 = boto3.client("s3")
    patched = 0
    already_present = 0
    missing = 0

    for source in SOURCES:
        for date_str in KNOWN_MANUAL_DATES:
            key = f"raw/{source.key}/{date_str}/manifest.json"
            try:
                response = s3.get_object(Bucket=BUCKET, Key=key)
            except s3.exceptions.NoSuchKey:
                missing += 1
                print(f"skip (no manifest): {key}")
                continue

            body = response["Body"].read()
            manifest = json.loads(body)

            if "invocation" in manifest:
                already_present += 1
                print(f"skip (already annotated, invocation={manifest['invocation']!r}): {key}")
                continue

            manifest["invocation"] = "manual"
            print(f"{'[dry-run] would patch' if dry_run else 'patching'}: {key}")

            if not dry_run:
                s3.put_object(
                    Bucket=BUCKET,
                    Key=key,
                    Body=json.dumps(manifest, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
            patched += 1

    print(
        f"\nSummary: patched={patched} already_present={already_present} missing={missing}"
        + (" (dry-run, nothing written)" if dry_run else "")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing anything."
    )
    args = parser.parse_args()
    return annotate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

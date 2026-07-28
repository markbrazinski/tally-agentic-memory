"""OAuth refresh canary — scheduled Lambda that keeps the Managed MCP OAuth
bundle perpetually fresh.

CockroachDB's OAuth refresh token ROTATES on every refresh and has an idle
expiry: if it is never used within that window it lapses, and only an
interactive browser re-mint can recover it. This canary fires on an EventBridge
schedule (well inside both the access-token and refresh-token idle windows),
performs one refresh through the SAME OAuthTokenManager the app uses, and writes
the rotated bundle back to SSM — resetting the idle clock so it never lapses.

It cannot resurrect an already-dead token; it keeps a live one alive. Seed once
via the interactive bootstrap, then this holds it.

Public-safe: logs only the rotation flag and the new access-token expiry (an
epoch int) — never token material. Fails loud (raises) so a failed fire surfaces
in CloudWatch and the EventBridge failure metric, rather than silently letting
the token drift toward expiry.
"""

from __future__ import annotations

import logging
import os

import boto3

from src.external.oauth_tokens import (
    DynamoDBRefreshLease,
    OAuthTokenManager,
    SSMTokenStore,
)

_log = logging.getLogger("tally.oauth_canary")
_log.setLevel(logging.INFO)


def _build_manager() -> OAuthTokenManager:
    region = os.environ.get("AWS_REGION", "us-east-1")
    parameter_name = os.environ["TALLY_OAUTH_TOKEN_PARAMETER"]
    lease_table = os.environ["TALLY_OAUTH_REFRESH_LEASE_TABLE"]
    store = SSMTokenStore(
        boto3.client("ssm", region_name=region), parameter_name=parameter_name
    )
    lease = DynamoDBRefreshLease(
        boto3.client("dynamodb", region_name=region),
        table_name=lease_table,
        bundle_key=parameter_name,
    )
    return OAuthTokenManager(store, refresh_lease=lease)


def handler(event=None, context=None) -> dict:
    """EventBridge entrypoint. Forces one refresh; returns a public-safe summary."""
    manager = _build_manager()
    bundle, rotated = manager.refresh()
    summary = {
        "ok": True,
        "rotated": bool(rotated),
        "access_token_expires_at": int(bundle.expires_at),
    }
    _log.info(
        "oauth canary refreshed: rotated=%s expires_at=%s",
        summary["rotated"], summary["access_token_expires_at"],
    )
    return summary

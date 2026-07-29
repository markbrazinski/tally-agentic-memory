"""Isolated App Runner background loop for Intake tasks and event relay."""

from __future__ import annotations

import os
from uuid import uuid4

import boto3

from src.external.cockroach_mcp import CockroachManagedMCP, ManagedMCPConfig
from src.external.dal import DAL, Tenant
from src.external.invoice_source_store import VersionedInvoiceSourceStore
from src.platform.access_evidence_worker import run_one_access_evidence_task
from src.platform.applicable_rule_worker import run_one_rule_task
from src.platform.intake_events import relay_outbox_batch
from src.platform.intake_worker import run_one_intake_task
from src.platform.judgment_worker import run_one_judgment_task
from src.platform.reconstruction_worker import run_one_reconstruction_task

# SSM SecureString holding a CockroachDB Cloud service-account API key — a
# long-lived (non-expiring) machine credential for the Managed MCP. When present,
# it is used INSTEAD of the rotating user-OAuth token, so the MCP path can never
# lapse on idle. Same Bearer transport; the MCP server accepts either token shape.
MCP_API_KEY_PARAMETER = "/tally/gate5/mcp-api-key"


def _read_mcp_api_key(region: str) -> str | None:
    """Return the service-account API key from SSM, or None if not configured."""
    try:
        resp = boto3.client("ssm", region_name=region).get_parameter(
            Name=MCP_API_KEY_PARAMETER, WithDecryption=True
        )
        value = (resp["Parameter"]["Value"] or "").strip()
        return value or None
    except Exception:  # noqa: BLE001 — absent/unreadable key → fall back to OAuth
        return None


def _reconstruction_mcp_factory():
    """Build a fresh read-only Managed MCP client.

    Prefers a long-lived service-account API key (SSM) as the bearer credential —
    it never expires, so the MCP path survives idle periods and unattended judge
    use. Falls back to the rotating OAuth token bundle only when the API key is
    not configured. Imported lazily so the intake-only runtime needs no OAuth
    wiring until a reconstruction task is actually leased. (A separate DRIVER
    fallback in reconstruction_worker still guarantees reconstruction can't stall
    even if the MCP itself is unreachable.)
    """

    def _factory() -> CockroachManagedMCP:
        region = os.environ.get("AWS_REGION", "us-east-1")
        api_key = _read_mcp_api_key(region)
        if api_key is not None:
            config = ManagedMCPConfig.from_env(access_token=api_key)
            return CockroachManagedMCP(config)

        # No API key yet — use the OAuth bundle (SSM token store + DynamoDB
        # refresh lease), same wiring as app.py:_get_oauth_manager.
        from src.external.oauth_tokens import (
            DynamoDBRefreshLease,
            OAuthTokenManager,
            SSMTokenStore,
        )

        parameter_name = os.environ["TALLY_OAUTH_TOKEN_PARAMETER"]
        lease_table = os.environ["TALLY_OAUTH_REFRESH_LEASE_TABLE"]
        store = SSMTokenStore(
            boto3.client("ssm", region_name=region),
            parameter_name=parameter_name,
        )
        lease = DynamoDBRefreshLease(
            boto3.client("dynamodb", region_name=region),
            table_name=lease_table,
            bundle_key=parameter_name,
        )
        with OAuthTokenManager(store, refresh_lease=lease) as manager:
            access_token, _refreshed = manager.access_token()
        config = ManagedMCPConfig.from_env(access_token=access_token)
        return CockroachManagedMCP(config)

    return _factory


def run_runtime_iteration() -> bool:
    tenant_id = os.environ["TALLY_TENANT_ID"]
    bucket = os.environ["TALLY_INTAKE_BUCKET"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    worker_id = f"intake-{uuid4()}"
    store = VersionedInvoiceSourceStore(
        boto3.client("s3", region_name=region),
        bucket=bucket,
        key_prefix=os.environ.get("TALLY_INTAKE_KEY_PREFIX", "intake/invoice-sources"),
    )
    with DAL.connect(Tenant(tenant_id, worker_id)) as dal:
        completion = run_one_intake_task(
            dal,
            worker_id=worker_id,
            source_store=store,
        )
        reconstruction = run_one_reconstruction_task(
            dal,
            worker_id=worker_id,
            mcp_factory=_reconstruction_mcp_factory(),
        )
        binding = run_one_access_evidence_task(dal, worker_id=worker_id)
        rule = run_one_rule_task(dal, worker_id=worker_id)
        judgment = run_one_judgment_task(dal, worker_id=worker_id)
        delivered = relay_outbox_batch(dal)
    return (
        completion is not None
        or reconstruction is not None
        or binding is not None
        or rule is not None
        or judgment is not None
        or delivered > 0
    )

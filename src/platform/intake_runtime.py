"""Isolated App Runner background loop for Intake tasks and event relay."""

from __future__ import annotations

import os
from uuid import uuid4

import boto3

from src.external.cockroach_mcp import CockroachManagedMCP, ManagedMCPConfig
from src.external.dal import DAL, Tenant
from src.external.invoice_source_store import VersionedInvoiceSourceStore
from src.platform.applicable_rule_worker import run_one_rule_task
from src.platform.intake_events import relay_outbox_batch
from src.platform.intake_worker import run_one_intake_task
from src.platform.judgment_worker import run_one_judgment_task
from src.platform.reconstruction_worker import run_one_reconstruction_task


def _reconstruction_mcp_factory():
    """Build a fresh read-only Managed MCP client from OAuth-manager state.

    Imported lazily so the intake-only runtime never requires OAuth wiring until
    a reconstruction task is actually leased. No direct-DB fallback is possible:
    the factory returns only a Managed MCP client.
    """
    from src.external.oauth_tokens import OAuthTokenManager

    def _factory() -> CockroachManagedMCP:
        with OAuthTokenManager.from_env() as manager:
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
        rule = run_one_rule_task(dal, worker_id=worker_id)
        judgment = run_one_judgment_task(dal, worker_id=worker_id)
        delivered = relay_outbox_batch(dal)
    return (
        completion is not None
        or reconstruction is not None
        or rule is not None
        or judgment is not None
        or delivered > 0
    )

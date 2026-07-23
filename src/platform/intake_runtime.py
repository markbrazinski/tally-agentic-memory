"""Isolated App Runner background loop for Intake tasks and event relay."""

from __future__ import annotations

import os
from uuid import uuid4

import boto3

from src.external.dal import DAL, Tenant
from src.external.invoice_source_store import VersionedInvoiceSourceStore
from src.platform.intake_events import relay_outbox_batch
from src.platform.intake_worker import run_one_intake_task


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
        delivered = relay_outbox_batch(dal)
    return completion is not None or delivered > 0

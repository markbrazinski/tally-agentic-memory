-- Authority transition — durable access-evidence verification record (additive).
--
-- The gap-driven BIND_ACCESS_EVIDENCE task verifies a retained-but-PENDING
-- per-day terminal-access snapshot against its exact source, then (only on pass)
-- projects shipment_event_memory.source_version_state PENDING -> VERIFIED. Delta
-- guardrail: the verification attempt + result must be recorded DURABLY BEFORE
-- the state transition, so every binding (and every refusal) is auditable and
-- the bind is idempotent. This table is that record. It never stores raw S3
-- locators — only public refs, the verdict, and a reason code.
--
-- The snapshot's own row in shipment_event_memory is otherwise immutable: this
-- flow changes only source_version_state, never the payload/timestamps/refs.

CREATE TABLE IF NOT EXISTS access_evidence_verifications (
  tenant_id            UUID NOT NULL REFERENCES tenants(id),
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id           UUID NOT NULL,
  task_id              UUID NOT NULL,
  snapshot_public_ref  STRING NOT NULL,
  container_ref        STRING NOT NULL,
  snapshot_date        DATE NOT NULL,
  outcome              STRING NOT NULL,          -- VERIFIED | REFUSED
  reason_code          STRING,                   -- NULL on VERIFIED; else failure code
  attempted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT access_verif_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  -- One durable verdict per (task, snapshot): idempotent re-runs of the same
  -- task do not create a second record or a second binding.
  UNIQUE INDEX access_verif_task_snapshot_idx
    (tenant_id, task_id, snapshot_public_ref)
);

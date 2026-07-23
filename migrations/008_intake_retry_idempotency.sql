-- Idempotent operator retry requests for immutable Intake task history.

CREATE TABLE IF NOT EXISTS workflow_retry_requests (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  idempotency_key    STRING NOT NULL,
  invoice_id         UUID NOT NULL,
  task_type          STRING NOT NULL,
  request_hash       STRING NOT NULL,
  state              STRING NOT NULL,
  response_snapshot  JSONB,
  initiated_by       UUID,
  actor_display      STRING NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, idempotency_key),
  CONSTRAINT workflow_retry_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id)
);

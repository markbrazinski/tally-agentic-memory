-- Locked-demo Intake/Orchestration foundation.
--
-- This migration is additive. Legacy Clerk/receipt rows remain readable while
-- the new Invoice aggregate gains a durable intake state, exact source
-- bindings, idempotent ingestion, leased tasks, immutable claim versions,
-- monotonic public events, and a transactional outbox.

ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS display_name STRING;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS intake_state STRING;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS aggregate_status STRING;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS status_sequence INT8 NOT NULL DEFAULT 0;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS active_claim_set_version INT8;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS row_version INT8 NOT NULL DEFAULT 0;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS invoice_sources (
  tenant_id                    UUID NOT NULL REFERENCES tenants(id),
  id                           UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id                   UUID NOT NULL,
  source_type                  STRING NOT NULL,
  display_filename             STRING NOT NULL,
  mime_type                    STRING NOT NULL,
  byte_length                  INT8 NOT NULL,
  sha256                       STRING NOT NULL,
  s3_bucket_ref_private        STRING NOT NULL,
  s3_object_key_private        STRING NOT NULL,
  s3_version_id_private        STRING NOT NULL,
  preservation_status          STRING NOT NULL,
  provenance_classification    STRING NOT NULL,
  public_disclosure            STRING NOT NULL,
  verified_at                  TIMESTAMPTZ NOT NULL,
  received_at                  TIMESTAMPTZ NOT NULL,
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT invoice_sources_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  UNIQUE INDEX invoice_sources_primary_idx
    (tenant_id, invoice_id, source_type),
  UNIQUE INDEX invoice_sources_s3_version_idx
    (tenant_id, s3_object_key_private, s3_version_id_private)
);

CREATE TABLE IF NOT EXISTS ingestion_requests (
  tenant_id              UUID NOT NULL REFERENCES tenants(id),
  idempotency_key        STRING NOT NULL,
  request_hash           STRING NOT NULL,
  state                  STRING NOT NULL,
  initiated_by           UUID,
  actor_display          STRING NOT NULL,
  reserved_invoice_id    UUID NOT NULL,
  reserved_source_id     UUID NOT NULL,
  s3_bucket_ref_private  STRING,
  s3_object_key_private  STRING,
  s3_version_id_private  STRING,
  source_sha256          STRING,
  source_byte_length     INT8,
  response_snapshot      JSONB,
  lease_owner            STRING,
  lease_expires_at       TIMESTAMPTZ,
  last_error_code        STRING,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, idempotency_key),
  UNIQUE INDEX ingestion_request_invoice_idx (tenant_id, reserved_invoice_id),
  UNIQUE INDEX ingestion_request_source_idx (tenant_id, reserved_source_id)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
  tenant_id                    UUID NOT NULL REFERENCES tenants(id),
  id                           UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id                   UUID NOT NULL,
  source_id                    UUID NOT NULL,
  source_sha256                STRING NOT NULL,
  source_version_ref_private   STRING NOT NULL,
  model_id                     STRING NOT NULL,
  schema_version               STRING NOT NULL,
  template_version             STRING NOT NULL,
  attempt                      INT8 NOT NULL,
  requested_at                 TIMESTAMPTZ NOT NULL,
  responded_at                 TIMESTAMPTZ,
  provider_request_ref_private STRING,
  raw_response_sha256          STRING,
  validation_state             STRING NOT NULL,
  issue_codes                  JSONB NOT NULL DEFAULT '[]',
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT extraction_runs_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  CONSTRAINT extraction_runs_source_fk
    FOREIGN KEY (tenant_id, source_id) REFERENCES invoice_sources (tenant_id, id),
  UNIQUE INDEX extraction_runs_attempt_idx
    (tenant_id, invoice_id, source_id, schema_version, template_version, attempt)
);

CREATE TABLE IF NOT EXISTS claim_sets (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  id                 UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id         UUID NOT NULL,
  claim_set_version  INT8 NOT NULL,
  extraction_run_id  UUID NOT NULL,
  validation_state   STRING NOT NULL,
  issue_codes        JSONB NOT NULL DEFAULT '[]',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT claim_sets_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  CONSTRAINT claim_sets_run_fk
    FOREIGN KEY (tenant_id, extraction_run_id) REFERENCES extraction_runs (tenant_id, id),
  UNIQUE INDEX claim_sets_version_idx (tenant_id, invoice_id, claim_set_version)
);

CREATE TABLE IF NOT EXISTS extracted_claims (
  tenant_id            UUID NOT NULL REFERENCES tenants(id),
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  claim_set_id          UUID NOT NULL,
  field_name            STRING NOT NULL,
  value_type            STRING NOT NULL,
  raw_value             STRING,
  normalized_value      JSONB,
  amount_minor          INT8,
  currency              STRING,
  validation_state      STRING NOT NULL,
  page_number           INT8,
  bounding_box          JSONB,
  text_excerpt          STRING,
  text_excerpt_sha256   STRING,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT extracted_claims_set_fk
    FOREIGN KEY (tenant_id, claim_set_id) REFERENCES claim_sets (tenant_id, id),
  UNIQUE INDEX extracted_claims_field_idx (tenant_id, claim_set_id, field_name)
);

CREATE TABLE IF NOT EXISTS workflow_tasks (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  id                 UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id         UUID NOT NULL,
  task_type          STRING NOT NULL,
  task_version       INT8 NOT NULL,
  state              STRING NOT NULL,
  initiated_by       UUID,
  actor_display      STRING NOT NULL,
  knowledge_cutoff_at TIMESTAMPTZ NOT NULL,
  input_fingerprint  STRING NOT NULL,
  input_object_refs  JSONB NOT NULL DEFAULT '[]',
  current_attempt    INT8 NOT NULL DEFAULT 0,
  lease_owner        STRING,
  lease_expires_at   TIMESTAMPTZ,
  not_before         TIMESTAMPTZ,
  started_at         TIMESTAMPTZ,
  completed_at       TIMESTAMPTZ,
  public_summary     STRING,
  private_error_code STRING,
  private_error_ref  STRING,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT workflow_tasks_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  UNIQUE INDEX workflow_tasks_identity_idx
    (tenant_id, invoice_id, task_type, task_version, input_fingerprint),
  INDEX workflow_tasks_runnable_idx
    (tenant_id, state, not_before, lease_expires_at)
);

CREATE TABLE IF NOT EXISTS workflow_task_attempts (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  id                 UUID NOT NULL DEFAULT gen_random_uuid(),
  task_id            UUID NOT NULL,
  attempt            INT8 NOT NULL,
  state              STRING NOT NULL,
  lease_owner        STRING,
  lease_expires_at   TIMESTAMPTZ,
  started_at         TIMESTAMPTZ,
  completed_at       TIMESTAMPTZ,
  output_object_refs JSONB NOT NULL DEFAULT '[]',
  private_error_code STRING,
  private_error_ref  STRING,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT workflow_attempts_task_fk
    FOREIGN KEY (tenant_id, task_id) REFERENCES workflow_tasks (tenant_id, id),
  UNIQUE INDEX workflow_attempts_number_idx (tenant_id, task_id, attempt)
);

CREATE TABLE IF NOT EXISTS invoice_events (
  tenant_id            UUID NOT NULL REFERENCES tenants(id),
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id           UUID NOT NULL,
  sequence             INT8 NOT NULL,
  event_type           STRING NOT NULL,
  schema_version       INT8 NOT NULL,
  occurred_at          TIMESTAMPTZ NOT NULL,
  role                 STRING NOT NULL,
  task                 STRING NOT NULL,
  tool_display_name    STRING,
  state                STRING NOT NULL,
  aggregate_status     STRING NOT NULL,
  summary              STRING NOT NULL,
  initiated_by         UUID,
  actor_display        STRING NOT NULL,
  input_object_refs    JSONB NOT NULL DEFAULT '[]',
  produced_object_refs JSONB NOT NULL DEFAULT '[]',
  output_count         INT8,
  elapsed_ms           INT8,
  public_error         JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT invoice_events_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  UNIQUE INDEX invoice_events_sequence_idx (tenant_id, invoice_id, sequence)
);

CREATE TABLE IF NOT EXISTS event_outbox (
  tenant_id        UUID NOT NULL REFERENCES tenants(id),
  id               UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id       UUID NOT NULL,
  event_id         UUID NOT NULL,
  state            STRING NOT NULL DEFAULT 'PENDING',
  available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_owner      STRING,
  lease_expires_at TIMESTAMPTZ,
  delivery_count   INT8 NOT NULL DEFAULT 0,
  delivered_at     TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT event_outbox_event_fk
    FOREIGN KEY (tenant_id, event_id) REFERENCES invoice_events (tenant_id, id),
  CONSTRAINT event_outbox_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  UNIQUE INDEX event_outbox_event_idx (tenant_id, event_id),
  INDEX event_outbox_delivery_idx (tenant_id, state, available_at, lease_expires_at)
);

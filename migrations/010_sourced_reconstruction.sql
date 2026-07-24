-- Gate 2 — Sourced reconstruction (additive, non-destructive).
--
-- Consumes the durable START_RECONSTRUCTION workflow_tasks row created by Intake
-- and persists an immutable, source-bound pre-invoice reconstruction: shipment
-- events with exact source/version/provenance/time bindings, the seven charged
-- days, and explicit source-coverage requirements. Reuses the existing
-- workflow_tasks / workflow_task_attempts / invoice_events / event_outbox spine
-- from migration 007; adds no second orchestration mechanism.
--
-- Money is integer minor units + ISO currency. Every table is tenant-scoped
-- (tenant-first PK, matching the repo convention). Exact S3 locator/version live
-- only in *_private columns; public projections read verification state only.

-- Representative retained source artifacts (INVOICE/TARIFF/MILESTONE_EXPORT/
-- AVAILABILITY_NOTICE). The invoice PDF itself is already in invoice_sources;
-- these are the additional pre-invoice memory artifacts a reconstruction reads.
CREATE TABLE IF NOT EXISTS reconstruction_source_artifacts (
  tenant_id                 UUID NOT NULL REFERENCES tenants(id),
  id                        UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id                UUID NOT NULL,
  public_ref                STRING NOT NULL,
  source_type               STRING NOT NULL,
  display_name              STRING NOT NULL,
  mime_type                 STRING NOT NULL,
  provenance_classification STRING NOT NULL,
  public_disclosure         STRING NOT NULL,
  adapter_name              STRING NOT NULL,
  s3_bucket_ref_private     STRING NOT NULL,
  s3_object_key_private     STRING NOT NULL,
  s3_version_id_private     STRING NOT NULL,
  sha256                    STRING NOT NULL,
  byte_length               INT8 NOT NULL,
  verification_state        STRING NOT NULL,
  recorded_at               TIMESTAMPTZ NOT NULL,
  verified_at               TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT recon_source_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  UNIQUE INDEX recon_source_public_ref_idx (tenant_id, public_ref),
  UNIQUE INDEX recon_source_s3_version_idx
    (tenant_id, s3_object_key_private, s3_version_id_private)
);

-- One immutable reconstruction version per (invoice, version). knowledge_cutoff
-- is inherited from the START_RECONSTRUCTION task (= invoice received_at); no
-- event recorded after it may enter the reconstruction.
CREATE TABLE IF NOT EXISTS reconstructions (
  tenant_id            UUID NOT NULL REFERENCES tenants(id),
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id           UUID NOT NULL,
  version              INT8 NOT NULL,
  task_id              UUID NOT NULL,
  input_fingerprint    STRING NOT NULL,
  claim_set_version    INT8 NOT NULL,
  knowledge_cutoff_at  TIMESTAMPTZ NOT NULL,
  effective_timezone   STRING NOT NULL,
  state                STRING NOT NULL,
  event_count          INT8 NOT NULL DEFAULT 0,
  days_total           INT8 NOT NULL DEFAULT 0,
  days_complete        INT8 NOT NULL DEFAULT 0,
  mcp_correlation_id   STRING,
  mcp_query_ref_private STRING,
  public_summary       STRING NOT NULL,
  issue_codes          JSONB NOT NULL DEFAULT '[]',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at         TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT reconstructions_invoice_fk
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
  CONSTRAINT reconstructions_task_fk
    FOREIGN KEY (tenant_id, task_id) REFERENCES workflow_tasks (tenant_id, id),
  UNIQUE INDEX reconstructions_version_idx (tenant_id, invoice_id, version),
  UNIQUE INDEX reconstructions_fingerprint_idx (tenant_id, invoice_id, input_fingerprint)
);

-- One row per accepted/context/rejected shipment event bound to a reconstruction
-- version. occurred_at, effective_from/to, observed_at, recorded_at, received_at
-- are the distinct time domains (never conflated). recorded_before_cutoff is
-- derived from recorded_at <= knowledge_cutoff, never client-supplied.
CREATE TABLE IF NOT EXISTS reconstruction_events (
  tenant_id               UUID NOT NULL REFERENCES tenants(id),
  id                      UUID NOT NULL DEFAULT gen_random_uuid(),
  reconstruction_id       UUID NOT NULL,
  invoice_id              UUID NOT NULL,
  public_ref              STRING NOT NULL,
  event_type              STRING NOT NULL,
  shipment_ref            STRING NOT NULL,
  container_ref           STRING NOT NULL,
  source_artifact_id      UUID NOT NULL,
  source_version_ref_private STRING NOT NULL,
  source_anchor_private   JSONB NOT NULL,
  display_anchor_public   STRING NOT NULL,
  provenance_classification STRING NOT NULL,
  occurred_at             TIMESTAMPTZ NOT NULL,
  effective_from          DATE,
  effective_to            DATE,
  observed_at             TIMESTAMPTZ,
  recorded_at             TIMESTAMPTZ NOT NULL,
  received_at             TIMESTAMPTZ NOT NULL,
  recorded_before_cutoff  BOOL NOT NULL,
  normalized_facts        JSONB NOT NULL DEFAULT '{}',
  use_state               STRING NOT NULL,
  verification_state      STRING NOT NULL,
  display_sequence        INT8 NOT NULL,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT recon_events_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  CONSTRAINT recon_events_source_fk
    FOREIGN KEY (tenant_id, source_artifact_id)
      REFERENCES reconstruction_source_artifacts (tenant_id, id),
  UNIQUE INDEX recon_events_public_ref_idx (tenant_id, reconstruction_id, public_ref),
  INDEX recon_events_recon_idx (tenant_id, reconstruction_id, display_sequence)
);

-- The seven charged days. invoice_rate_minor from the extracted claim;
-- applicable_rate_minor stays NULL until Gate 3/4 validate a rule. Gate 2 fills
-- coverage_state and chargeability from bound events; outcome resolves in Gate 4.
CREATE TABLE IF NOT EXISTS reconstruction_charged_days (
  tenant_id             UUID NOT NULL REFERENCES tenants(id),
  id                    UUID NOT NULL DEFAULT gen_random_uuid(),
  reconstruction_id     UUID NOT NULL,
  invoice_id            UUID NOT NULL,
  charge_date           DATE NOT NULL,
  invoice_claim_field   STRING NOT NULL,
  chargeability         STRING NOT NULL,
  coverage_state        STRING NOT NULL,
  state                 STRING NOT NULL,
  invoice_rate_minor    INT8 NOT NULL,
  applicable_rate_minor INT8,
  currency              STRING NOT NULL,
  outcome               STRING NOT NULL,
  dispute_amount_minor  INT8,
  missing_requirements  JSONB NOT NULL DEFAULT '[]',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT recon_days_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  UNIQUE INDEX recon_days_date_idx (tenant_id, reconstruction_id, charge_date)
);

-- Which shipment events adjudicate each charged day and in what role.
CREATE TABLE IF NOT EXISTS reconstruction_day_event_bindings (
  tenant_id           UUID NOT NULL REFERENCES tenants(id),
  charged_day_id      UUID NOT NULL,
  reconstruction_event_id UUID NOT NULL,
  role                STRING NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, charged_day_id, reconstruction_event_id, role),
  CONSTRAINT recon_day_binding_day_fk
    FOREIGN KEY (tenant_id, charged_day_id)
      REFERENCES reconstruction_charged_days (tenant_id, id),
  CONSTRAINT recon_day_binding_event_fk
    FOREIGN KEY (tenant_id, reconstruction_event_id)
      REFERENCES reconstruction_events (tenant_id, id)
);

-- Categorical source-coverage requirements for the reconstruction (PRESENT_VERIFIED,
-- MISSING, UNAVAILABLE, CONFLICTED, NOT_APPLICABLE). Never a stored confidence.
CREATE TABLE IF NOT EXISTS reconstruction_coverage (
  tenant_id           UUID NOT NULL REFERENCES tenants(id),
  reconstruction_id   UUID NOT NULL,
  invoice_id          UUID NOT NULL,
  requirement_code    STRING NOT NULL,
  coverage_state      STRING NOT NULL,
  detail              STRING,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, reconstruction_id, requirement_code),
  CONSTRAINT recon_coverage_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id)
);

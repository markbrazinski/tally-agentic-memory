-- Gate 4 — Deterministic judgment (additive, non-destructive).
--
-- One independently-explainable judgment row per charged day and one frozen,
-- immutable recommendation version per reconstruction. All money is integer
-- minor units. Models never write these rows — Python computes every amount.
-- Tenant-scoped. Consumes only Gate 2 (charged days + coverage) and Gate 3
-- (applicable rule) persisted outputs.

-- Per-charged-day deterministic judgment. Each row explains one day: the
-- invoice rate, the applicable rate, the discrepancy, and the outcome — bound to
-- the charged day, the applicable rule, and the recommendation version.
CREATE TABLE IF NOT EXISTS charged_day_judgments (
  tenant_id             UUID NOT NULL REFERENCES tenants(id),
  id                    UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id            UUID NOT NULL,
  reconstruction_id     UUID NOT NULL,
  recommendation_id     UUID NOT NULL,
  charged_day_id        UUID NOT NULL,
  charge_date           DATE NOT NULL,
  invoice_rate_minor    INT8 NOT NULL,
  applicable_rate_minor INT8,
  discrepancy_minor     INT8 NOT NULL,
  currency              STRING NOT NULL,
  outcome               STRING NOT NULL,
  coverage_state        STRING NOT NULL,
  applicable_rule_id    UUID,
  explanation           STRING NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT judgment_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  CONSTRAINT judgment_day_fk
    FOREIGN KEY (tenant_id, charged_day_id)
      REFERENCES reconstruction_charged_days (tenant_id, id),
  UNIQUE INDEX judgment_day_version_idx (tenant_id, recommendation_id, charge_date)
);

-- One frozen recommendation version per reconstruction fingerprint. Immutable:
-- corrections create a new version. recommendation_type is DISPUTE,
-- APPROVE_FOR_PAYMENT, or REQUEST_EVIDENCE. Amounts are integer minor units.
CREATE TABLE IF NOT EXISTS recommendations (
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  id                       UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id               UUID NOT NULL,
  reconstruction_id        UUID NOT NULL,
  applicable_rule_id       UUID,
  version                  INT8 NOT NULL,
  input_fingerprint        STRING NOT NULL,
  recommendation_type      STRING NOT NULL,
  disputed_amount_minor    INT8 NOT NULL,
  supported_amount_minor   INT8 NOT NULL,
  claimed_amount_minor     INT8 NOT NULL,
  currency                 STRING NOT NULL,
  days_total               INT8 NOT NULL,
  days_covered             INT8 NOT NULL,
  evidence_coverage        STRING NOT NULL,
  state                    STRING NOT NULL,
  digest                   STRING NOT NULL,
  public_summary           STRING NOT NULL,
  superseded_by            UUID,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT recommendation_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  UNIQUE INDEX recommendation_version_idx (tenant_id, reconstruction_id, version),
  UNIQUE INDEX recommendation_fingerprint_idx (tenant_id, reconstruction_id, input_fingerprint)
);

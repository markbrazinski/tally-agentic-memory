-- Gate 5 — Human authority and atomic seal (additive, non-destructive).
--
-- One approval binds a human to one exact immutable recommendation version.
-- One atomic seal binds the recommendation, its seven day judgments, the
-- applicable rule, the reconstruction version, the claim set, the exact invoice
-- source, the approver authorization, and timestamps/revision. Sealed records
-- are never edited in place. Tenant-scoped.

-- Human approval of one frozen recommendation version. Idempotency key +
-- optimistic concurrency (expected_recommendation_version). Repeated approval
-- with the same idempotency key replays; a stale version is rejected.
CREATE TABLE IF NOT EXISTS approvals (
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  id                       UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id               UUID NOT NULL,
  reconstruction_id        UUID NOT NULL,
  recommendation_id        UUID NOT NULL,
  recommendation_version   INT8 NOT NULL,
  recommendation_digest    STRING NOT NULL,
  idempotency_key          STRING NOT NULL,
  request_hash             STRING NOT NULL,
  approver_user_id         UUID,
  approver_display         STRING NOT NULL,
  approver_kind            STRING NOT NULL,
  decision                 STRING NOT NULL,
  state                    STRING NOT NULL,
  response_snapshot        JSONB,
  approved_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, idempotency_key),
  CONSTRAINT approval_recommendation_fk
    FOREIGN KEY (tenant_id, recommendation_id) REFERENCES recommendations (tenant_id, id),
  UNIQUE INDEX approval_id_idx (tenant_id, id),
  UNIQUE INDEX approval_active_idx (tenant_id, recommendation_id)
    WHERE state = 'APPROVED'
);

-- One atomic decision seal per approved recommendation. Immutable: a correction
-- creates a new recommendation version + new seal, never an in-place edit.
-- bound_object_refs records every exact input version bound at seal time.
CREATE TABLE IF NOT EXISTS decision_seals (
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  id                       UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id               UUID NOT NULL,
  reconstruction_id        UUID NOT NULL,
  recommendation_id        UUID NOT NULL,
  recommendation_version   INT8 NOT NULL,
  applicable_rule_id       UUID,
  claim_set_version        INT8 NOT NULL,
  approval_id              UUID NOT NULL,
  approver_display         STRING NOT NULL,
  revision                 INT8 NOT NULL,
  seal_digest              STRING NOT NULL,
  bound_object_refs        JSONB NOT NULL,
  invoice_source_ref_private STRING NOT NULL,
  public_summary           STRING NOT NULL,
  -- sealed_at is the DATA timestamp; sealed_txn_ts is the CockroachDB HLC commit
  -- timestamp (PROOF). The two time domains are never conflated (TDD D1).
  sealed_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  sealed_txn_ts            DECIMAL,
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT seal_recommendation_fk
    FOREIGN KEY (tenant_id, recommendation_id) REFERENCES recommendations (tenant_id, id),
  CONSTRAINT seal_approval_fk
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, id),
  UNIQUE INDEX seal_recommendation_idx (tenant_id, recommendation_id),
  UNIQUE INDEX seal_revision_idx (tenant_id, invoice_id, revision)
);

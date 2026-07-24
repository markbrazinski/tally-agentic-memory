-- Gate 3 — Applicable rule via Distributed Vector Indexing (additive).
--
-- Persists vector RETRIEVAL separately from deterministic APPLICABILITY. A
-- candidate retrieved by the vector index is only ever a candidate; it becomes
-- an applicable rule solely when every deterministic validator passes. The
-- existing tariff_clauses table (migration 002/005) is the corpus and the real
-- CockroachDB VECTOR INDEX; this migration adds only the run/candidate/rule
-- records and the charged-day binding. Tenant-scoped, additive, non-destructive.

-- One vector retrieval run per reconstruction hero query.
CREATE TABLE IF NOT EXISTS rule_retrieval_runs (
  tenant_id               UUID NOT NULL REFERENCES tenants(id),
  id                      UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id              UUID NOT NULL,
  reconstruction_id       UUID NOT NULL,
  query_fingerprint       STRING NOT NULL,
  query_text_private      STRING NOT NULL,
  vector_index_name       STRING NOT NULL,
  embedding_model         STRING NOT NULL,
  embedding_input_sha256  STRING NOT NULL,
  state                   STRING NOT NULL,
  candidate_count         INT8 NOT NULL DEFAULT 0,
  started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at            TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT rule_run_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  UNIQUE INDEX rule_run_fingerprint_idx (tenant_id, reconstruction_id, query_fingerprint)
);

-- Ranked candidates from the vector index. distance is the real L2 distance
-- (a ranking hint), never a fabricated confidence. candidate_state is
-- RETRIEVED until deterministic validation accepts or rejects it.
CREATE TABLE IF NOT EXISTS rule_candidates (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  id                 UUID NOT NULL DEFAULT gen_random_uuid(),
  retrieval_run_id   UUID NOT NULL,
  tariff_clause_id   UUID NOT NULL,
  clause_public_ref  STRING NOT NULL,
  rank               INT8 NOT NULL,
  distance           FLOAT8 NOT NULL,
  candidate_state    STRING NOT NULL,
  rejection_code     STRING,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT rule_candidate_run_fk
    FOREIGN KEY (tenant_id, retrieval_run_id) REFERENCES rule_retrieval_runs (tenant_id, id),
  CONSTRAINT rule_candidate_clause_fk
    FOREIGN KEY (tenant_id, tariff_clause_id) REFERENCES tariff_clauses (tenant_id, id),
  UNIQUE INDEX rule_candidate_rank_idx (tenant_id, retrieval_run_id, rank)
);

-- An applicable rule exists only after deterministic validation. validation_state
-- is VERIFIED (all validators passed), REJECTED, or CONFLICTED. rate is minor
-- units + ISO currency. Bound to the reconstruction version it governs.
CREATE TABLE IF NOT EXISTS applicable_rules (
  tenant_id             UUID NOT NULL REFERENCES tenants(id),
  id                    UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id            UUID NOT NULL,
  reconstruction_id     UUID NOT NULL,
  retrieval_run_id      UUID NOT NULL,
  tariff_clause_id      UUID NOT NULL,
  public_ref            STRING NOT NULL,
  clause_ref            STRING NOT NULL,
  display_excerpt       STRING NOT NULL,
  rate_minor            INT8 NOT NULL,
  currency              STRING NOT NULL,
  unit                  STRING NOT NULL,
  effective_from        DATE NOT NULL,
  effective_to          DATE,
  scope_code            STRING NOT NULL,
  source_locator_private STRING NOT NULL,
  source_version_state  STRING NOT NULL,
  validation_state      STRING NOT NULL,
  validation_results    JSONB NOT NULL DEFAULT '{}',
  validated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT applicable_rule_recon_fk
    FOREIGN KEY (tenant_id, reconstruction_id) REFERENCES reconstructions (tenant_id, id),
  CONSTRAINT applicable_rule_run_fk
    FOREIGN KEY (tenant_id, retrieval_run_id) REFERENCES rule_retrieval_runs (tenant_id, id),
  CONSTRAINT applicable_rule_clause_fk
    FOREIGN KEY (tenant_id, tariff_clause_id) REFERENCES tariff_clauses (tenant_id, id),
  UNIQUE INDEX applicable_rule_recon_idx (tenant_id, reconstruction_id)
);

-- Which applicable rule governs each charged day.
CREATE TABLE IF NOT EXISTS charged_day_rule_bindings (
  tenant_id          UUID NOT NULL REFERENCES tenants(id),
  charged_day_id     UUID NOT NULL,
  applicable_rule_id UUID NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, charged_day_id, applicable_rule_id),
  CONSTRAINT cd_rule_binding_day_fk
    FOREIGN KEY (tenant_id, charged_day_id)
      REFERENCES reconstruction_charged_days (tenant_id, id),
  CONSTRAINT cd_rule_binding_rule_fk
    FOREIGN KEY (tenant_id, applicable_rule_id)
      REFERENCES applicable_rules (tenant_id, id)
);

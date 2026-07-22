-- Migration 001a: recording-side tables only (Bundle R Session 3).
--
-- Scope per docs/bundle-r.md Session 3: carriers, recordings,
-- tariff_snapshots, terminal_snapshots. NOT the full schema (Bundle 0's
-- job) and NOT tariff_clauses (C-SPANN/embeddings, deferred per Session
-- Lock 3 - no LLM/embeddings/Bedrock in Bundle R).
--
-- tenants is created here too, minimally (id/name/created_at per TDD
-- ss2.1) only to satisfy the FK these tables require - Bundle 0 owns the
-- full tenants story (users, auth, etc.) and can extend this table
-- additively without conflict.
--
-- Retention config: gc.ttlseconds = 7776000 (90 days) per docs/smoke-results.md
-- S7, which passed on BASIC tier via an explicit CONFIGURE ZONE (not a
-- documented default - the untouched-table baseline was 4500s/75min).
-- Applied per-table below, matching the smoke test's proof that this is
-- honored on this tier.

CREATE TABLE IF NOT EXISTS tenants (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       STRING NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS carriers (
  tenant_id        UUID NOT NULL REFERENCES tenants(id),
  id               UUID NOT NULL DEFAULT gen_random_uuid(),
  scac             STRING NOT NULL,
  name             STRING NOT NULL,
  date_format_hint STRING,
  free_time_basis_default STRING,
  lanes            JSONB NOT NULL DEFAULT '[]',
  tariff_source_url STRING,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX carriers_scac_idx (tenant_id, scac)
);

CREATE TABLE IF NOT EXISTS recordings (
  tenant_id     UUID NOT NULL REFERENCES tenants(id),
  id            UUID NOT NULL DEFAULT gen_random_uuid(),
  run_date      DATE NOT NULL,
  target        STRING NOT NULL,            -- tariff | terminal
  carrier_id    UUID,                       -- for tariff targets
  lane          STRING,
  terminal_code STRING,                    -- for terminal targets
  status        STRING NOT NULL,            -- COMMITTED | FAILED | SKIPPED
  rows_written  INT NOT NULL DEFAULT 0,
  s3_key        STRING,
  error         STRING,
  started_at    TIMESTAMPTZ NOT NULL,
  committed_at  TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id),
  -- lane/terminal_code are legitimately NULL depending on target (tariff
  -- rows never set terminal_code; terminal rows never set lane), but SQL
  -- NULL is never equal to NULL for unique-index purposes (confirmed
  -- against the real cluster) - a plain UNIQUE INDEX on these raw columns
  -- silently fails to catch duplicate tariff-target commits, since every
  -- one of them has terminal_code=NULL. COALESCE to a sentinel so NULL
  -- becomes a real, comparable value and the "one recording per day per
  -- target" invariant actually holds.
  UNIQUE INDEX recordings_day_idx (
    tenant_id, run_date, target, carrier_id,
    COALESCE(lane, ''), COALESCE(terminal_code, '')
  )
);

CREATE TABLE IF NOT EXISTS tariff_snapshots (
  tenant_id      UUID NOT NULL REFERENCES tenants(id),
  id             UUID NOT NULL DEFAULT gen_random_uuid(),
  carrier_id     UUID NOT NULL,
  lane           STRING NOT NULL,
  version_label  STRING NOT NULL,
  effective_date DATE NOT NULL,
  captured_at    TIMESTAMPTZ NOT NULL,      -- OBSERVATION domain (S3 server timestamp)
  source_url     STRING NOT NULL,
  s3_key         STRING NOT NULL,
  doc_sha256     STRING NOT NULL,
  doc_text       STRING NOT NULL,
  headline_rate  DECIMAL(12,2),
  -- embedding VECTOR(1024) intentionally omitted: tariff_clauses/C-SPANN is
  -- Lock-3-deferred, out of scope for this migration.
  recording_id   UUID,
  committed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- SYSTEM domain
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT tariff_snap_fk FOREIGN KEY (tenant_id, carrier_id) REFERENCES carriers (tenant_id, id),
  UNIQUE INDEX tariff_snap_day_idx (tenant_id, carrier_id, lane, captured_at),
  INDEX tariff_snap_lookup_idx (tenant_id, carrier_id, lane, effective_date DESC)
);

CREATE TABLE IF NOT EXISTS terminal_snapshots (
  tenant_id     UUID NOT NULL REFERENCES tenants(id),
  id            UUID NOT NULL DEFAULT gen_random_uuid(),
  terminal_code STRING NOT NULL,
  captured_at   TIMESTAMPTZ NOT NULL,       -- OBSERVATION domain
  gate_status   JSONB NOT NULL DEFAULT '{}',
  appointment_availability JSONB NOT NULL DEFAULT '{}',
  empty_return_restrictions JSONB NOT NULL DEFAULT '{}',
  source        STRING NOT NULL,            -- source URL or 'seed:story'
  s3_key        STRING,
  sha256        STRING NOT NULL,
  recording_id  UUID,
  committed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX terminal_snap_day_idx (tenant_id, terminal_code, captured_at)
);

-- Retention config: smoke-gated per docs/smoke-results.md S7 (BASIC tier,
-- passed with an explicit CONFIGURE ZONE at 7776000s/90 days).
ALTER TABLE recordings CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE tariff_snapshots CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE terminal_snapshots CONFIGURE ZONE USING gc.ttlseconds = 7776000;

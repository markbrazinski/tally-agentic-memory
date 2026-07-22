-- Migration 002: full remaining §2 schema (Bundle 0, Session 1).
--
-- Additive to 001a/001b (Bundle R) — tenants/carriers/recordings/
-- tariff_snapshots/terminal_snapshots already live and untouched here,
-- except tariff_snapshots gains the `embedding` column it deferred per
-- Bundle R lock 3 (no embeddings in that bundle).
--
-- tariff_clauses ships WITHOUT a VECTOR INDEX in this migration: TDD §2.5
-- requires bulk clause inserts to happen BEFORE CREATE VECTOR INDEX, and
-- zero clause data exists yet (Clerk step 4 / clause pinning is Bundle 2
-- scope). The index is created in whichever bundle first seeds clause
-- data — deferred deliberately, not an oversight.
--
-- Retention (gc.ttlseconds = 7776000, 90 days) applied to every
-- historically-queried table per TDD §2.22, using the value already
-- smoke-gate-confirmed on BASIC tier (docs/smoke-results.md S7). Tables
-- carrying this from 001a (tariff_snapshots, terminal_snapshots) are not
-- re-altered here.

-- Nullable (unlike tariff_clauses.embedding below): every tariff_snapshots
-- row committed before this column existed (all of Bundle R's live
-- history) has no embedding and never will unless a future bundle adds a
-- backfill job. New rows may also go unembedded until that bundle exists
-- — this column's presence doesn't imply population.
ALTER TABLE tariff_snapshots ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);

CREATE TABLE IF NOT EXISTS users (
  tenant_id    UUID NOT NULL REFERENCES tenants(id),
  id           UUID NOT NULL DEFAULT gen_random_uuid(),
  email        STRING NOT NULL,
  display_name STRING NOT NULL,
  title        STRING,
  role         STRING NOT NULL DEFAULT 'viewer',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX users_email_idx (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS tariff_clauses (
  tenant_id       UUID NOT NULL REFERENCES tenants(id),
  id              UUID NOT NULL DEFAULT gen_random_uuid(),
  carrier_id      UUID NOT NULL,
  snapshot_id     UUID NOT NULL,
  clause_ref      STRING NOT NULL,
  clause_kind     STRING NOT NULL,
  clause_text     STRING NOT NULL,
  rate_amount     DECIMAL(12,2),
  free_time_basis STRING,
  sha256          STRING NOT NULL,
  embedding       VECTOR(1024) NOT NULL,  -- required: every row needs a real Titan
                                          -- embedding at insert time regardless of
                                          -- whether the VECTOR INDEX exists yet (see
                                          -- file header) — unlike tariff_snapshots
                                          -- above, this table has zero rows until
                                          -- Bundle 2's seeding computes real vectors,
                                          -- so NOT NULL never faces an unpopulated row.
  committed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT clause_snap_fk FOREIGN KEY (tenant_id, snapshot_id) REFERENCES tariff_snapshots (tenant_id, id)
  -- VECTOR INDEX intentionally omitted — see file header.
);

CREATE TABLE IF NOT EXISTS containers (
  tenant_id    UUID NOT NULL REFERENCES tenants(id),
  id           UUID NOT NULL DEFAULT gen_random_uuid(),
  container_no STRING NOT NULL,
  carrier_id   UUID NOT NULL,
  lane         STRING NOT NULL,
  meta         JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX containers_no_idx (tenant_id, container_no)
);

CREATE TABLE IF NOT EXISTS container_events (
  tenant_id    UUID NOT NULL REFERENCES tenants(id),
  id           UUID NOT NULL DEFAULT gen_random_uuid(),
  container_id UUID NOT NULL,
  event_type   STRING NOT NULL,
  occurred_at  TIMESTAMPTZ NOT NULL,
  captured_at  TIMESTAMPTZ NOT NULL,
  source       STRING NOT NULL,
  details      JSONB NOT NULL DEFAULT '{}',
  committed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT ce_container_fk FOREIGN KEY (tenant_id, container_id) REFERENCES containers (tenant_id, id),
  INDEX ce_timeline_idx (tenant_id, container_id, occurred_at)
);

CREATE TABLE IF NOT EXISTS invoices (
  tenant_id        UUID NOT NULL REFERENCES tenants(id),
  id               UUID NOT NULL DEFAULT gen_random_uuid(),
  carrier_id       UUID NOT NULL,
  container_id     UUID,
  invoice_no       STRING,
  received_at      TIMESTAMPTZ NOT NULL,
  s3_key           STRING NOT NULL,
  sha256           STRING NOT NULL,
  page_count       INT,
  is_image_only    BOOL NOT NULL DEFAULT false,
  raw_text         STRING,
  extracted        JSONB,
  extraction_model STRING,
  amount           DECIMAL(12,2),
  currency         STRING DEFAULT 'USD',
  invoice_date     DATE,
  status           STRING NOT NULL DEFAULT 'RECEIVED',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX invoices_sha256_idx (tenant_id, sha256),
  INDEX invoices_status_idx (tenant_id, status, received_at)
);

CREATE TABLE IF NOT EXISTS clerk_runs (
  tenant_id    UUID NOT NULL REFERENCES tenants(id),
  id           UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id   UUID NOT NULL,
  status       STRING NOT NULL DEFAULT 'QUEUED',
  current_step INT NOT NULL DEFAULT 0,
  steps        JSONB NOT NULL DEFAULT '[]',
  error        STRING,
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  INDEX clerk_runs_invoice_idx (tenant_id, invoice_id, created_at DESC)
);

CREATE TABLE IF NOT EXISTS findings (
  tenant_id            UUID NOT NULL REFERENCES tenants(id),
  id                   UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id           UUID NOT NULL,
  clerk_run_id         UUID NOT NULL,
  verdict              STRING NOT NULL,
  cited_rule           STRING,
  field_results        JSONB NOT NULL,
  window_result        JSONB NOT NULL,
  tariff_result        JSONB NOT NULL,
  timeline_event_count INT NOT NULL DEFAULT 0,
  summary              STRING NOT NULL,
  amount_disputed      DECIMAL(12,2),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX findings_invoice_idx (tenant_id, invoice_id, clerk_run_id)
);

CREATE TABLE IF NOT EXISTS cases (
  tenant_id         UUID NOT NULL REFERENCES tenants(id),
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id        UUID NOT NULL,
  finding_id        UUID NOT NULL,
  carrier_id        UUID NOT NULL,
  state             STRING NOT NULL DEFAULT 'ANALYZED',
  pin_date          DATE NOT NULL,
  draft_dispute     STRING NOT NULL,
  amount            DECIMAL(12,2) NOT NULL,
  sealed_at_display TIMESTAMPTZ,
  sealed_txn_ts     DECIMAL,
  sealed_by         UUID,
  evidence_hash     STRING,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX cases_invoice_idx (tenant_id, invoice_id),
  INDEX cases_record_idx (tenant_id, pin_date),
  INDEX cases_state_idx (tenant_id, state)
);

CREATE TABLE IF NOT EXISTS case_evidence (
  tenant_id           UUID NOT NULL REFERENCES tenants(id),
  id                  UUID NOT NULL DEFAULT gen_random_uuid(),
  case_id             UUID NOT NULL,
  kind                STRING NOT NULL,
  source_table        STRING NOT NULL,
  source_id           UUID NOT NULL,
  content             JSONB NOT NULL,
  content_sha256      STRING NOT NULL,
  embedding_sha256    STRING,
  captured_at_display TIMESTAMPTZ,
  sealed              BOOL NOT NULL DEFAULT false,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT ev_case_fk FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
  INDEX ev_case_idx (tenant_id, case_id)
);

CREATE TABLE IF NOT EXISTS contests (
  tenant_id        UUID NOT NULL REFERENCES tenants(id),
  id               UUID NOT NULL DEFAULT gen_random_uuid(),
  case_id          UUID NOT NULL,
  carrier_id       UUID NOT NULL,
  received_at      TIMESTAMPTZ NOT NULL,
  sender           STRING NOT NULL,
  claim_text       STRING NOT NULL,
  claimed_rate     DECIMAL(12,2),
  s3_key           STRING,
  status           STRING NOT NULL DEFAULT 'OPEN',
  rebuttal_text    STRING,
  rebuttal_sent_at TIMESTAMPTZ,
  resolved_at      TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT contest_case_fk FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
  INDEX contests_open_idx (tenant_id, status, received_at)
);

CREATE TABLE IF NOT EXISTS ledger_events (
  tenant_id   UUID NOT NULL REFERENCES tenants(id),
  id          UUID NOT NULL DEFAULT gen_random_uuid(),
  case_id     UUID NOT NULL,
  carrier_id  UUID NOT NULL,
  kind        STRING NOT NULL,
  amount      DECIMAL(12,2),
  occurred_on DATE NOT NULL,
  details     JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  INDEX ledger_day_idx (tenant_id, occurred_on),
  INDEX ledger_carrier_idx (tenant_id, carrier_id, kind)
);

CREATE TABLE IF NOT EXISTS query_log (
  tenant_id     UUID NOT NULL,
  id            UUID NOT NULL DEFAULT gen_random_uuid(),
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind          STRING NOT NULL,
  tag           STRING NOT NULL,
  sql_text      STRING,
  elapsed_ms    INT,
  row_count     INT,
  render_source STRING NOT NULL DEFAULT 'live',
  actor         STRING,
  ok            BOOL NOT NULL DEFAULT true,
  error         STRING,
  PRIMARY KEY (tenant_id, id),
  INDEX query_log_ts_idx (tenant_id, ts DESC)
) WITH (ttl_expiration_expression = "ts + INTERVAL '30 days'", ttl_job_cron = '@daily');

CREATE TABLE IF NOT EXISTS eval_runs (
  tenant_id         UUID NOT NULL,
  id                UUID NOT NULL DEFAULT gen_random_uuid(),
  git_sha           STRING NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  invoices_total    INT NOT NULL DEFAULT 0,
  invoices_passed   INT NOT NULL DEFAULT 0,
  assertions_total  INT NOT NULL DEFAULT 0,
  assertions_passed INT NOT NULL DEFAULT 0,
  report_s3_key     STRING,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS eval_results (
  tenant_id  UUID NOT NULL,
  id         UUID NOT NULL DEFAULT gen_random_uuid(),
  run_id     UUID NOT NULL,
  invoice_id UUID NOT NULL,
  archetype  STRING NOT NULL,
  assertion  STRING NOT NULL,
  expected   STRING NOT NULL,
  actual     STRING NOT NULL,
  passed     BOOL NOT NULL,
  PRIMARY KEY (tenant_id, id),
  INDEX eval_results_run_idx (tenant_id, run_id, passed)
);

-- Retention: 90 days on every historically-queried table not already
-- covered by 001a (tariff_snapshots, terminal_snapshots).
ALTER TABLE tariff_clauses CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE container_events CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE cases CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE case_evidence CONFIGURE ZONE USING gc.ttlseconds = 7776000;

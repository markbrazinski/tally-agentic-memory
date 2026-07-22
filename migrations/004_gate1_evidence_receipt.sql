-- Gate 1: exact-version tariff facts and canonical sealed receipts.
-- Existing observation timestamps remain `tariff_snapshots.captured_at`;
-- CockroachDB commit timestamps remain `committed_at` / `sealed_txn_ts`.

ALTER TABLE tariff_snapshots
  ADD COLUMN IF NOT EXISTS source_version_id STRING;
ALTER TABLE tariff_snapshots
  ADD COLUMN IF NOT EXISTS source_byte_size INT8;

-- Gate 1 must not fabricate a vector before Gate 2 builds the real embedding
-- dataset. Verified clauses may therefore exist without an embedding.
ALTER TABLE tariff_clauses ALTER COLUMN embedding DROP NOT NULL;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS rate_currency STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS rate_unit STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS effective_from DATE;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS effective_to DATE;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS source_locator STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS confidence DECIMAL(5,4);
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS verification_status STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS verification_reason STRING;
CREATE UNIQUE INDEX IF NOT EXISTS tariff_clause_source_idx
  ON tariff_clauses (tenant_id, snapshot_id, sha256);

ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS source_version_id STRING;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS claimed_rate DECIMAL(12,2);
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS rate_unit STRING;
ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS charge_days INT8;

ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS tariff_clause_id UUID;
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS recorded_rate DECIMAL(12,2);
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS invoice_claimed_rate DECIMAL(12,2);
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS rate_unit STRING;
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS charge_days INT8;
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS recommendation STRING;
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS calculation JSONB;
ALTER TABLE findings
  ADD COLUMN IF NOT EXISTS human_approval_state STRING NOT NULL DEFAULT 'NOT_PRESSED';
ALTER TABLE findings
  ADD CONSTRAINT IF NOT EXISTS findings_clause_fk
  FOREIGN KEY (tenant_id, tariff_clause_id)
  REFERENCES tariff_clauses (tenant_id, id);

ALTER TABLE cases
  ADD COLUMN IF NOT EXISTS manifest_version INT8;
ALTER TABLE cases
  ADD COLUMN IF NOT EXISTS evidence_manifest JSONB;

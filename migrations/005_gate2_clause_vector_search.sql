-- Gate 2: product-path approximate clause retrieval.
--
-- `tariff_clauses.embedding` is the real CockroachDB VECTOR(1024) column
-- introduced by migration 002. Gate 1 deliberately made it nullable so a
-- verified retained-source fact is never blocked or supplied a fabricated
-- vector. This migration only indexes populated vectors; it neither creates
-- embeddings nor changes verification or decision state.
--
-- The scalar prefix is deliberately tenant + carrier. It permits ranking over
-- all retained capture versions for one carrier; snapshot and temporal
-- applicability are deterministic post-filters, never part of approximate
-- index selection. `embedding` remains the final vector key as required by
-- CockroachDB's VECTOR INDEX syntax.

ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS equipment_type STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS route_code STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS service_context STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS document_family STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS embedding_model STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS embedding_input_sha256 STRING;
ALTER TABLE tariff_clauses
  ADD COLUMN IF NOT EXISTS embedding_sha256 STRING;

-- CockroachDB requires this session setting when backfilling a VECTOR INDEX
-- over existing clause rows. The migration runner keeps one autocommit
-- connection for this ordered file, and the setting ends with that session.
SET sql_safe_updates = false;

CREATE VECTOR INDEX IF NOT EXISTS tariff_clause_embedding_search_idx
  ON tariff_clauses
  (tenant_id, carrier_id, embedding vector_l2_ops);

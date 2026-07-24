-- Durable audit link when a new idempotency key submits an already-known source.
ALTER TABLE ingestion_requests
    ADD COLUMN IF NOT EXISTS deduplicated_invoice_id UUID;

ALTER TABLE ingestion_requests
    ADD COLUMN IF NOT EXISTS deduplicated_source_id UUID;

CREATE INDEX IF NOT EXISTS ingestion_requests_duplicate_invoice_idx
    ON ingestion_requests (tenant_id, deduplicated_invoice_id)
    WHERE deduplicated_invoice_id IS NOT NULL;

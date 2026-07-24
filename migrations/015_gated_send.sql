-- Gate 6 — Gated external action (additive, non-destructive).
--
-- A bounded correspondence draft is produced ONLY from the sealed record. A
-- second human authorization plus fresh MCP / vector-binding / exact-source /
-- no-fallback gate checks must all pass before one idempotent send attempt calls
-- the controlled provider. A failed gate prevents send. Tenant-scoped.

-- Bounded draft derived only from the sealed decision fact pack. Locked fields
-- (amount, dates, identifiers, decision) are copied from the seal and validated;
-- the model may only write prose, never change a locked field.
CREATE TABLE IF NOT EXISTS correspondence_drafts (
  tenant_id             UUID NOT NULL REFERENCES tenants(id),
  id                    UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id            UUID NOT NULL,
  decision_seal_id      UUID NOT NULL,
  seal_digest           STRING NOT NULL,
  recipient_class       STRING NOT NULL,
  subject               STRING NOT NULL,
  body_prose            STRING NOT NULL,
  locked_fields         JSONB NOT NULL,
  locked_fields_digest  STRING NOT NULL,
  validation_state      STRING NOT NULL,
  state                 STRING NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT draft_seal_fk
    FOREIGN KEY (tenant_id, decision_seal_id) REFERENCES decision_seals (tenant_id, id),
  UNIQUE INDEX draft_seal_idx (tenant_id, decision_seal_id)
);

-- One send attempt per (draft, idempotency key). Records the second
-- authorization and the fresh-gate outcome; a failed gate leaves state
-- SEND_BLOCKED and never calls the provider. A provider timeout/retry is
-- idempotent on provider_idempotency_key and never duplicates delivery.
CREATE TABLE IF NOT EXISTS send_attempts (
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  id                       UUID NOT NULL DEFAULT gen_random_uuid(),
  invoice_id               UUID NOT NULL,
  draft_id                 UUID NOT NULL,
  decision_seal_id         UUID NOT NULL,
  idempotency_key          STRING NOT NULL,
  request_hash             STRING NOT NULL,
  second_approver_display  STRING NOT NULL,
  gate_state               STRING NOT NULL,
  send_state               STRING NOT NULL,
  provider_idempotency_key STRING NOT NULL,
  provider_message_id      STRING,
  recipient_class          STRING NOT NULL,
  blocked_reason           STRING,
  response_snapshot        JSONB,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at          TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, idempotency_key),
  CONSTRAINT send_draft_fk
    FOREIGN KEY (tenant_id, draft_id) REFERENCES correspondence_drafts (tenant_id, id),
  UNIQUE INDEX send_attempt_id_idx (tenant_id, id),
  UNIQUE INDEX send_provider_key_idx (tenant_id, provider_idempotency_key)
);

-- Each fresh gate check bound to a send attempt: MCP, VECTOR_BINDING,
-- EXACT_SOURCE, NO_FALLBACK, SECOND_AUTHORIZATION, LOCKED_FIELDS. All must be
-- VERIFIED for the send to proceed. Persisted separately for the audit trail.
CREATE TABLE IF NOT EXISTS send_gate_runs (
  tenant_id       UUID NOT NULL REFERENCES tenants(id),
  id              UUID NOT NULL DEFAULT gen_random_uuid(),
  send_attempt_id UUID NOT NULL,
  gate_code       STRING NOT NULL,
  gate_state      STRING NOT NULL,
  detail          STRING,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT gate_run_send_fk
    FOREIGN KEY (tenant_id, send_attempt_id) REFERENCES send_attempts (tenant_id, id),
  UNIQUE INDEX gate_run_code_idx (tenant_id, send_attempt_id, gate_code)
);

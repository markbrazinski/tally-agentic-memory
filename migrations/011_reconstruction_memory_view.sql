-- Gate 2 — pre-invoice shipment-event memory + the Managed MCP read view.
--
-- Additive, non-destructive. `shipment_event_memory` holds the representative
-- retained pre-invoice events (recorded before any invoice arrives), each bound
-- to a verified source version. `mcp_reconstruction_memory_v1` is the narrow,
-- allowlisted view the Managed MCP read is confined to: it exposes ONLY verified,
-- non-superseded events with their public refs and source verification state —
-- never raw S3 locators. This is the read model the fixed MCP SELECT targets.

CREATE TABLE IF NOT EXISTS shipment_event_memory (
  tenant_id                 UUID NOT NULL REFERENCES tenants(id),
  id                        UUID NOT NULL DEFAULT gen_random_uuid(),
  public_ref                STRING NOT NULL,
  shipment_ref              STRING NOT NULL,
  container_ref             STRING NOT NULL,
  event_type                STRING NOT NULL,
  source_public_ref         STRING NOT NULL,
  source_version_state      STRING NOT NULL,
  display_anchor            STRING NOT NULL,
  provenance_classification STRING NOT NULL,
  occurred_at               TIMESTAMPTZ NOT NULL,
  effective_from            DATE,
  effective_to              DATE,
  observed_at               TIMESTAMPTZ,
  recorded_at               TIMESTAMPTZ NOT NULL,
  superseded_by             UUID,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE INDEX shipment_event_memory_ref_idx (tenant_id, public_ref),
  INDEX shipment_event_memory_scope_idx
    (tenant_id, shipment_ref, container_ref, recorded_at)
);

-- The Managed MCP read model. Exposes only verified, non-superseded events.
-- The MCP identity is scoped so its select_query reads only from this view.
CREATE VIEW IF NOT EXISTS mcp_reconstruction_memory_v1 AS
  SELECT
    tenant_id,
    public_ref,
    event_type,
    shipment_ref,
    container_ref,
    source_public_ref,
    source_version_state AS source_verification_state,
    display_anchor,
    provenance_classification,
    occurred_at,
    recorded_at,
    observed_at,
    effective_from,
    effective_to
  FROM shipment_event_memory
  WHERE superseded_by IS NULL
    AND source_version_state = 'VERIFIED';

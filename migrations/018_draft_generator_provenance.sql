-- Generator provenance for correspondence drafts (P1).
--
-- A draft's prose may be written by Bedrock or by the deterministic generator
-- (the fallback used when Bedrock is unavailable). Which one ran is an audit
-- fact: it tells a reviewer whether a model touched the text at all, and which
-- model version did. Recorded per draft, never inferred after the fact.
--
-- prose_validation_state is separate from validation_state: the latter covers
-- the LOCKED fields (re-derived from the seal, so structurally always valid),
-- while this covers the generated PROSE, which is the only place a model can
-- introduce an unsupported fact. A draft is sendable only when both are VALIDATED.

ALTER TABLE correspondence_drafts
    ADD COLUMN IF NOT EXISTS generator_kind STRING;

ALTER TABLE correspondence_drafts
    ADD COLUMN IF NOT EXISTS generator_model_id STRING;

ALTER TABLE correspondence_drafts
    ADD COLUMN IF NOT EXISTS prose_validation_state STRING;

ALTER TABLE correspondence_drafts
    ADD COLUMN IF NOT EXISTS prose_validation_issues JSONB;

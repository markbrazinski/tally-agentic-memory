-- Authority transition — machine-readable reason codes (additive, non-destructive).
--
-- Delta §2.1 requires the authority evaluator to record machine-readable reason
-- codes on each frozen recommendation (e.g. MISSING_DAY_SOURCE for the 6/7 hero
-- revision that withholds financial authority). The recommendations table
-- (migration 013) froze the recommendation but had no place to store WHY it
-- withheld. This adds one JSONB column; existing rows default to [].
--
-- JSONB array of string codes, not a normalized table: the codes are a small,
-- read-only, per-recommendation fact inspected as a set (matches issue_codes on
-- reconstructions). Tenant scoping and immutability are unchanged — a corrected
-- recommendation still creates a new version rather than mutating this column.

ALTER TABLE recommendations
  ADD COLUMN IF NOT EXISTS reason_codes JSONB NOT NULL DEFAULT '[]';

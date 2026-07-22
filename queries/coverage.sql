-- Coverage tile prototype (Beat 6 / Build Gate 1) — Bundle R Session 3.
--
-- Per docs/tech-design-doc.md ss2.15: "Coverage tile = SELECT count(DISTINCT
-- run_date) ... WHERE status='COMMITTED' AND carrier=... AND lane=..." — the
-- "N days · CARRIER · LANE" number the demo cannot fudge (CLAUDE.md demo
-- requirement #1 / bundle-r.md's whole premise: "a recorded day can only be
-- created on that day").
--
-- Parameterized (psycopg-style named placeholders, matching
-- recording/commit.py's own SQL style) rather than hardcoding demo values —
-- callers bind tenant_id/carrier_id/lane explicitly, never a default
-- (CLAUDE.md: every query is tenant-scoped, never implicit).
--
-- NOTE for callers executing this file's text directly with psycopg: psycopg's
-- placeholder scanner reads the whole SQL string including comments, so a
-- literal percent sign anywhere in this file's prose would be misread as an
-- incomplete placeholder - keep this file's comments free of that character.
--
-- ---------------------------------------------------------------------
-- KNOWN GAP vs. the TDD spec (flagged, not silently fixed or invented):
--
-- TDD ss2.15's fuller description says the tile must count *live* coverage
-- only, "filtered by joining to snapshots where source != 'seed:story' —
-- the tile is never inflated by seed data" (Build Gate 5). That filter
-- requires a `source` column (or equivalent) on tariff_snapshots to
-- distinguish a real capture from story-domain seed data.
--
-- migrations/001a_recording_tables.sql's actual tariff_snapshots table (as
-- shipped by Bundle R Session 3) has NO `source` column at all — Session 3
-- did not seed any story-domain data, only live captures, so there was
-- nothing to filter against yet. terminal_snapshots DOES carry a `source`
-- STRING column ("source URL or 'seed:story'"), but tariff_snapshots does
-- not mirror it in this bundle's migration.
--
-- Per CLAUDE.md ("drift from spec is a flag, not a silent fix"): this query
-- does NOT invent a `source` column that doesn't exist, and does NOT
-- pretend to implement the seed-exclusion filter against a column that
-- isn't there. As written below, it counts entirely live data (Session 3
-- seeded no story-domain rows), so the gap is currently harmless in practice — but
-- once Bundle 0/story-domain seeding exists and starts writing tariff
-- snapshot rows with source='seed:story', THIS QUERY WILL NEED an added
-- join/filter (e.g. `tariff_snapshots.source != 'seed:story'`, or whatever
-- column Bundle 0's migration adds) or the tile will be inflated by seed
-- data, which is the exact failure Build Gate 5 exists to prevent.
-- ---------------------------------------------------------------------

SELECT count(DISTINCT r.run_date) AS live_days
FROM recordings r
WHERE r.tenant_id = %(tenant_id)s
  AND r.status = 'COMMITTED'
  AND r.carrier_id = %(carrier_id)s
  AND r.lane = %(lane)s;

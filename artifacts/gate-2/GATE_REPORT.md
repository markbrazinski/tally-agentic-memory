# Gate 2 — Sourced Reconstruction — Gate Report

**Branch:** `feat/sourced-reconstruction-v1` (from `main` @ Gate-1 merge `19abe7a`)
**Status:** IMPLEMENTED + TESTED + LIVE MANAGED MCP SPONSOR TRACE PASSED (real read-only MCP read against tally_gate2_iso; 5 hero events; 7/7 COMPLETE)
**Suite:** 738 passed (699 baseline + 39 Gate 2); ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migrations 001–011); protected `defaultdb` verified unchanged (21 tables)

## Requirement matrix

| Requirement | Controlling doc §| Status | Repository evidence | Smallest implementation | Acceptance test | Dependency |
|---|---|---|---|---|---|---|
| Consume the durable START_RECONSTRUCTION task | BE Plan §6.4; Recon Commission §6.1 | PASS | `claim_next_reconstruction_task` leases `task_type='START_RECONSTRUCTION'` reusing the intake lease/fence pattern | Lease filter + fence on current_attempt+lease_owner | `test_reconstruction_repository` (fencing), live positive trace | Intake (Gate 1) |
| Real Managed MCP read; no direct-SQL/fixture/model fallback | BE Plan §2, §6.3; Commission §6.1–6.3 | PASS (LIVE) | `reconstruction_mcp.read_reconstruction_memory` → `CockroachManagedMCP.select_query` only; worker fails closed | Live read-only MCP read (write tool denied) against tally_gate2_iso: 5 hero events, 7/7 COMPLETE (`live-mcp-trace.json`); `test_reconstruction_worker` 6 no-fallback cases; `test_reconstruction_mcp` | none |
| Immutable reconstruction versioning | Commission §5.2 | PASS | `reconstructions` unique(invoice,version) + unique(invoice,fingerprint); replay path | `complete_reconstruction` replays existing version on duplicate | `test_reconstruction_repository::test_duplicate_delivery_replays_existing_version` | none |
| Every event: source/version/provenance + distinct time domains | Commission §5.1–5.2; UX §5.4 | PASS | `reconstruction_events` columns; `validate_events` | occurred/effective/observed/recorded/received columns; server-derived `recorded_before_cutoff` | `test_reconstruction_core` (time domains, source, ownership) | none |
| Knowledge cutoff = received_at; no later event enters | BE Plan §4.1(7); Commission §5.2 | PASS | `validate_events` rejects `recorded > cutoff`; cutoff inherited from task | Cutoff filter in validator + MCP WHERE clause | `test_reconstruction_core::test_event_after_knowledge_cutoff_is_rejected_not_labelled` | Intake (sets received_at) |
| Explicit gaps for missing evidence | Commission §9; UX §7.3 | PASS | `reconstruction_coverage` categorical; `adjudicate_charged_days` INSUFFICIENT_EVIDENCE | Coverage classification; missing_requirements on day | `test_reconstruction_core` (missing gate-out), live negative trace | none |
| Seven charged days, each source-complete | Demo Beat 3–5; Commission §7 (RE-08/11) | PASS | `reconstruction_charged_days` | Boundary + per-day adjudication | Live positive trace 7/7 COMPLETE; `test_seven_days_all_source_complete` | none |
| Deterministic boundary in scenario timezone | Commission §5.2, §7.3 | PASS | `resolve_charge_boundary` uses `America/Los_Angeles` | tz-aware `.date()` (bug fixed) | `test_boundary_uses_scenario_timezone_not_utc` (regression) | none |
| Durable lease/fence/retry/events/outbox/SSE | BE Plan §4; Commission §10.3 | PASS | Reuses `workflow_tasks`/`_attempts`/`invoice_events`/`event_outbox`; `_insert_recon_event` writes event+outbox atomically | Reuse intake spine helpers | `test_reconstruction_repository` (atomic, fence, fail, retry) | Intake spine |
| MCP/source failure blocks or exposes gaps | Commission §6.3, §13 | PASS | `fail_reconstruction` BLOCKED/NEEDS_EVIDENCE; view excludes unverified source | Fail-closed worker + verified-only view | Live negative trace; worker unit no-fallback | none |
| Public payloads leak no private identifiers | BE Plan §4.1(10); Handoff §16 | PASS | `reconstruction_api` projection returns state/refs only; `validate_public_event` firewall | Projection excludes S3/SQL/correlation | `test_reconstruction_projection::test_projection_has_no_private_identifiers`; live trace JSON scanned | none |
| Exactly one downstream contract for Gate 3/4 | BE Plan §5 ownership; Commission §3 | PASS | `GET /api/invoices/{id}/reconstruction` | Single projection endpoint | `test_reconstruction_projection::test_projection_shape_is_downstream_contract` | consumed by Gate 3/4 |
| Additive, restartable, non-destructive migrations | AGENTS.md; CLAUDE.md §9 | PASS | `010_sourced_reconstruction.sql`, `011_reconstruction_memory_view.sql` (all IF NOT EXISTS) | Additive DDL | Applied 001–011 to blank `tally_gate2_iso`; readback 36+2 objects | none |
| Tenant scoping on every object/query | AGENTS.md; CLAUDE.md §10 | PASS | tenant-first PK on every Gate-2 table; DAL binds tenant first | tenant-scoped keys | schema readback; negative trace tenant scope | none |

## Deployed proof (isolated `tally_gate2_iso`, real CockroachDB)

- **Positive** (`live-positive-trace.json`): 5 events accepted, 0 rejected, reconstruction version 1 state **COMPLETE**, 7 charged-day rows all SOURCE_COMPLETE, 1 public reconstruction event + 1 outbox row read back. `mock_fallback:false`.
- **Negative** (`live-negative-trace.json`): GATE_OUT source `UNAVAILABLE` → MCP view returns 4 rows (excludes it) → boundary not formed → 0 source-complete days → terminal **NEEDS_EVIDENCE** → `no_complete_reconstruction:true`. `mock_fallback:false`.
- **Bug caught live:** UTC-truncation in the boundary date math dropped June 8 (6/7 days). Fixed to compute calendar dates in the scenario timezone; regression-tested.

## Deferrals

1. **Live Managed MCP sponsor trace** — **PASSED (live).** A real read-only Managed
   MCP read ran against the isolated `tally_gate2_iso` database (the same cluster
   MCP with the database overridden to the isolated lineage — Managed MCP scopes to
   a cluster, the database is a per-query parameter). The identity was verified
   read-only (write tool denied), the fixed reconstruction SELECT returned the hero
   events, the deterministic validator accepted exactly the 5 hero events, and
   reconstruction reached 7/7 COMPLETE (`live-mcp-trace.json`, `mock_fallback:false`).
   Reproduce: `python -m scripts.gate5b_oauth_bootstrap` (one-time browser re-auth),
   then `python -m scripts.gate2_live_mcp_trace`. The earlier driver-diagnostic
   read is retained as a fallback-behavior oracle only.

   **Deploy note:** the browser step uses a `127.0.0.1` OAuth *callback* — a
   laptop-only, one-time login artifact. Runtime MCP reads go to the public
   `https://cockroachlabs.cloud/mcp` endpoint; App Runner reads the sealed token
   from SSM and auto-refreshes it, so no localhost is involved in the deployed path.
2. **Gate 3+ fields** — `applicable_rate_minor`/`outcome`/`dispute_amount_minor` intentionally null/PENDING in Gate 2.

## No-fallback statement (Gate 2 scope)

No fixture, direct-SQL, embedded-constant, model, or current-object fallback produces a successful reconstruction. The MCP read is the only memory path; its failure blocks. Proven by 6 worker no-fallback unit cases and the live negative trace. (The cross-cutting "MCP, S3, vector binding, and no fallback without private identifiers" statement remains OWNED BY Gate 6 and stays OPEN.)

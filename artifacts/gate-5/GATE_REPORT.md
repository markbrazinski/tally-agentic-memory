# Gate 5 — Human Authority and Seal — Gate Report

**Branch:** `feat/authority-seal-v1` (stacked on `feat/deterministic-judgment-v1`)
**Status:** IMPLEMENTED + TESTED + LIVE SEAL TRACE PASSED (approve/seal/stale/replay)
**Suite:** 795 passed (was 783); ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migration 014); protected `defaultdb` verified unchanged (21 tables)

## Requirement matrix

| Requirement | Controlling doc §| Status | Evidence | Acceptance test | Dependency |
|---|---|---|---|---|---|
| Approval bound to one exact immutable recommendation version | Handoff §13; BE Plan Gate 5 | PASS (LIVE) | `approvals.recommendation_version` + `recommendation_digest`; FOR UPDATE lock | Live trace seals v1; `test_approve_binds_and_seals_atomically` | Gate 4 |
| Authenticated or explicitly synthetic-demo approver | Handoff §13 | PASS | `approver_kind='SYNTHETIC_DEMO'`, `AuthedActor` (rachel.martinez) via `require_auth` | approve route `Depends(require_auth)` | Gate 0 auth |
| Idempotency key | Handoff §13 | PASS (LIVE) | `approvals` PK `(tenant, idempotency_key)`; replay returns existing | Live `idempotent_replay_same_seal:true`; `test_idempotent_replay...` | none |
| Optimistic concurrency (ETag) + stale rejection | Handoff §13 | PASS (LIVE) | `If-Match` → expected version+digest; `is_stale` check | Live `stale_rejected:true`; `test_stale_version_rejected`, `test_stale_digest_rejected` | none |
| Safe repeated approval replay (no duplicate active decision) | Handoff §13 | PASS (LIVE) | idempotent path; `seal_count==1` after replay | Live `seal_count_after_replay:1` | none |
| Concurrent approval conflict handling | Handoff §13 | PASS | FOR UPDATE + SERIALIZABLE (`run_with_retry` 40001 retry); same-key-different-request → conflict | `test_same_key_different_request_conflicts` | none |
| Atomic seal binding recommendation + 7 judgments + rule + reconstruction + claim set + exact source + approver + timestamps/revision | Handoff §13; BE Plan Gate 5 | PASS (LIVE) | `decision_seals.bound_object_refs`; one txn | Live `bound_input_types` ⊇ {recommendation, reconstruction, claim_set, applicable_rule}; `test_approve_binds_and_seals_atomically` | Gates 2/3/4 |
| No edit-in-place after sealing | Handoff §13; CLAUDE.md §12 | PASS (LIVE) | unique(recon) seal; correction = new version + new seal | Live one seal despite replay; `test_idempotent_replay...` | none |
| HLC proof timestamp distinct from data timestamp | TDD D1; CLAUDE.md | PASS | `sealed_txn_ts DECIMAL = cluster_logical_timestamp()`; `sealed_at TIMESTAMPTZ = now()` | live trace (seal committed) | none |
| In-transaction audit row | AGENTS.md (audit on every action) | PASS (LIVE) | `query_log` INSERT inside the seal txn | Live `in_transaction_audit_rows:1`; `test_...atomically` (audit==1) | none |
| Public receipt exposes safe facts, not private source | Handoff §13; BE Plan §4.1(10) | PASS | seal projection returns digest/refs/approver, not source_locator | `load_seal_projection` (no private ref) | none |
| Additive migration | AGENTS.md; CLAUDE.md §9 | PASS | `014_authority_seal.sql` | applied to isolated DB | none |

## Deployed proof (isolated `tally_gate2_iso`, live CockroachDB)

`live-seal-trace.json`: a frozen DISPUTE recommendation was approved and sealed in
one SERIALIZABLE transaction — the seal bound recommendation + reconstruction +
claim-set + applicable-rule (revision 1, real `sha256:` digest). A repeated
approval with the same idempotency key returned the **same seal** (`seal_count=1`,
no second seal). A stale-digest approval was **rejected**. One in-transaction audit
row was written. The invoice advanced to `DISPUTED`. `mock_fallback:false`.

## Deferrals

- Correspondence drafting + second authorization + controlled send are Gate 6 (OPEN).
- The cross-cutting "Verification shows MCP, S3, vector binding, and no fallback
  without private identifiers" statement remains OWNED BY Gate 6 and stays OPEN.

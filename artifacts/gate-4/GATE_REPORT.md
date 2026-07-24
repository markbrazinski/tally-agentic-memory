# Gate 4 — Deterministic Judgment — Gate Report

**Branch:** `feat/deterministic-judgment-v1` (stacked on `feat/applicable-rule-v1`)
**Status:** IMPLEMENTED + TESTED + LIVE TRACE PASSED (all three locked outcomes + deterministic replay)
**Suite:** 783 passed (was 766); ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migration 013); protected `defaultdb` verified unchanged (21 tables)

## Requirement matrix

| Requirement | Controlling doc §| Status | Evidence | Acceptance test | Dependency |
|---|---|---|---|---|---|
| One independently-explainable judgment per charged day | Demo Beat 5; Handoff §12 | PASS (LIVE) | `charged_day_judgments` (one row/day, explanation) | Live trace `judgment_rows:7`; `test_hero_seven_day_700_dispute` | Gate 2 days |
| Integer minor-unit arithmetic only | BE Plan §4.1(1); Handoff §12 | PASS | `src/core/judgment` pure int subtraction | `test_rounding_boundary_minor_units_exact` | none |
| Locked seven-day $700 dispute | Demo Beat 5; Handoff §12 | PASS (LIVE) | 7 × ($350−$250) = $700 | Live trace `disputed_minor:70000`; `test_hero_seven_day_700_dispute` | Gate 3 rule |
| Independent valid $875 supported/approved-for-payment | Demo Beat 9; Handoff §12 | PASS (LIVE) | $125=$125 × 7 → APPROVE_FOR_PAYMENT | Live trace `restraint.supported_minor:87500`; `test_restraint_875_approve_for_payment` | none |
| Missing-evidence → REQUEST_EVIDENCE (not dispute) | Demo Beat 5; Handoff §12 | PASS (LIVE) | any insufficient day → REQUEST_EVIDENCE | Live trace `missing_evidence.type:REQUEST_EVIDENCE`; `test_missing_evidence_requests_evidence_not_dispute` | none |
| One frozen immutable recommendation version | Handoff §12; BE Plan §4 | PASS | `recommendations` unique(recon,version)+unique(recon,fingerprint), state FROZEN | `test_idempotent_replay_no_second_freeze` | none |
| Complete source/input bindings | Handoff §12 | PASS | judgment rows bind charged_day + applicable_rule; recommendation binds reconstruction + rule | schema FKs; live readback | Gates 2/3 |
| Deterministic replay | Handoff §12 | PASS (LIVE) | fingerprint + digest | Live trace `deterministic_replay.same_version:true`; `test_deterministic_replay_identical_digest` | none |
| Models never perform authoritative arithmetic | AGENTS.md; BE Plan §2 | PASS | all amounts computed in `src/core/judgment` Python | live trace `model_arithmetic:false` | none |
| Currency/unit mismatch, partial coverage, stale revision handled | Handoff §12 | PASS | `resolve_recommendation` currency check; INSUFFICIENT on partial; version supersession | `test_currency_mismatch_fails_closed`, `test_missing_evidence...` | none |
| Durable lease/fence/idempotency | BE Plan §4 | PASS | reuses task spine; fingerprint replay; lease fence | `test_late_worker_fenced`, `test_idempotent_replay_no_second_freeze` | task spine |
| Additive migration | AGENTS.md; CLAUDE.md §9 | PASS | `013_deterministic_judgment.sql` | applied to isolated DB | none |

## Deployed proof (isolated `tally_gate2_iso`, live CockroachDB)

`live-judgment-trace.json`: three reconstructions seeded and judged live —
**hero DISPUTE $700** (70000 minor, 7 independent judgment rows), **restraint
APPROVE_FOR_PAYMENT $875** (87500 minor), **missing-evidence REQUEST_EVIDENCE**.
Re-running the hero judgment returned the **same recommendation_id and version**
(idempotent freeze; no second version). `model_arithmetic:false`,
`mock_fallback:false`. The frozen digest is `sha256:`-prefixed.

## No-fallback / no-model-arithmetic statement

Every amount is Python integer arithmetic over persisted Gate 2/3 rows; the model
writes no number. Missing coverage or applicable rate yields REQUEST_EVIDENCE, never
a fabricated dispute. Currency mismatch fails closed.

## Deferrals

- Human approval + seal binding this frozen recommendation is Gate 5 (OPEN).

# Gate 3 — Applicable Rule via Distributed Vector Indexing — Gate Report

**Branch:** `feat/applicable-rule-v1` (stacked on `feat/sourced-reconstruction-v1`)
**Status:** IMPLEMENTED + TESTED + LIVE SPONSOR TRACE PASSED (real Titan + real CockroachDB VECTOR index)
**Suite:** 766 passed (was 755 Gate-2 base); ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migration 012); protected `defaultdb` verified unchanged (21 tables)

## Requirement matrix

| Requirement | Controlling doc §| Status | Repository evidence | Acceptance test | Dependency |
|---|---|---|---|---|---|
| Hero query uses real Distributed Vector Indexing | Demo Beat 4; Commission §8.2, RE-09 | PASS (LIVE) | `applicable_rule_worker` → `CockroachClauseVectorSearch.search` forces `tariff_clauses@tariff_clause_embedding_search_idx` | Live trace `vector_index_selected:true` via EXPLAIN | Gate 2 reconstruction |
| Retrieval persisted separately from applicability | Commission §8.3 firewall | PASS | `rule_retrieval_runs` + `rule_candidates` (rank/distance) vs `applicable_rules` (only if VERIFIED) | `test_applicable_rule_repository` (rejected writes candidate, no rule) | none |
| Independent validation: exact text/rate/currency/unit/effective-date/scope/source-version/supersession | Commission §8.3 (10 validators), §13.5 | PASS | `src/core/applicable_rule.validate_candidate` | `test_applicable_rule_core` (each mutation rejects) | none |
| Wrong-date candidate retrieves but is rejected deterministically | Demo Beat 4 guardrail; Commission RE-10, §13.5 | PASS (LIVE) | `_covers_all_dates`; rejection `REJECTED_WRONG_DATE` | Live trace: distractor `Clause 9.9` ranks #1, rejected; `Clause 4.2` accepted. `test_wrong_date_distractor_rejected` | none |
| Vector similarity alone never decides the rule | Demo Beat 4; Commission §8.3 | PASS (LIVE) | `decide_applicable_rule` consults validators only, never distance | Live trace (top-ranked distractor rejected) | none |
| Conflicting accepted rates → CONFLICTED (request evidence) | Commission §8.3 | PASS | `decide_applicable_rule` distinct-rate check | `test_decide_conflicting_rates_is_conflicted`; `test_conflicting_rates_writes_no_rule` | none |
| Vector unavailable → no embedded-clause/fixture fallback | Commission §13.4 | PASS | worker `VectorSearchError`/embed failure → `fail_rule` (NEEDS_EVIDENCE); no rule row | `test_vector_unavailable_fails_closed`, `test_embedding_failure_fails_closed`, `test_empty_hits_refuses` | none |
| Applicable rate stamped on charged days + bound | Commission §5.3; Demo Beat 4 | PASS | `applicable_rate_minor` UPDATE + `charged_day_rule_bindings` | `test_verified_persists_rule_and_stamps_rate` | Gate 2 days |
| Durable lease/fence/idempotency | BE Plan §4 | PASS | reuses task spine; fingerprint replay; lease fence | `test_idempotent_replay_on_fingerprint`, `test_late_worker_fenced` | task spine |
| Public projection exposes rule state, not private locator | BE Plan §4.1(10) | PASS | `reconstruction_api` applicable_rule block (no source_locator) | `test_projection_includes_applicable_rule_when_verified` | none |
| Additive migration | AGENTS.md; CLAUDE.md §9 | PASS | `012_applicable_rule.sql` (all IF NOT EXISTS) | applied to isolated DB, readback 4 tables | none |

## Deployed proof (isolated `tally_gate2_iso`, REAL sponsor path)

`live-vector-trace.json`: Amazon Titan (`amazon.titan-embed-text-v2:0`) embedded the hero query + two seeded clauses; the real CockroachDB VECTOR index `tariff_clause_embedding_search_idx` was **selected** (EXPLAIN `uses_named_vector_index:true`). The wrong-date distractor `Clause 9.9` (same $250 rate, effective 2026-07-01) **ranked first by vector distance** yet was **rejected `REJECTED_WRONG_DATE`**; the correctly-dated `Clause 4.2` ($250, effective 2026-06-01) was accepted at 25000 minor. `decision_state: VERIFIED`, `mock_fallback: false`. This is a stronger proof than a driver diagnostic: CockroachDB's Distributed Vector Indexing is the sponsor tech and it ran live end to end.

## No-fallback statement (Gate 3 scope)

No embedded clause, fixture, or direct exact-key lookup substitutes for the vector path. Vector/embedding unavailability fails closed to NEEDS_EVIDENCE. Proven by three worker no-fallback unit cases and the live trace's honest index-selection assertion.

## Deferrals

- Gate 4 fields (`outcome`, `dispute_amount_minor`) intentionally unset; Gate 3 stamps `applicable_rate_minor` only.

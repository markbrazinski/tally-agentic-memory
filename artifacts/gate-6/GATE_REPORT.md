# Gate 6 — Gated External Action — Gate Report

**Branch:** `feat/gated-send-v1` (stacked on `feat/authority-seal-v1`)
**Status:** IMPLEMENTED + TESTED + LIVE GATED-SEND TRACE PASSED (controlled inbox). **Real external send to an owner-approved recipient/provider: BLOCKED / DEFERRED** (no approved recipient supplied).
**Suite:** 815 passed (was 795); ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migration 015); protected `defaultdb` verified unchanged (21 tables)

## Requirement matrix

| Requirement | Controlling doc §| Status | Evidence | Acceptance test | Dependency |
|---|---|---|---|---|---|
| Drafting uses only the sealed record | Handoff §14; Demo Beat 7 | PASS (LIVE) | `draft_from_sealed` reads only `load_sealed_fact_pack` | `test_correspondence_core`; live trace drafts from seal | Gate 5 |
| Locked-field validation (model can't change money/dates/ids/decision) | Handoff §14 | PASS | `validate_draft_locked_fields` (canonical compare) | `test_draft_with_changed_amount_rejected`, `..._identifier_rejected`; live `SEND_BLOCKED_LOCKED_FIELDS` path | none |
| Second human authorization | Handoff §14; Demo Beat 7 | PASS (LIVE) | `SECOND_AUTHORIZATION` gate; `send_attempts.second_approver_display` | `test_missing_second_authorization_blocks`; live sent with second approver | Gate 0 auth |
| Fresh MCP / vector-binding / exact-source / no-fallback checks | Handoff §14; Demo Beat 8 | PASS (LIVE) | injected fresh `gate_checks`; per-gate `send_gate_runs` | live trace: EXACT_S3_SOURCE + VECTOR_CLAUSE_BINDING real DB reads verified | Gates 2/3 |
| A failed MCP/vector/source/no-fallback check prevents send | Handoff §14; Demo Beat 8 | PASS (LIVE) | `evaluate_send_gates` all-must-pass; provider not called when blocked | live `forced_source_failure_blocked:true`, `SEND_BLOCKED_SOURCE`; `test_failed_*_gate_blocks*` | none |
| One send-attempt record; provider retry idempotent, no duplicate delivery | Handoff §14 | PASS (LIVE) | `send_attempts` PK idempotency_key + unique provider key; provider idempotency | live `distinct_provider_messages:1`, `replay_same_message:true`; `test_idempotent_replay_no_duplicate_send`, `test_provider_idempotency_no_duplicate_delivery` | none |
| Controlled provider call + acknowledgement | Handoff §14; Demo Beat 7 | PASS (LIVE, controlled inbox) | `DemonstrationInboxProvider` → `demo-` message id, `CONTROLLED_DEMONSTRATION_INBOX` | live `sent.state:SENT`, `message_id:demo-...` | none |
| No claim of carrier receipt/acceptance/payment | Handoff §14 | PASS | `ACK_DISCLAIMER`; send summary wording | `test_all_gates_pass...` (disclaimer) | none |
| Additive migration | AGENTS.md; CLAUDE.md §9 | PASS | `015_gated_send.sql` | applied to isolated DB | none |

## Deployed proof (isolated `tally_gate2_iso`, live CockroachDB)

`live-gated-send-trace.json`: a real sealed DISPUTE decision was drafted from the
sealed fact pack and sent to the controlled demonstration inbox — all six fresh
gates verified (EXACT_S3_SOURCE + VECTOR_CLAUSE_BINDING were real isolated-DB
reads), `demo-` acknowledgement returned. A repeated send was **idempotent** (same
message, `distinct_provider_messages:1`, no duplicate delivery). A **forced
EXACT_S3_SOURCE failure blocked the send** (`SEND_BLOCKED_SOURCE`) and the provider
was never called. `mock_fallback:false`.

## BLOCKER / DEFERRAL — real external send

Per the Gate 6 commission: **no owner-approved controlled recipient/provider was
supplied**, so the exact external action — a send to a real external mailbox — was
**NOT performed**. All other Gate 6 implementation and testing is complete. The
controlled demonstration inbox (`DemonstrationInboxProvider`, in-process) proves
the gated-send path end to end without contacting any external address. Sending to
an arbitrary or inferred address was deliberately not done.

**To close the external-send blocker:** the owner supplies an approved controlled
provider + recipient; the `DemonstrationInboxProvider` is swapped for that
provider behind the same gated `approve_and_send` path (which already runs the
fresh MCP/vector/source/no-fallback + second-authorization gates before any
provider call).

## MCP / S3 / vector / no-fallback statement

This gate implements the action-gating path that owns the statement "Verification
shows MCP, S3, vector binding, and no fallback without private identifiers." The
EXACT_S3_SOURCE, VECTOR_CLAUSE_BINDING, and NO_FALLBACK gates run fresh and block
on failure (proven live). The APPROVED_MEMORY_MCP gate is currently a bounded
reconstruction-state check because the isolated Managed MCP endpoint is not
provisioned (same boundary as Gate 2); the live Managed MCP sponsor read remains
the one deferred sub-check. The statement is therefore **PARTIALLY CLOSED**:
S3 + vector + no-fallback proven live; live MCP read deferred with the isolated
MCP endpoint.

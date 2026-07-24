# Gate 7 — Public Hero — Gate Report

**Branch:** `feat/public-hero-v1` (stacked on `feat/gated-send-v1`)
**Status:** BACKEND RELEASE PROOF COMPLETE (integrated hero loop + 3 clean rehearsals + privacy scan). **Frontend UI, video/Devpost, live Managed MCP read, and real external send remain the integrated/deferred dependencies.**
**Suite:** 815 passed; ruff clean on changed files
**Isolated env:** `tally_gate2_iso` (migrations 001a–015); protected `defaultdb` verified unchanged (21 tables)

## Scope split

Gate 7 is an integrated backend/frontend/release gate. The **backend + release**
responsibilities are owned here. The **frontend UI** (queue/workbench/SSE surfaces)
is a separate workstream per the locked UX/IA and AGENTS.md scope locks; the
**video/Devpost** artifacts and the **live Managed MCP** / **real external send**
are deferred dependencies.

## Requirement matrix (backend + release)

| Requirement | Controlling doc §| Status | Evidence |
|---|---|---|---|
| Full positive trace (integrated hero loop) | Handoff §15; Demo Beats 2–8 | PASS (LIVE) | `integrated-trace.json`: intake claims → reconstruction COMPLETE → rule VERIFIED (real vector index) → judgment DISPUTE $700 → seal → send SENT → `DISPUTED`. `mock_fallback:false` |
| Every visible claim derives from executed server state | Handoff Gate 7 exit; BE Plan §8 | PASS | all projection values read from persisted rows; no fixture/animation path (proven across Gates 2–6 no-fallback tests) |
| Valid-invoice restraint trace | Demo Beat 9; Handoff §15 | PASS (LIVE) | Gate 4 `$875` APPROVE_FOR_PAYMENT live trace (`artifacts/gate-4/`) |
| Negative send-gating trace | Demo Beat 8; Handoff §15 | PASS (LIVE) | Gate 6 forced source-failure blocks send (`artifacts/gate-6/`) |
| Three consecutive clean rehearsals | Handoff §15 | PASS (LIVE) | `g7_1.json`, `g7_2.json`, `g7_3.json` — all exit 0, `DISPUTED` |
| Reset/reseed procedure | Handoff §15; BE Plan Gate 0 | PASS | `gate7_integrated_trace.reset()` (idempotent per-tenant); RUNBOOK.md |
| Representative-data disclosure | Handoff §15; Current Truth §4 | PASS | every trace tagged `SYNTHETIC DEMO — FICTIONAL DATA`; `provenance_class:DEMO_SCENARIO`; source disclosure "Representative demonstration data" |
| Privacy scan (no private identifiers) | Handoff §15; §16 | PASS | `gate7_privacy_scan.py` — 321 files, **clean**; redacted the cluster hostname from the Gate-2 manifest |
| Public-safe logs/diagnostics | Handoff §15 | PASS | `validate_public_event` firewall on every event; per-gate projections expose state/refs only |
| Deployment/readiness/teardown procedure | Handoff §15 | PASS | RUNBOOK.md (migrations, run, scan, teardown) |
| Additive, restartable migrations | AGENTS.md; CLAUDE.md §9 | PASS | migrations 001a–015 apply to a blank isolated DB; readback verified |
| Public README matches live demo | Handoff §15 | PARTIAL | README still describes the older deployed read-only hero; reconciling it is part of the integrated frontend/public-hero work (flagged, not silently rewritten) |
| Logged-out judge path (browser) | Handoff §15 | DEFERRED (frontend) | backend logged-out projections exist (Gate 1); the browser queue/workbench is the frontend workstream |
| Unauthenticated clone/install/build/link validation | Handoff §15 | DEFERRED (release) | requires the public repository publication decision + the frontend build |
| Video + Devpost artifacts | Handoff §15 | DEFERRED | recording is produced against the finished frontend |

## Deployed proof (isolated `tally_gate2_iso`, live CockroachDB)

`integrated-trace.json` runs the entire hero loop for one fresh invoice in a
single script against real CockroachDB. Sponsor tech exercised **live**:
CockroachDB persistence, **Distributed Vector Indexing (named index selected)**,
Amazon Titan embeddings, Amazon Bedrock. Terminal: `DISPUTED`, 5 public events,
`mock_fallback:false`. A status-ordering bug (a successful send regressing the
sealed outcome to `READY_TO_SEND`) was **caught by the integration** and fixed —
the send now preserves the sealed `DISPUTED` outcome.

## BLOCKERS / DEFERRALS

1. **Frontend UI** (queue insertion without hard refresh, workbench, deep links,
   evidence drawer, keyboard/responsive, SSE reconnect) — the separate frontend
   workstream against the frozen `GET /api/invoices/{id}/reconstruction` +
   approve/send contracts. Not started here per scope locks.
2. **Live Managed MCP read** — isolated MCP endpoint not provisioned; reconstruction
   memory is read driver-diagnostic. Needs an isolated MCP endpoint + OAuth.
3. **Real external send** — no owner-approved recipient/provider; controlled
   demonstration inbox only.
4. **Video / Devpost / public README reconciliation / unauthenticated clone-build**
   — release artifacts produced once the frontend is integrated and the
   publication decision is made.

## Gate 7 exit assessment

Backend: **the integrated hero loop derives every visible value from executed
server state, runs repeatably (3/3 clean), and passes the privacy scan.** The gate
does not fully PASS until the frontend judge path, the recording, and the
publication validation are complete — those are the named remaining integrated
dependencies.

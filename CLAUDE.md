# Tally — CLAUDE.md

This file encodes the current implementation standards for the remaining product
gates (Gates 2–7). It **complements** `AGENTS.md` and never weakens or
contradicts it. Where `AGENTS.md` and this file both speak, both bind; where they
appear to differ, the stricter rule wins. `AGENTS.md` is owner-governed and must
not be modified without separate owner authorization.

## Controlling authority and reading order

The authoritative planning packet lives in the private coordination checkout.
Read it in this exact order before changing anything:

1. `docs/TALLY_CURRENT_TRUTH.md`
2. `docs/Tally_Locked_Demo_Script_v1.md`
3. `docs/Tally_UX_Audit_and_IA_v1.md`
4. `docs/Tally_BE_Execution_Plan_v1.md`
5. `docs/Tally_BE_Intake_Orchestration_Commission.md`
6. the Reconstruction/Evidence commission
7. the Decision/Correspondence commission

Then read this repo's `AGENTS.md`, this file, the intake handoff, the intake
execution manifest and teardown, migrations 007–009, and the intake modules.

## Standards (binding)

1. **Executed current truth beats proposed architecture.** A document that
   proposes work does not prove it. Only executed, read-back state is truth.
2. **The locked demo controls narrative scope and visible outcomes.** Do not add
   or remove visible product behavior the locked demo does not show.
3. **The UX/IA controls nouns, states, routes, and interactions.** Match its
   exact vocabulary (canonical nouns, state enums, route patterns) precisely.
4. **The backend execution plan controls dependency order and gate acceptance.**
   Gates proceed in its order; a gate's exit criteria are its acceptance test.
5. **Backend plan §4.1 supersedes conflicting illustrative payloads** in any
   workstream commission (money in `*_minor`, `GET /api/stream`, the event
   envelope, lowercase-namespaced event names, typed/versioned
   `produced_object_refs`, orchestrator-owned sequence, knowledge-cutoff rule).
6. **Work proceeds gate by gate; later gates cannot inherit unexecuted success.**
   A previous gate's evidence proves only the capability it actually exercised.
7. **Every gate requires a real deployed positive path and a counterfactual
   dependency-failure path.** The sponsor dependency must be load-bearing, and a
   forced failure of it must visibly block or expose gaps — never silently pass.
8. **Python/FastAPI/raw SQL remain the implementation conventions.** No ORM, no
   query builder; SQL values are always bound parameters.
9. **Migrations must be additive, restartable, and non-destructive.** They run
   cleanly against a blank recovery database and are idempotently tracked.
10. **Every database object and query must be tenant scoped.** Every key,
    lookup, task, event, source, and API read carries tenant scope; wrong-tenant
    access does not reveal object existence. App-level scoping is not
    database-enforced RLS — do not overclaim it.
11. **Exact source versions must remain exact; no latest-object substitution.**
    Internal records retain exact locator/version/checksum; public APIs return
    only verification state and scoped access. No endpoint falls back to "latest."
12. **Completed claims, reconstructions, recommendations, decisions, drafts, and
    send attempts are immutable and versioned.** Corrections create a new
    version; approved or sealed records are never edited in place.
13. **Money uses integer minor units plus ISO currency.** `$350` is `35000`.
    Public projections may display whole units; persistence stays minor-unit
    integer. Currency/unit compatibility is a deterministic check.
14. **Models do not perform authoritative arithmetic or invent missing
    evidence.** LLM output is schema-validated; a quoted extraction must occur in
    the retained source or the workflow abstains. Python decides verdicts.
15. **Mutations and tasks require durable idempotency fingerprints.** Every
    externally triggered mutation has an idempotency key + request fingerprint;
    every durable task has an input fingerprint; replay returns the existing
    result; a different payload under one key conflicts.
16. **Workers require durable leases, attempts, bounded retries, and late-worker
    fencing.** Expired leases are reclaimable; a worker that lost its lease
    cannot commit; retry counts and backoff are bounded.
17. **Domain transitions, task state, Invoice state, events, and outbox rows
    commit atomically.** Do not claim atomicity across S3/provider and
    CockroachDB — use explicit durable pending/reconciliation states there.
18. **SSE is notification; persisted server snapshots are authoritative.** Events
    are durable and monotonically sequenced per Invoice; `Last-Event-ID` replays;
    an unknown cursor forces snapshot reconciliation; no client timer invents
    progress.
19. **Public payloads and logs must not expose** credentials, tenant IDs,
    database details, storage bindings (bucket/key/version), raw private hashes,
    SQL, prompts, raw model responses, or provider internals. Expose safe state,
    summary, source label, verification status, and recovery guidance.
20. **No fixture, direct-SQL, embedded-constant, model, current-object, mock
    provider, or client-animation fallback may produce a successful gate.** A
    required Managed MCP read has no direct-SQL fallback; a required vector
    retrieval has no embedded-clause fallback; a missing exact source becomes a
    gap, not a substituted current object.
21. **Every gate requires positive, negative, privacy, logged-out, restart,
    retry, and reconciliation evidence appropriate to its scope.** Fallback paths
    are not real unless tested.
22. **Historical Gate 5 evidence is reusable only as historical regression or
    public-projection evidence**, never as proof of the new intake,
    reconstruction, vector, approval/send, or valid-invoice paths.
23. **Gate work may be merged into main only after its acceptance tests and
    deployed proof pass.** Use PRs; never force-push shared history or push
    feature work directly to main.
24. **`AGENTS.md` must remain unchanged unless the owner separately authorizes
    it.**
25. **Progress reports use only:**

    ```
    STATUS
    CHANGED
    VERIFICATION
    BLOCKERS/DEFERRALS
    NEXT ACTION
    ```

## Definition of done (per gate)

The gate's implementation, additive migrations, deterministic domain tests, and
transaction/retry/concurrency/ownership integration tests are complete and
green; the real sponsor path and its counterfactual failure both ran; no
fallback produced success; durable and deployment state were read back; public
responses/events/logs/artifacts were scanned for private data; a public-safe
execution manifest and approval-gated teardown plan exist; the branch is pushed,
the PR opened/updated, and the gate merged only after it passes. Later gates stay
explicitly OPEN.

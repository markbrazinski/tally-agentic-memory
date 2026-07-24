# Tally backend implementation handoff after Live Intake

**As of:** 2026-07-23  
**Audience:** The next lead backend implementation agent  
**Repository:** `markbrazinski/tally-agentic-memory`  
**Current feature branch:** `feat/intake-orchestration-v1`  
**Current handoff commit before this document:** `1154c04`  
**Draft pull request:** `#1`, targeting `main`  
**Base revision:** `746f2e4`  
**Current program position:** Gate 1 backend complete; Gates 2–7 remain  
**Classification:** Public-safe engineering coordination. No credentials,
private source bindings, database connection details, tenant identifiers, or
private infrastructure identifiers belong in this document.

---

## 1. What the next agent is taking over

Tally is a hackathon product that evaluates ocean-freight demurrage invoices by
reconstructing what happened before the invoice arrived.

The locked demo is not the older read-only recovery tour. Its intended live
flow is:

```text
representative PDF arrives
→ exact original source is retained
→ carrier claims are extracted
→ pre-invoice shipment memory is reconstructed
→ each charged day receives source coverage
→ a tariff candidate is retrieved and independently validated
→ deterministic policy produces a recommendation
→ a human approves one frozen recommendation
→ the decision is atomically sealed
→ correspondence is drafted only from the sealed record
→ a second human authorization occurs
→ fresh dependency/action gates run
→ a controlled demonstration send may occur
→ an independent valid-invoice case proves restraint
```

The completed work in this branch establishes the real beginning of that flow:

```text
multipart PDF
→ exact versioned source preservation
→ stable Invoice
→ durable extraction task
→ real Bedrock extraction
→ validated anchored claims
→ durable events/outbox/SSE
→ exactly one reconstruction task
```

The next backend implementation objective is **Gate 2 — Sourced
reconstruction**. Do not jump directly to vector retrieval, judgment, approval,
or correspondence. Gate 2 must first reconstruct a truthful, source-bound
pre-invoice event timeline through the approved Managed MCP path.

---

## 2. Authority and required reading order

The authoritative planning packet currently lives in the private coordination
checkout, not in this public implementation repository. Read it in this exact
order before proposing Gate 2 changes:

1. `docs/TALLY_CURRENT_TRUTH.md`
   - Actual implemented and externally observed truth.
   - Do not treat proposed work as complete.
   - Its July 22 gate ledger predates the new Intake implementation; reconcile
     it with this handoff and the committed execution manifest.
2. `docs/Tally_Locked_Demo_Script_v1.md`
   - Locked narrative, amounts, timing, visible claims, and demo outcome.
3. `docs/Tally_UX_Audit_and_IA_v1.md`
   - Locked nouns, states, information architecture, interaction semantics,
     public/private boundaries, and frontend/backend responsibilities.
4. `docs/Tally_BE_Execution_Plan_v1.md`
   - Controlling dependency order, workstream ownership, integration gates,
     and claim-risk proof matrix.
5. `docs/Tally_BE_Intake_Orchestration_Commission.md`
   - The bounded commission completed by the current branch.
   - Read it to understand the cross-agent reconstruction handoff and the
     durability contracts Gate 2 inherits.
6. The bounded Gate 2 Reconstruction/Evidence commission, once supplied.
7. The bounded Decision/Correspondence commission only when Gates 4–6 are
   actually being planned.

### Conflict rules

Use these rules without improvising:

1. Executed current truth beats a proposal.
2. The locked demo controls narrative scope and visible outcomes.
3. The UX/IA controls nouns, states, routes, information architecture, and
   interaction semantics.
4. The backend execution plan controls dependency order and integration gates.
5. Backend plan §4.1 coordination resolutions supersede conflicting
   illustrative payloads in workstream commissions.
6. A bounded commission may narrow work but may not broaden the locked demo.
7. A previous gate's evidence may be reused only for the capability it actually
   exercised.

### Missing commissions

The execution plan names these ready commissions:

- `Tally_BE_Reconstruction_Evidence_Commission.md`
- `Tally_BE_Decision_Correspondence_Commission.md`

They were not present in the inspected coordination checkout or public
implementation repository at handoff time.

The next agent may perform a read-only Gate 2 audit from the locked documents
and repository. It should not infer authorization for new cloud resources,
production mutations, representative source selection, or later external
actions from this handoff. Obtain the bounded Gate 2 commission or an explicit
owner decision before those mutations.

---

## 3. Repository and worktree truth

### Judged implementation repository

- Repository: `markbrazinski/tally-agentic-memory`
- Default branch: `main`
- Reviewed base: `746f2e4`
- Intake branch: `feat/intake-orchestration-v1`
- Draft PR: `#1`
- Latest implementation/evidence commit before this handoff: `1154c04`
- The branch is pushed.
- The PR remains a draft.
- Nothing was merged or pushed directly to `main`.

The existing working copy used for Intake is a temporary Git worktree. A future
agent should not rely on a temporary path surviving indefinitely. Prefer one
of:

1. merge the reviewed Intake PR, then create a clean Gate 2 branch from the new
   `main`; or
2. with explicit owner authorization, create a stacked Gate 2 branch from
   `feat/intake-orchestration-v1`.

Do not silently build Gate 2 on an unmerged draft branch. Record whether the
branch is stacked and what it depends on.

### Remote warning

The original local checkout has historically had a remote associated with the
older private repository. The Intake worktree used a separately named remote
for `markbrazinski/tally-agentic-memory`.

Before any push:

```bash
git remote -v
git branch --show-current
git status --short
git log --oneline --decorate -10
```

Resolve the exact public target before writing. Never push this work to the
older private repository by accident, and never push directly to `main`.

### Private coordination/recovery checkout

The separate `tally-agent` checkout contains private recovery history and the
authoritative planning documents. It is not the judged implementation base.

Use it to read the controlling documents. Do not:

- merge its private recovery branch into the public repository;
- copy private evidence or runtime artifacts into this repository;
- push its history to the public repository;
- assume its old product flow is the locked demo;
- edit its `AGENTS.md`;
- treat its historical gate ledger as proof of the new path.

### Older Phase B worktree

An older local Phase B candidate may still exist as an uncommitted temporary
worktree. It implements the superseded read-only IA. Preserve it, but do not
copy it wholesale into the new invoice workbench or use it as proof of Gates
2–7.

---

## 4. Mandatory repository reading

After the controlling documents, read these implementation files in order.

### Agent and repository conventions

1. `AGENTS.md`
   - Gate-by-gate implementation.
   - Python/FastAPI/raw SQL conventions.
   - Additive migration requirement.
   - Tenant and audit requirements.
   - No-network unit-test expectation.
   - Exact source/version handling.
   - No UI redesign without the proper workstream.
2. `README.md`
   - Existing setup, deployment, and historical architecture.
   - Some hero wording may still describe the older demo; do not use README
     prose to override the locked documents.
3. `pyproject.toml`, `requirements.txt`, and `requirements-dev.txt`
   - Test and lint environment.

### Intake schema and domain

4. `migrations/007_intake_orchestration.sql`
   - Invoice source bindings.
   - Ingestion requests.
   - Extraction runs and immutable claim sets.
   - Durable workflow tasks and attempts.
   - Invoice events and transactional outbox.
5. `migrations/008_intake_retry_idempotency.sql`
   - Durable retry request idempotency.
6. `migrations/009_intake_duplicate_link.sql`
   - Durable cross-idempotency-key duplicate linkage.
7. `src/core/intake.py`
   - PDF envelope, state/task primitives, and deterministic task
     fingerprints.
8. `src/core/intake_claims.py`
   - Claim schema, anchor validation, and missing/invalid behavior.

### Intake persistence and orchestration

9. `src/platform/intake_repository.py`
   - Receipt reservation/finalization transaction.
   - Cross-key SHA deduplication.
   - Stable Invoice/source/task/event/outbox creation.
10. `src/platform/intake_tasks.py`
    - Task leasing, attempts, fencing, retries, extraction completion, and
      atomic reconstruction handoff.
11. `src/platform/intake_worker.py`
    - Exact-source fetch, PDF read, real extraction, validation, and
      fail-closed behavior.
12. `src/platform/intake_runtime.py`
    - Durable worker/outbox runtime loop.
13. `src/platform/intake_events.py`
    - Durable event history, outbox delivery, SSE replay, heartbeat, and
      unknown-cursor reconciliation.
14. `src/platform/intake_api.py`
    - Controlled multipart intake, queue/read/source/retry endpoints, public
      projections, and SSE route.

### External adapters

15. `src/external/invoice_source_store.py`
    - Versioned S3 preservation, exact-version retrieval, length/checksum
      verification, and no-latest fallback.
16. `src/external/intake_bedrock.py`
    - Real bounded extraction call and response handling.
17. `src/external/dal.py` and `src/external/db.py`
    - Tenant-scoped database access and retry conventions.
18. Existing Managed MCP, vector, OAuth, source-verification, deterministic
    receipt, seal, and temporal-replay modules.
    - Identify them with `rg` rather than assuming historical filenames still
      match an old handoff.
    - Treat them as reusable primitives, not already-integrated Gate 2–6 paths.

### Tests and executable evidence

19. `tests/unit/test_intake_repository.py`
20. `tests/unit/test_intake_worker.py`
21. `tests/unit/test_intake_events.py`
22. `tests/unit/test_intake_api.py`
23. `tests/unit/test_intake_tasks.py`, if present in the checked revision.
24. `tests/fixtures/demo/INV-1048.pdf`
25. `tests/fixtures/demo/INV-1048.expected-claims.json`
26. `artifacts/intake-v1/EXECUTION_MANIFEST.json`
27. `artifacts/intake-v1/TEARDOWN.md`
28. `scripts/provision_intake_v1.sh`
29. `scripts/deploy_intake_v1.sh`

The execution manifest is the concise committed truth for the deployed Intake
run. Private bindings and raw execution details were intentionally retained
outside the public repository.

---

## 5. What Gate 1 actually completed

### Product behavior

The branch implements:

- controlled multipart PDF upload;
- byte-based PDF validation and bounded parsing;
- idempotency-key reservation and replay;
- conflict on one key reused for a different payload;
- durable linkage when the same immutable PDF arrives under a new key;
- exact versioned S3 preservation before `RECEIVED`;
- exact-version retrieval with byte/hash verification;
- stable Invoice identity;
- canonical Invoice/source/task/event response;
- durable leased extraction worker;
- bounded retry attempts;
- real Bedrock extraction;
- local deterministic validation;
- exact page/region/excerpt anchors;
- immutable claim-set versioning;
- monotonic Invoice state/event sequencing;
- transactional outbox;
- durable event history;
- aggregate SSE with `Last-Event-ID`;
- unknown-cursor reconciliation;
- retry endpoint with idempotency and optimistic version checks;
- atomic transition to `RECONSTRUCTING`;
- exactly one reconstruction task bound to source version 1 and claim-set
  version 1;
- public-safe projections that omit private infrastructure bindings.

### Deployed positive proof

A fresh, clean isolated deployment executed the committed fictional
`INV-1048.pdf`:

- fresh multipart intake returned `201`;
- receipt response completed in 1.735 seconds;
- same key and payload returned `200` with replay enabled;
- same key and different PDF returned `409`;
- anonymous mutation returned `401`;
- invalid bytes returned `415`;
- exact source retrieval returned `200` in 0.688 seconds;
- returned source bytes matched the committed PDF exactly;
- one real model extraction run completed;
- all 10 expected claims were validated;
- every claim had a page, bounding region, and excerpt binding;
- one immutable claim set became active;
- state progressed through `RECEIVED`, `INITIAL_PROCESSING`, and
  `RECONSTRUCTING`;
- reconstruction handoff occurred 6.185 seconds after receipt;
- exactly one reconstruction task was created;
- four public events had contiguous sequences 1–4;
- all four outbox rows were delivered;
- SSE replay after event 1 returned events 2–4 once;
- an unknown SSE cursor emitted `stream.reconcile_required`.

### Deployed negative proof

The isolated deployed worker was also exercised against controlled failures:

1. **Exact source version unavailable**
   - one non-retryable attempt;
   - Invoice moved to `BLOCKED_SOURCE_VERSION_UNAVAILABLE`;
   - zero extraction runs;
   - zero claim sets;
   - zero reconstruction tasks;
   - no latest-object or model fallback.
2. **Bedrock unavailable**
   - a temporary deny affected only the isolated runtime role;
   - exactly three durable attempts occurred;
   - the task ended in terminal failure;
   - zero extraction runs;
   - zero claim sets;
   - zero reconstruction tasks;
   - the temporary deny was removed and exact lookup confirmed it absent.

### Privacy and logged-out proof

- Logged-out queue read returned `200`.
- Logged-out Invoice read returned `200`.
- Logged-out event history returned `200`.
- Logged-out exact source read returned `200`.
- Wrong Invoice/source pairings returned `404`.
- Documentation and legacy routes returned `404` in Intake mode.
- Public responses, headers, and SSE captures had zero targeted private-field
  matches.
- 82 isolated application log events had zero targeted private identifier,
  token, database-connection, prompt, model-internal, or source-binding
  matches.

### Test state

- Full Python suite: **699 passed**.
- One existing Starlette deprecation warning remains.
- Lint passes for files changed by the Intake work.
- Repository-wide Ruff has nine pre-existing formatting findings in unrelated
  historical tests. Do not silently reformat them as part of Gate 2.
- Shell syntax, JSON parsing, diff checks, and public artifact scans passed.

### Operational state

- The isolated Intake service is **PAUSED**.
- The isolated configuration, database, exact source versions, roles, and image
  repository are retained for review and possible continuation.
- Resource deletion has **not** been executed.
- `artifacts/intake-v1/TEARDOWN.md` is a separately approval-gated checklist.
- The existing hero database, lineage, source objects, and judged routing were
  not modified.

Do not resume, reconfigure, delete, or repurpose retained isolated resources
without confirming the next commission and owner authorization.

---

## 6. Known implementation lessons and traps

These issues were found during Intake. Preserve their fixes.

### 6.1 Same bytes under a different idempotency key

The database already enforced tenant-scoped Invoice SHA uniqueness. A fresh
idempotency key for bytes already known to the tenant originally reached that
constraint and returned `500`.

The fix:

- tenant-scoped lookup by source SHA;
- durable duplicate Invoice/source link on the ingestion request;
- replay of the existing public projection;
- no duplicate Invoice, source, task, event, or claim set;
- no weakening or removal of the unique index.

Do not regress to “catch the unique error and retry blindly,” and do not drop
the uniqueness constraint.

### 6.2 App Runner update timing

An App Runner update can return while the previously RUNNING instance is still
serving old secret values. The first deployment script treated any RUNNING
state as completion.

The fix records the previous update timestamp and waits until the service is
RUNNING with a changed update timestamp.

For a secret/configuration change:

- deploy an immutable new image tag;
- wait for the actual update marker to advance;
- verify behavior from the service, not only the control-plane return code.

### 6.3 Runtime parameter resolution

The isolated runtime needed both singular and batch parameter-read permission.
The committed provisioning policy includes both. Do not remove one based on a
local SDK assumption.

### 6.4 AWS identities are scoped

Local profile aliases can resolve to different identities. One profile could
read runtime data but could not pass the runtime role for App Runner. A
separate scoped deployer role performed deployment.

Before mutation:

```bash
aws sts get-caller-identity --profile <intended-profile>
```

Use the least-privileged identity already configured. Do not ask the owner for
root credentials, persistent access keys, or copied session tokens.

### 6.5 App Runner operation-list permission

The scoped deployer could describe and update the service but could not list
operations or manually start a deployment. The corrected deployment script
uses service readback rather than depending on operation-list access.

Do not expand IAM merely for convenience if a narrower readback already proves
the required state.

### 6.6 Shell portability

- `status` is a read-only variable in the local `zsh`; use a task-specific name
  such as `service_state`.
- The machine's inherited locale can make Perl-based `shasum` fail. OpenSSL was
  used for the bounded live negative proof.
- Keep secrets in process variables only, suppress command output, and scan
  logs/artifacts before commit.

### 6.7 Public versus private evidence

The public repository may contain:

- aggregate counts;
- HTTP status classes;
- public state names;
- timing aggregates;
- fictional fixture paths;
- public commit/PR references;
- boolean no-fallback/privacy outcomes.

It must not contain:

- database connection strings;
- tenant IDs;
- object keys, bucket names, or exact storage versions;
- raw source hashes used as private bindings;
- account IDs;
- credentials or bearer tokens;
- provider request IDs;
- raw prompts/model responses;
- private runtime log bodies;
- private representative-source identifiers.

---

## 7. Gate ledger at handoff

| Gate | Status now | What is proven | What remains |
|---|---|---|---|
| Gate 0 — Platform and lineage | **PARTIAL FOR FULL BUILD** | Intake has isolated authenticated CockroachDB, versioned storage, Bedrock, App Runner, scoped roles/configuration, logs, deployment, and teardown procedure | Gate 2 must re-smoke Managed MCP and representative source access; Gate 3 must re-smoke vector indexing; Gate 6 still needs an explicitly selected controlled mail provider and send authority; new tables need retention/readback decisions |
| Gate 1 — Live intake | **BACKEND PASS; FRONTEND DEPENDENCY OPEN** | Fresh deployed PDF through exact source, real extraction, durable orchestration, events/SSE, reconstruction handoff, failures, privacy, and logged-out backend reads | Browser queue insertion, immediate clickability, workbench rendering while processing, keyboard behavior, and browser reconnect proof belong to the frontend workstream |
| Gate 2 — Sourced reconstruction | **OPEN** | Intake creates one durable `START_RECONSTRUCTION` task with exact source/claim-set references; older MCP primitives exist | Execute a real pre-invoice reconstruction through Managed MCP and persist every event with source, time, version, and provenance |
| Gate 3 — Applicable rule | **OPEN WITH REUSABLE PRIMITIVES** | Historical vector adapter/index and deterministic clause checks exist | Run the actual hero query in the new workflow; persist retrieval separately from applicability; reject wrong-date/scope/rate candidates |
| Gate 4 — Deterministic judgment | **OPEN WITH REUSABLE PRIMITIVES** | Historical deterministic arithmetic and sealing primitives exist | Seven independently sourced day judgments, `$700` dispute, independent `$875` supported path, missing-evidence restraint, frozen recommendation version |
| Gate 5 — Human authority and seal | **OPEN WITH REUSABLE PRIMITIVES** | Historical atomic seal and idempotency patterns exist | Approval of one frozen recommendation version; concurrency/staleness tests; new exact input/source bindings; immutable decision record |
| Gate 6 — Gated external action | **OPEN** | Only reusable historical public-projection/privacy patterns exist | Sealed-record-only draft, second authorization, fresh MCP/vector/source/no-fallback gates, controlled provider acknowledgement, failure-blocked send |
| Gate 7 — Public hero | **OPEN** | Public repository and backend logged-out Intake reads exist | Full frontend workflow, integrated deployed trace, negative send path, logged-out rehearsals, runbook/video/README and publication validation |

There are **six major numbered gates remaining: Gates 2–7**. Gate 1 also has a
frontend completion dependency, and Gate 0 has later provider-specific
prerequisites.

---

## 8. Non-negotiable deferrals

The following statement remains **OPEN**:

> Verification shows MCP, S3, vector binding, and no fallback without private
> identifiers.

Do not mark it complete from:

- the older Gate 5 read-only hero;
- the new Intake exact-source proof;
- historical vector retrieval;
- historical MCP reads;
- public projection/privacy scans.

It belongs to the future **Gate 6 action-gating path**, where every check is run
fresh and a failed check prevents the controlled send.

Also OPEN:

- approval;
- frozen recommendation authorization;
- atomic decision seal for the new object model;
- correspondence drafting;
- second authorization;
- email/provider send;
- provider acknowledgement;
- the independent valid `$875` path;
- full browser/public-hero execution.

Do not let the labels “Gate 5” and “Gate 6” from the older recovery program
confuse the new execution plan's Gates 5 and 6.

---

## 9. How to start the next agent safely

### Step 1 — establish the exact branch strategy

Ask or inspect whether Intake PR `#1` has been reviewed and merged.

- If merged: branch Gate 2 from updated `main`.
- If not merged but stacking is explicitly authorized: branch from
  `feat/intake-orchestration-v1` and record that dependency.
- If neither is true: audit Gate 2 read-only and stop before implementation.

Suggested branch name:

```text
feat/sourced-reconstruction-v1
```

Do not merge the Intake PR, mark it ready, or push directly to `main` without
explicit owner authorization.

### Step 2 — verify the checkout

```bash
git status --short
git remote -v
git branch --show-current
git rev-parse HEAD
git log --oneline --decorate -10
```

Confirm:

- the repository is `tally-agentic-memory`;
- the intended base contains migrations 007–009;
- no unrelated user changes are present;
- `AGENTS.md` remains unchanged.

### Step 3 — establish a clean environment

Follow the repository README for a fresh virtual environment. Then run:

```bash
python -m pytest -q
bash -n scripts/provision_intake_v1.sh scripts/deploy_intake_v1.sh
jq -e . artifacts/intake-v1/EXECUTION_MANIFEST.json
```

Expected Python baseline at this handoff: `699 passed`, plus one existing
Starlette deprecation warning.

Do not use repository-wide Ruff as a surprise scope-expansion trigger. First
run Ruff on files Gate 2 changes and separately record the known unrelated
baseline.

### Step 4 — read and audit before editing

Produce a requirement matrix for Gate 2:

- requirement;
- controlling document and section;
- current status: `PASS`, `PARTIAL`, `OPEN`, or `CONFLICT`;
- exact repository evidence;
- smallest required implementation;
- acceptance test;
- dependency on another workstream.

Do not perform a general architecture or product redesign.

### Step 5 — obtain the bounded Gate 2 commission

Before external mutation, resolve:

- representative event/source package;
- allowed source disclosures;
- exact Managed MCP query boundary;
- allowed database and AWS resources;
- whether to extend the retained isolated environment or create a new one;
- public/private evidence handling;
- acceptance commands;
- cost and teardown authorization;
- Git branch/PR authorization.

### Step 6 — re-smoke dependencies without claiming the gate

Use read-only or isolated checks first:

- scoped identity;
- CockroachDB connectivity;
- Managed MCP authentication and fixed read;
- exact source access;
- current retention/TTL posture for tables Gate 2 will depend on;
- current paused/running state of retained Intake resources;
- cost and teardown controls if cloud mutation is authorized.

A smoke test proves connectivity, not reconstruction.

---

## 10. Gate 2 implementation plan — Sourced reconstruction

Gate 2 passes only when one real trace reads pre-invoice events through Managed
MCP and persists a reconstruction in which every event has source, time,
version, and provenance.

### 10.1 Inputs inherited from Intake

The worker may rely on:

- one stable Invoice ID;
- one exact verified invoice-source reference;
- one active immutable claim-set version;
- normalized invoice identifiers;
- charged-period, daily-rate, charged-days, and total claims;
- exact PDF anchors for all published claims;
- one durable `START_RECONSTRUCTION` task;
- monotonically sequenced public events;
- Invoice `received_at` as the knowledge cutoff.

It may not assume:

- invoice claims are historical facts;
- the invoice proves a container actually moved;
- the invoice proves free time, terminal status, availability, appointments, or
  empty-return conditions;
- the invoice-selected charge period is applicable;
- a tariff candidate governs merely because retrieval returned it;
- source availability is permanent;
- reconstruction is complete merely because rows exist.

### 10.2 Freeze the Gate 2 contract before coding

The Gate 2 commission and backend plan must define:

- canonical reconstruction aggregate/version;
- canonical shipment-event/source-reference representation;
- time domains:
  - occurred time;
  - effective time;
  - observation/recorded time;
  - received time;
  - knowledge cutoff;
- provenance classification and representative disclosure;
- source availability/verification states;
- reconstruction coverage/gap states;
- task types and transitions;
- public event types and summaries;
- what the queue/workbench may expose while reconstruction is incomplete;
- the exact cross-agent output Gate 3 and Gate 4 will consume.

Reuse the existing `workflow_tasks`, `workflow_task_attempts`,
`invoice_events`, and `event_outbox` conventions. Do not build a second
orchestration or animation mechanism for reconstruction.

### 10.3 Additive persistence

Use additive raw-SQL migrations. The exact table names belong to the commission,
but the persisted model must represent:

- reconstruction run/version;
- stable Invoice binding;
- immutable input claim-set version;
- Invoice knowledge cutoff;
- every reconstructed event;
- source artifact/reference;
- exact source version or observation version;
- provenance classification;
- all relevant time domains;
- normalized event facts;
- coverage/gap state;
- validation issues;
- Managed MCP query/audit reference in private storage;
- public-safe reconstruction projection;
- downstream task/input fingerprint;
- uniqueness/idempotency constraints.

Required invariants:

- tenant scope on every key and lookup;
- no mutation of a completed reconstruction version;
- no event without a source and required time fields;
- no event after the knowledge cutoff may enter the reconstruction;
- one active reconstruction version per intended input fingerprint;
- task retry does not duplicate events or reconstruction versions;
- late workers cannot commit after losing a lease;
- transition, output rows, public event, and outbox update atomically;
- public rows never contain raw private source locators or query details.

Every new history-sensitive table must receive the controlling retention
decision and live readback before the demo depends on historical behavior.

### 10.4 Representative source package

This is a major truth boundary.

The existing broader external archive does not automatically support the
locked `$700` story. The old recovery lineage does not prove a terminal or
container-event feed for the new demo.

The owner/commission must select a clearly representative, fictional, licensed,
or otherwise permitted source package that supports the locked demo without
being described as a live carrier/customer feed.

For every source:

- preserve exact bytes or an exact immutable observation;
- record source type and public disclosure;
- record observation/recorded time separately from event effective time;
- retain exact version identity privately;
- expose only a public-safe source label and verification state;
- define a deterministic reset/reseed;
- prove that missing/unavailable sources produce gaps rather than invented
  events.

Do not fabricate missing terminal/container facts inside the MCP response,
worker, prompt, frontend, or fallback fixture.

### 10.5 Managed MCP path

The reconstruction query must be:

- fixed and bounded;
- tenant- and Invoice-scoped;
- read-only;
- constrained to the Invoice knowledge cutoff;
- explicit about required time domains;
- explicit about source and version bindings;
- deterministic in ordering;
- fail-closed;
- free of broad natural-language query generation;
- free of direct database fallback when MCP is unavailable.

Persist enough private audit data to diagnose the query without exposing it in
public API/events/logs.

The reconstruction worker should consume the durable task created by Intake,
lease it using the shared task machinery, execute the fixed MCP read, validate
every returned row, persist an immutable reconstruction version, and atomically
emit its public progress/result events.

### 10.6 Reconstruction validation

Validate locally, outside the model:

- source exists and is owned by the tenant/Invoice context;
- source version is exact and currently verifiable;
- required provenance classification exists;
- required time fields exist and are internally consistent;
- no fact crosses the Invoice knowledge cutoff;
- container and bill-of-lading identifiers match allowed normalized inputs;
- event types are allowlisted;
- duplicate semantic events are deterministically collapsed or rejected;
- contradictory events become visible issues rather than silent selection;
- missing coverage remains a gap;
- model or MCP prose cannot become a fact without a persisted source row.

### 10.7 Gate 2 negative tests

At minimum:

- cross-tenant Invoice/task/source lookup;
- wrong Invoice/source pairing;
- unknown source version;
- missing exact source;
- MCP unavailable;
- MCP unauthorized;
- malformed MCP row;
- missing provenance;
- missing required timestamp;
- event after knowledge cutoff;
- conflicting events;
- lease expiry and reclaim;
- late-worker fencing;
- retry exhaustion;
- repeated task delivery;
- outbox duplicate delivery;
- restart between read and commit;
- public projection/log leak scan;
- explicit proof that no direct SQL, fixture, or model fallback creates a
  successful reconstruction.

### 10.8 Gate 2 deployed proof

Use a clean isolated lineage. Prove:

1. the committed Intake fixture creates a new Invoice;
2. Intake produces the exact reconstruction task;
3. the deployed reconstruction worker leases that task;
4. the worker performs a real Managed MCP read;
5. every accepted event has source, version, time, and provenance;
6. no event crosses the Invoice knowledge cutoff;
7. one immutable reconstruction version commits;
8. task/result/event/outbox state is atomic;
9. SSE and snapshot reads expose ordered public progress;
10. an MCP outage visibly blocks or retries without fallback;
11. a missing source creates an explicit gap/block;
12. public API, SSE, logs, and committed artifacts contain no private
    identifiers;
13. the exact downstream Gate 3/4 input contract is persisted once.

Do not use the old Gate 5 MCP read as proof. It read sealed memory for a
different path.

### 10.9 Gate 2 exit

Only mark Gate 2 `PASS` when:

- one real new-workflow trace exists;
- Managed MCP is load-bearing;
- every event is completely source/time/version/provenance bound;
- the result is immutable and idempotent;
- missing dependencies cannot produce a successful reconstruction;
- the deployed public-safe path and counterfactual failure both pass.

---

## 11. Gate 3 continuation — Applicable rule

Do not begin Gate 3 integration before Gate 2 provides the source-bound charged
period and event coverage contract.

Gate 3 must:

- form the actual hero retrieval query from persisted reconstruction and
  normalized claim inputs;
- use the real Distributed Vector Indexing path;
- persist the query/input fingerprint;
- persist candidates separately from applicability decisions;
- retain candidate source/version provenance;
- independently verify exact text;
- independently verify carrier/lane/equipment/scope;
- independently verify effective dates against the charged period;
- independently verify currency, unit, and rate;
- reject wrong-date, wrong-scope, wrong-rate, or unavailable-source candidates;
- expose retrieval confidence separately from deterministic applicability;
- fail closed with `REQUEST_EVIDENCE` or the locked equivalent when no
  candidate passes;
- never let vector similarity alone decide the governing rule.

Required counterfactual:

- an intentionally wrong-date candidate may retrieve semantically but must be
  deterministically rejected.

Gate 3 exit:

- the actual hero query used vector indexing;
- the selected candidate is independently validated;
- the wrong-date candidate is rejected;
- vector unavailability does not fall back to a fixture or embedded clause.

---

## 12. Gate 4 continuation — Deterministic judgment

Gate 4 consumes only persisted, validated Gate 2 and Gate 3 outputs.

Required behavior:

- one deterministic judgment row/result per charged day;
- integer minor-unit money;
- explicit claimed, applicable, supported, and discrepancy amounts;
- complete source/input bindings for every day;
- no model arithmetic;
- no aggregate-only `$700` without seven independently explainable rows;
- locked `$700` dispute path;
- independent valid `$875` supported/approved-for-payment path;
- missing-evidence path that requests evidence rather than disputing;
- frozen recommendation version;
- deterministic input fingerprint;
- immutable revision history;
- explicit stale/superseded recommendation state;
- retry/replay produces identical results.

Required tests:

- all seven hero days;
- date boundaries;
- free-time boundaries;
- rate/currency/unit mismatch;
- rounding and minor-unit arithmetic;
- partial source coverage;
- missing terminal/container evidence;
- valid `$875` case;
- stale revision;
- repeated calculation;
- conflicting candidate;
- no-fallback dependency failures.

Gate 4 exit:

- seven days independently resolve to the locked discrepancy;
- total is `$700`;
- valid case resolves to `$875` supported;
- missing evidence resolves to `REQUEST_EVIDENCE`;
- all results are deterministic and source bound.

---

## 13. Gate 5 continuation — Human authority and seal

Gate 5 adapts historical approval/seal primitives to the new object model.

Required behavior:

- human approval references one exact immutable recommendation version;
- authenticated or explicitly synthetic-demo approver boundary;
- optimistic concurrency/ETag;
- idempotency key;
- stale recommendation rejection;
- repeated approval replay without duplicate active decisions;
- concurrent approval conflict handling;
- atomic transaction binds:
  - recommendation version;
  - seven day judgments;
  - applicable-rule version;
  - reconstruction version;
  - immutable claim set;
  - exact invoice source;
  - approver authorization;
  - timestamps and revision;
- sealed decision record cannot be edited in place;
- later changes create a new explicit version/process, never history rewriting;
- public receipt exposes safe verification facts, not private source bindings.

Gate 5 exit:

- one frozen recommendation is approved;
- one atomic seal binds all exact inputs and authority;
- concurrent/repeated/stale approvals cannot create conflicting active records.

---

## 14. Gate 6 continuation — Gated external action

Gate 6 requires a separate explicit owner decision for the controlled provider,
recipient, permissions, and send execution.

Do not treat the word “email” as an Intake requirement. Email begins here,
after a sealed decision exists.

Required sequence:

```text
sealed decision
→ sealed-record-only fact pack
→ bounded correspondence draft
→ deterministic locked-field validation
→ second human authorization
→ fresh MCP check
→ fresh vector binding check
→ fresh exact-source checks
→ explicit no-fallback check
→ one send-attempt record
→ controlled provider call
→ provider acknowledgement
```

Required invariants:

- drafting reads only the sealed record/fact pack;
- model output cannot alter money, dates, identifiers, or decision state;
- second authorization references one exact draft and sealed decision;
- every fresh gate result binds to the same send attempt;
- a failed MCP, vector, exact-source, or no-fallback gate prevents send;
- provider timeout/retry is idempotent;
- no duplicate sends;
- acknowledgement means only what the provider actually confirmed;
- public wording does not claim carrier receipt, acceptance, payment, recovered
  money, or legal correctness.

This is the gate that may eventually close:

> Verification shows MCP, S3, vector binding, and no fallback without private
> identifiers.

It remains OPEN until the action-gating path is executed.

---

## 15. Gate 7 continuation — Public hero

Gate 7 is an integrated backend/frontend/release gate.

Backend responsibilities:

- stable logged-out projections;
- safe source streaming;
- durable event history and SSE reconnect;
- deep-linkable Invoice state;
- full positive trace;
- valid-invoice restraint trace;
- negative send-gating trace;
- public-safe logs and diagnostics;
- reset/reseed;
- deployment/readiness/teardown procedure.

Frontend responsibilities:

- queue insertion without hard refresh;
- immediate clickability;
- workbench loads before processing completes;
- canonical tabless IA;
- source coverage and gaps;
- reconstruction, rule, recommendation, authority, correspondence, activity,
  and verification surfaces;
- keyboard and responsive behavior;
- truthful retry/block states;
- no client-only timers or simulated completion.

Release responsibilities:

- public README matches the live demo;
- runbook;
- three consecutive clean logged-out rehearsals;
- representative-data disclosure;
- privacy scan;
- unauthenticated clone/install/build/link validation;
- video and Devpost artifacts;
- enough retained logs to diagnose a failure;
- no private identifiers in repository, UI, network payloads, logs, or video.

Gate 7 exit:

- a logged-out judge can run or inspect the complete path;
- every visible claim derives from executed server state;
- the representative boundary is disclosed;
- the public repository and recording requirements pass.

---

## 16. Shared engineering contracts for every remaining gate

### Tenant isolation

- Every table, key, query, task, event, source, and API read is tenant scoped.
- Wrong-tenant access does not reveal object existence.
- Existing application-level filtering is not database-enforced RLS; do not
  overclaim it.

### Immutability and versions

- Exact source versions remain exact.
- Completed claim sets, reconstructions, recommendations, seals, drafts, and
  send attempts are immutable.
- Changes create explicit versions.
- Public IDs are opaque.

### Money

- Integer minor units plus ISO currency.
- Models never perform authoritative arithmetic.
- Currency and unit compatibility are deterministic checks.

### Idempotency

- Every externally triggered mutation has an idempotency key and request
  fingerprint.
- Every durable task has an input fingerprint.
- Replay returns the existing identity/result.
- A different payload under one key conflicts.
- Repeated delivery never duplicates active domain objects or side effects.

### Leasing and fencing

- Durable work is leased.
- Attempts are persisted.
- Expired leases can be reclaimed.
- Late workers are fenced from commit.
- Retry counts and backoff are bounded.
- Terminal failures are public-safe and diagnostically useful.

### Atomicity

Each meaningful transition atomically writes:

- domain result/version;
- task state;
- Invoice aggregate state/sequence;
- public event;
- outbox row.

Do not claim atomicity across S3/provider and CockroachDB. Use explicit durable
pending/reconciliation states.

### Events and SSE

- Server state is authoritative.
- Events are durable and monotonically sequenced per Invoice.
- Outbox delivery is idempotent.
- SSE is notification, not the source of truth.
- `Last-Event-ID` replays missed events.
- Unknown cursor requires snapshot reconciliation.
- Frontend may not invent progress from timers.

### Public safety

Never expose:

- credentials;
- tenant-internal IDs;
- storage bucket/key/version;
- raw hashes used as private bindings;
- database connection details;
- SQL;
- prompts/raw model responses;
- provider request internals;
- private error bodies.

Expose safe state, summary, source label, verification status, and recovery
guidance.

### No fallback

No gate may succeed by silently substituting:

- fixture data;
- embedded hero constants;
- direct SQL for required Managed MCP;
- hard-coded clause for required vector retrieval;
- current/unversioned source for missing exact source;
- model invention for missing evidence;
- client animation for missing server state;
- mock provider acknowledgement for an unexecuted send.

---

## 17. Testing and evidence standard

For each gate, return:

- requirement;
- controlling document and section;
- status: `PASS`, `PARTIAL`, `OPEN`, or `CONFLICT`;
- exact repository evidence;
- smallest implementation;
- acceptance test;
- dependency on another workstream.

Before implementation:

- unit-test the pure contracts;
- integration-test transaction boundaries;
- test migrations from blank and upgrade state;
- test retries/replays/concurrency;
- test tenant and object ownership;
- test public serializers;
- test leak patterns.

Before declaring a gate complete:

1. run the real sponsor path;
2. run at least one counterfactual in which the sponsor dependency is
   unavailable or wrong;
3. prove no fallback;
4. read back durable state;
5. read back deployment state;
6. scan public responses/events/logs/artifacts;
7. record a public-safe execution manifest;
8. retain private exact bindings separately;
9. document teardown;
10. leave later gates OPEN.

Historical evidence is a regression oracle, not automatic success for a new
path.

---

## 18. Git, cloud, and destructive-action discipline

### Git

- Work on a named feature branch.
- Make bounded commits.
- Push only the authorized branch.
- Keep the PR draft until review criteria are met.
- Never force-push or rewrite shared history.
- Never merge or push directly to `main` without explicit authorization.
- Preserve unrelated user changes.

### Cloud and database

- Resolve the exact identity/resource before mutation.
- Prefer isolated resources for gate proof.
- Never modify the existing hero database, lineage, source objects, or judged
  routing unless a later owner decision explicitly changes that boundary.
- Use additive migrations.
- Do not run destructive migrations.
- Do not copy secrets into files, chat, commit messages, or command output.
- Consolidate IAM needs instead of asking the owner to add permissions one at
  a time.
- Read back every external change.
- Pause retained services when evidence is complete unless the owner requires
  them running.

### Teardown

- Teardown is separately approval gated.
- Resolve exact targets from private records.
- Confirm isolated tags/scope.
- Capture before/after readbacks.
- Never use broad recursive deletion or unresolved globs.
- Do not delete retained evidence needed for review.

---

## 19. Recommended Gate 2 delivery sequence

Use this order to minimize rework:

1. Obtain and audit the Gate 2 commission.
2. Decide merge versus stacked-branch strategy.
3. Freeze reconstruction nouns, states, time domains, source contract, and
   downstream output contract.
4. Add pure domain validators and contract tests.
5. Add additive reconstruction/source migrations.
6. Add repository transactions and idempotent task completion.
7. Add fixed Managed MCP adapter/query integration.
8. Add durable reconstruction worker using shared leasing/fencing.
9. Add public projections and events using shared outbox/SSE.
10. Add source gaps and fail-closed states.
11. Add unit and CockroachDB integration tests.
12. Add representative fictional source package and manifest only after
    disclosure approval.
13. Provision or extend isolated resources within the commission.
14. Execute one clean deployed positive trace.
15. Execute MCP/source/no-fallback negatives.
16. Scan API/events/logs/artifacts.
17. Record execution/teardown manifest.
18. Pause isolated runtime.
19. Push/update a draft PR.
20. Leave Gate 3 and later states OPEN.

---

## 20. Suggested first report from the new agent

Use a concise external report even if the internal audit is detailed:

```text
STATUS

Gate 2 audit status and whether implementation is authorized.

CHANGED

Files/resources changed, or “none” for read-only audit.

VERIFICATION

Exact tests/readbacks and what they prove.

BLOCKERS/DEFERRALS

Missing commission, representative source decision, frontend dependency,
external-send authority, or later gates explicitly left OPEN.

NEXT ACTION

The smallest safe action that advances Gate 2.
```

---

## 21. Ready-to-paste prompt for the next lead backend agent

```text
You are the new lead backend implementation agent for Tally.

Work only in markbrazinski/tally-agentic-memory. Read AGENTS.md first.

Read the authoritative planning documents from the private coordination
checkout in this order:

1. docs/TALLY_CURRENT_TRUTH.md
2. docs/Tally_Locked_Demo_Script_v1.md
3. docs/Tally_UX_Audit_and_IA_v1.md
4. docs/Tally_BE_Execution_Plan_v1.md
5. docs/Tally_BE_Intake_Orchestration_Commission.md
6. the Gate 2 Reconstruction/Evidence commission when supplied

Then read:

- docs/TALLY_BE_HANDOFF_AFTER_INTAKE_2026-07-23.md
- artifacts/intake-v1/EXECUTION_MANIFEST.json
- artifacts/intake-v1/TEARDOWN.md
- migrations/007_intake_orchestration.sql
- migrations/008_intake_retry_idempotency.sql
- migrations/009_intake_duplicate_link.sql
- src/core/intake.py
- src/core/intake_claims.py
- src/platform/intake_repository.py
- src/platform/intake_tasks.py
- src/platform/intake_worker.py
- src/platform/intake_events.py
- src/platform/intake_api.py
- src/external/invoice_source_store.py
- src/external/intake_bedrock.py
- the corresponding Intake tests and demo fixture manifest

Current truth:

- Gate 1 backend is complete and deployed proof passed.
- Full suite baseline is 699 passing tests.
- The Intake service is paused and retained.
- Draft PR #1 targets main from feat/intake-orchestration-v1.
- Gate 1 browser rendering remains a frontend dependency.
- Gates 2–7 remain.
- Approval, correspondence, send, and action gating are not complete.
- “Verification shows MCP, S3, vector binding, and no fallback without private
  identifiers.” remains OPEN until Gate 6 executes.

First audit Gate 2 — Sourced reconstruction. For every requirement return:

- requirement;
- controlling document and section;
- PASS / PARTIAL / OPEN / CONFLICT;
- exact repository evidence;
- smallest implementation;
- acceptance test;
- dependency on another workstream.

Do not redesign the product. Do not inherit success from the old read-only
hero. Do not mutate cloud, databases, remotes, or external systems until the
bounded Gate 2 commission and authorization are confirmed.

Gate 2 must eventually prove one real Managed MCP reconstruction trace in which
every accepted pre-invoice event has source, time, version, and provenance; no
event crosses the Invoice knowledge cutoff; failures block or expose gaps; and
no direct SQL, fixture, model, or client fallback produces success.

Report only:

STATUS
CHANGED
VERIFICATION
BLOCKERS/DEFERRALS
NEXT ACTION
```

---

## 22. Final handoff truth

The Intake implementation is valuable, current, and reusable. It is not the
whole product.

The safe continuation is:

```text
review/merge or explicitly stack Intake
→ obtain Gate 2 commission
→ execute sourced reconstruction through Managed MCP
→ execute and validate vector retrieval
→ calculate deterministic judgment and restraint cases
→ bind human authority and seal
→ execute separately authorized gated correspondence/send
→ integrate and rehearse the logged-out public hero
```

Keep every gate independently truthful. A cleanly deferred claim is better
than an inherited or simulated pass.

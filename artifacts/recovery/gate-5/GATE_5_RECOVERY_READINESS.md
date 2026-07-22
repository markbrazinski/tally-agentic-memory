# Gate 5 recovery release-readiness report

Verdict: **DEMO PASS WITH LIMITATIONS; PUBLICATION VERIFIED**

Executed on July 22, 2026 under recovery lineage
`16c798f2-3bb4-4cfc-ac44-72c7011f13e5`. This is a new fictional demonstration
execution. The expired original lineage remains unchanged as historical
evidence and is not represented as recovered.

## Executed result

- All 13 tables used by the hero lineage read back a 90-day CockroachDB MVCC
  TTL before the new lineage was created.
- The retained exact, versioned, hash-verified synthetic tariff and invoice
  objects were reopened through S3. The real application path recomputed
  $250/day recorded versus $350/day claimed for seven days: a $700 finding.
- The owner explicitly approved the displayed receipt. Sealing recorded that
  approval, its canonical manifest, exact evidence bindings, and the filing
  transaction in one transaction. A repeated seal was idempotent.
- A later fictional contest was committed in a separate transaction. AS OF
  SYSTEM TIME replay proves `FILED` at sealing versus `CONTESTED` now, while
  the receipt, evidence identities, S3 Version IDs, and hashes remain byte-for-
  byte bound to the same values. Those private values are intentionally not
  published here.
- Managed MCP retrieved and revalidated the sealed receipt through its fixed
  read surface. Cross-tenant, unknown, unsealed, write-denial, outage,
  idempotency, and no-fallback checks passed. Both exact S3 versions passed all
  59 verifier checks.

No carrier or customer was contacted. Nothing was sent, recovered, paid,
resolved, or legally determined.

## Verification and deployed judge path

| Check | Result |
|---|---|
| Python suite | PASS — 664 tests; one dependency deprecation warning |
| UI suite | PASS — 23 tests |
| Vite production build | PASS; existing non-module `support.js` warning |
| Locked Gate 4 evaluation | PASS — 10/10 |
| Focused retention/seal checks | PASS — 37 preparation and 21 seal tests |
| Changed-scope Ruff and Git diff integrity | PASS |
| Exact versioned S3 verifier | PASS — 59/59 |
| Managed MCP fixed read and negative matrix | PASS |
| linux/amd64 image, App Runner readback, `/readyz` | PASS |
| Logged-out hero, clean clients | PASS — 3/3 consecutive requests |

Each clean-client response from
<https://x69yr3tibq.us-east-1.awsapprunner.com/> returned the synthetic label,
`executed`, `mock_fallback: false`, historical/current `FILED` / `CONTESTED`,
unchanged bindings, exact-version S3 verification, and MCP `verified_read`.

The deployed code image is commit `b5ef407f99b299f3d2c8f539969b54f8fba07d70`.
The implementation commit was scanned and pushed while the repository was
private. Publication was separately authorized and verified afterward.

## Limitations and publication readiness

- CockroachDB history is bounded to the configured 90-day MVCC window. Raising
  the TTL did not and cannot resurrect the expired original execution.
- Tenant separation is enforced by the fixed application query, not database
  RLS. Managed MCP does not provide request-exact server-side audit evidence.
- OAuth refresh and rotation are observed live, not a provider guarantee for
  the full judging period. Failure remains fail-closed with no substitute data.
- The accepted MCP write denial is the exact observed in-band denial shape,
  not an HTTP 403. No generic MCP proxy is browser-accessible.
- The repository is public at
  <https://github.com/markbrazinski/tally-agentic-memory>. Immediately before
  publication, its reachable-history and worktree scan found no credential,
  DSN, infrastructure identifier, private object identity, source body, live
  token, or curated private value. The owner explicitly accepted four ordinary
  historical author-email findings. An unauthenticated clone, installation,
  README/license read, full test/build run, and link check passed after
  publication. No ref was deleted, rewritten, squashed, or force-pushed.

## Cost and teardown

The AWS budget is now one cumulative `CUSTOM` period from July 1 through the
September 22 teardown, with a $10 limit and actual-spend alerts at 80% and
100%. Readback after deployment showed $4.484 actual and an AWS forecast of
$5.248. A conservative bottom-up projection is $9.734–$10.984 cumulative by
teardown. Because the upper case exceeds the authorization, spend must remain
monitored and the demo must be paused or removed early if it approaches $10.
The budget filters the seven authorized services but is not resource-tag
isolated, so it may conservatively include other use of those services.

The logged-out path is intended to remain available through September 16. The
one-time App Runner deletion remains scheduled for September 22; the remaining
ECR, SSM, DynamoDB, IAM, budget, and OAuth cleanup still requires the documented
manual teardown and readback on that date.

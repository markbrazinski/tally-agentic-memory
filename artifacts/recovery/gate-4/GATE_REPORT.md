# Gate 4 Report — temporal replay, retention, and truthful public demo

## Verdict

`PASS` — the stored CockroachDB seal timestamp replayed the transactional
case state at filing, the later state differed meaningfully, configured MVCC
retention and outside-window behavior were read back, exact versioned S3
evidence reopened successfully, the locked ten-case harness passed, and the
public demo was truth-labeled.

This report contains only sanitized aggregate evidence. Exact timestamps,
tenant/case identities, hashes, S3 Version IDs, bucket/object identities,
database/cluster details, connection metadata, and route response bodies stay
under ignored mode-restricted private paths.

## Commit

This public-safe release is the root commit of
`public/p2-recovery-sanitized`; it has no parent. The accepted bounded Gate 4
commit and prior gate history remain local because their intermediate trees
are not truth-labeled to the final publication standard. The release contains
the accepted current tree but makes none of that local history reachable. Its
immutable commit SHA and matching remote branch SHA are reported after commit
creation and the post-commit scan. No private recovery ref is included.

## What changed

Gate 4 adds an authenticated `GET /cases/{case_id}/replay` application route.
It first loads the current tenant-scoped sealed case, derives the replay anchor
only from `cases.sealed_txn_ts`, reads back the queried tables' GC TTLs, and
executes a real historical query at that exact CockroachDB system timestamp.
It then validates the complete case/evidence/tariff/snapshot binding and
compares the historical and current projections.

There is no current-state fallback for the historical half. Missing, unsealed,
malformed, wrong-tenant, expired, retention-misconfigured, unaudited, or
database-unavailable paths return no invented replay.

No schema migration was required. The narrow replay queries only `cases`,
`case_evidence`, `tariff_clauses`, and `tariff_snapshots`; every table already
has the required 90-day CockroachDB GC TTL.

## Migrations and infrastructure changes

None. Gate 4 added no database migration, schema object, AWS permission,
CockroachDB cluster, paid resource, integration, production deployment, or
external send. It reads the pre-existing 90-day GC TTL configuration without
changing it. Runtime proof used the existing isolated recovery environment and
kept exact infrastructure identifiers in ignored private artifacts.

## Executed evidence

- Command/query:

  ```text
  python -m scripts.gate4_replay
  python -m scripts.gate4_evaluation
  python -m pytest
  npm test
  npm run build
  ruff check <Gate-4 Python scope>
  git diff --check
  ```

- Environment: the existing isolated recovery CockroachDB and versioned S3
  evidence, plus the local public-safe branch and synthetic evaluation fixture.
  All connection data, infrastructure identifiers, exact object identities, and
  raw response bodies remained in ignored mode-`0600` private artifacts.

- Result: exact stored-HLC replay `FILED` then versus `CONTESTED` now; all four
  queried tables reported 90-day GC TTL; outside-window replay was rejected
  without fallback; exact S3 verification passed `59/59`; the locked evaluation
  passed `10/10`; Python passed `550`; UI passed `27`; lint, build, and diff
  checks passed.

- Artifact: this sanitized Gate Report and
  `artifacts/recovery/gate-4/evaluation-results.json`. Detailed executed replay
  and exact-version results remain private under `runtime-artifacts/gate-4/`.

## Executed exact-timestamp replay

The public-safe command shape was:

```text
TALLY_GATE4_CRDB_DSN=<private-isolated-database> \
TALLY_GATE4_TENANT_ID=<private-synthetic-tenant> \
TALLY_GATE4_HERO_CASE_ID=<private-synthetic-case> \
TALLY_GATE4_WRONG_TENANT_ID=<private-synthetic-negative> \
TALLY_GATE4_UNKNOWN_CASE_ID=<private-synthetic-negative> \
TALLY_GATE4_UNSEALED_CASE_ID=<private-synthetic-negative> \
python -m scripts.gate4_replay
```

The runner invoked the actual FastAPI route in process with the exact private
database adapter. The executed sanitized result was:

```text
route_passed=true
stored_timestamp_replay=true
historical_state_filed=true
current_state_contested=true
meaningful_state_difference=true
receipt_unchanged=true
retention_90_days=true
target_timestamp_queryable=true
approved_retention_language_present=true
wrong_tenant_not_found=true
unknown_not_found=true
unsealed_rejected=true
outside_retention_rejected=true
outside_retention_sqlstate_present=true
executed_query_lines=2
audit_tags_advanced=true
passed=true
```

The historical query returned `FILED` at the stored seal HLC. The current read
returned `CONTESTED` after the later response recorded in Gate 3. The tariff
rate, version label, manifest hash, and sealed evidence-content hash remained
equal after independent recomputation. This is the required meaningful
transaction-time difference; no effective, observed, display, or fictional UI
date was substituted for the Cockroach system timestamp.

CockroachDB does not accept a bound placeholder in `AS OF SYSTEM TIME`. The
implementation therefore has one documented narrow exception to the normal
all-values-bound rule: only the database-returned `sealed_txn_ts` may enter the
fixed AOST query, and only after exact positive canonical-decimal validation.
Tenant and case UUIDs remain bound. Signs, whitespace, exponent notation,
quotes, SQL punctuation, non-finite values, excess precision, and caller-
supplied timestamps are rejected. The durable audit copy redacts the HLC.

## Retention and long-term evidence

Live zone-configuration readback returned `gc.ttlseconds = 7776000` (90 days)
for each queried table:

| Table | Executed readback |
| --- | --- |
| `cases` | 7,776,000 seconds |
| `case_evidence` | 7,776,000 seconds |
| `tariff_clauses` | 7,776,000 seconds |
| `tariff_snapshots` | 7,776,000 seconds |

The stored target timestamp was queryable. A separate fixed `-91d` diagnostic
was rejected by CockroachDB and supplied a SQLSTATE; raw database error text
remains private. The route never fell back to a current read.

The approved product language is returned by the route and used here exactly:

> Versioned S3 retains the dated source artifact. Within CockroachDB’s
> configured MVCC window, Tally can also replay the transactional case state
> at filing.

Long-term verification reopened the receipt's exact versioned S3 objects and
recomputed every binding: `59/59` checks passed with zero reasons. The verifier
was corrected narrowly so an already sealed receipt remains sealed through
the legitimate `FILED`, `CONTESTED`, and `RESOLVED` lifecycle states; unsealed
states continue to fail.

This report does not claim indefinite CockroachDB time travel. Beyond MVCC
retention, recovery depends on the exact S3 Version ID and source SHA-256.

## Locked ten-case evaluation harness

The committed fixture is explicitly classified as synthetic demonstration
data, uses fictional Asterline Demo Shipping sources and `GATE4-SYNTHETIC-*`
identifiers, performs no network I/O, and compares expected results only after
production functions compute the actual outcome.

Executed command:

```text
python -m scripts.gate4_evaluation
```

Executed aggregate result:

```text
case_count=10
passed_count=10
failed_count=0
all_passed=true
```

| # | Contract case | Computed actual | Result |
| --- | --- | --- | --- |
| 1 | Valid invoice at recorded rate | `PASS` | PASS |
| 2 | Later/wrong rate | `FLAG` | PASS |
| 3 | Late invoice when rule applies | `FLAG` | PASS |
| 4 | Required-field failure | `FLAG` | PASS |
| 5 | Unsupported/invented citation | `ABSTAIN` | PASS |
| 6 | Missing coverage | `ABSTAIN` | PASS |
| 7 | Cross-tenant retrieval | `FAIL_CLOSED` | PASS |
| 8 | Duplicate filing/seal | `IDEMPOTENT` | PASS |
| 9 | Corrupted source hash | `VERIFICATION_FAILED` | PASS |
| 10 | Similar but temporally inapplicable source | `REJECTED_TEMPORALLY` | PASS |

The duplicate-seal case invokes production `seal_case` twice through a
deterministic synthetic database boundary and proves one ledger mutation, one
sealed evidence item, one approval, an unchanged evidence hash, and an
unchanged seal timestamp. The Gate 1 live execution separately proved full
workflow double-seal idempotency; Gate 4 did not create another live filing.

The detailed public synthetic output is
`artifacts/recovery/gate-4/evaluation-results.json`. Every metric in that file
is emitted by the harness; none is copied from the UI.

## Public UI before and after

| Before Gate 4 | Gate 4 result |
| --- | --- |
| Film data lacked a persistent provenance label | Persistent `SYNTHETIC FILM — NOT LIVE · FICTIONAL DATA` badge and explicit fictional-date/amount domains |
| Misconfigured live mode silently fell back to film | Explicit `LIVE UNAVAILABLE — NO MOCK FALLBACK` empty state |
| `41` invoices and `300/300` assertions were constants | Removed; evaluation is unavailable unless an explicit computed synthetic result is connected |
| Fictional credits appeared as recovered money | Default recovered-money dataset is empty; UI states no external recovery was confirmed |
| Carrier conduct/defect/re-bill ratios were hardcoded | Default conduct dataset is empty until a computed conduct harness exists |
| Re-bill lineage and LFD/business-day behavior appeared evaluated | Explicitly labeled not evaluated |
| Real-looking company, carrier, terminal, operator, and email names | Explicitly fictional example entities and reserved `.example` addresses; every public contract JSON is classified synthetic |
| Mock SQL/commit lines appeared executed | Labeled synthetic previews/events and not executed; live mode shows no query-log feed |
| UI overstated tenant-scoped credentials | Correct application-filter wording; no RLS or tenant-restricted credential claim |
| Hero ended in an invented carrier credit | Hero remains `CONTESTED`; memory replay rejects the later rate without claiming external sending, credit, or resolution |
| Build-time browser bearer configuration was available | Removed; live credentials can only be injected ephemerally at runtime and are never compiled into assets |
| Live case view had no real replay path | LiveProvider calls the authenticated replay route and renders its `FILED` to `CONTESTED` result in the existing case view |

The film's observation, filing, and contest dates now fall in July 2026 and
remain explicitly labeled fictional scenario dates, not Cockroach MVCC
timestamps or source observation times. The real replay route supplies its own
distinct stored HLC and current state.

## Executed automated evidence

```text
Python suite: 550 passed, 1 existing Starlette/httpx deprecation warning
UI suite: 27 passed
Gate 4 harness: 10/10 passed
Exact S3 receipt verification: 59/59 passed
Ruff on the complete Gate 4 Python scope: passed
Component and provider JavaScript syntax checks: passed
Vite production build: passed; support.js remained present in the output
git diff --check: passed
```

The repository-wide Ruff invocation still reports nine pre-existing style
findings in untouched Gate 0 capture tests. Ruff passes on every Python file in
the Gate 4 scope; no unrelated formatting was changed.

## Acceptance criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Real historical query at stored Cockroach timestamp | PASS | `FILED` then, `CONTESTED` now |
| Target timestamp remains queryable | PASS | Stored target returned from the exact AOST query |
| Retention configuration visible and documented | PASS | 90-day live readback and approved route/report language |
| Outside-retention behavior | PASS | Rejected with SQLSTATE, no fallback |
| Long-term exact S3 version/hash recovery | PASS | 59/59 checks |
| Default UI numbers generated, demo-labeled, or removed | PASS | Unconnected evaluation renders unavailable |
| Public entities fictional | PASS | Fixture classification test and public-safety audit |
| No fabricated recovered totals or conduct ratios | PASS | Datasets empty and claims removed |
| Query-log lines correspond to executed operations | PASS | Route supplies two executed lines; film examples say not executed |
| Locked ten-case harness produces reported metrics | PASS | 10/10 recomputed outcomes |
| Memory changes outcome visibly | PASS | Live view renders stored-HLC replay; film makes no external-result claim |
| Final demo build assembles | PASS | Vite build and required output assets verified |

## Negative tests

| Test | Expected | Actual | Result |
| --- | --- | --- | --- |
| Wrong tenant / unknown case / unsealed case | Fail closed | Rejected without replay data | PASS |
| Malformed or caller-supplied HLC | Reject before SQL | Only stored canonical HLC accepted | PASS |
| Outside-retention replay | SQLSTATE error, no fallback | Cockroach rejected; no current substitute | PASS |
| Missing or wrong table TTL | Refuse replay | Retention validation rejected mismatch | PASS |
| Clause/source/hash corruption | Verification failure | Historical and current tampering rejected | PASS |
| Cross-tenant evaluation | `FAIL_CLOSED` | `FAIL_CLOSED` | PASS |
| Unsupported citation / missing coverage | `ABSTAIN` | `ABSTAIN` | PASS |
| Temporally inapplicable evidence | Reject | `REJECTED_TEMPORALLY` | PASS |
| Duplicate seal | No second mutation | Idempotent result and unchanged receipt | PASS |
| Missing live UI configuration | No synthetic fallback | Live unavailable state | PASS |
| Browser build credentials | No compiled bearer | Bundle scan/test found none | PASS |
| Runner exception | No raw secret-bearing body | Exception class only; detail private | PASS |

## Independent verification

Engineering review passed after the historical source-hash binding and error-
redaction fixes. Public-safety review passed with zero exact private-value
collisions in the working tree or current public-reachable history. Final
acceptance confirmation and the mandatory post-commit reachable-history scan
are recorded before push.

## Claim impact

Gate 4 supports only these new claims: the stored Cockroach transaction
timestamp was replayed within configured MVCC retention; the historical state
differed meaningfully from current state; the exact versioned S3 evidence was
re-verified; and the local synthetic demonstration and locked ten-case harness
produced the reported results.

It does not expand the accepted product or security claims:

## Limitations

- Gate 3's accepted limitation remains: request-exact server-side MCP audit
  correlation is unavailable. Gate 4 does not claim otherwise.
- Tenant separation is application query filtering and canonical tenant-bound
  routing, not CockroachDB RLS or database-enforced isolation.
- `case_evidence` is an application-sealed evidence copy, not a claim of
  database-enforced physical immutability.
- Historical Cockroach replay is limited to configured MVCC retention.
- The public UI does not consume the committed harness artifact through a live
  evaluation route; it truthfully renders evaluation unavailable. Its live
  case view does consume the real replay route.
- The public film is synthetic and built locally. There was no production
  deployment, paid-resource creation, external dispute send, or confirmed
  carrier credit.
- Private executed response details remain unpublished.

## Recommended next action

Run the public-reachable history scan, push only
`public/p2-recovery-sanitized`, and verify that the remote named
branch resolves to the same SHA. Then stop: no production deployment, external sending, paid-
resource creation, private-evidence publication, or further gate is authorized.

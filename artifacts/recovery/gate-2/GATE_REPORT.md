# Gate 2 Report — load-bearing CockroachDB vector retrieval

## Status

`PASS` — independently verified. All content and identifiers in this report
are synthetic or redacted.

## Bounded outcome

The receipt path now uses a populated CockroachDB `VECTOR(1024)` index to rank
fictional tariff clauses. The authenticated tenant and carrier form the exact
index prefix. Python separately enforces effective dates, equipment, route,
service, embedding provenance, and exact retained-source verification. Vector
similarity never determines a verdict or overrides those fences.

The selected fictional historical clause remains bound to the existing Gate 1
finding/case/evidence path. Running that path twice produced one invoice, one
finding, one case, and one evidence row; the second filing returned the same
case and finding as already filed. No approval or seal action was performed.

## Migration and index DDL

Migration `005_gate2_clause_vector_search.sql` is additive. It adds nullable
context and embedding-provenance columns without backfilling or fabricating
values for historical rows. The load-bearing index is:

```sql
SET sql_safe_updates = false;

CREATE VECTOR INDEX IF NOT EXISTS tariff_clause_embedding_search_idx
  ON tariff_clauses
  (tenant_id, carrier_id, embedding vector_l2_ops);
```

The session-scoped safe-updates setting permits index creation over existing
rows. The authoritative final reproduction created a fresh target, applied all
six migrations, and immediately applied zero files on rerun. Its readback
confirmed the vector feature is enabled, all six migrations are tracked, all
eight Gate 2 columns are present, and the named index exists.

An earlier fresh-target helper stopped after executing idempotent DDL but before
tracking four files. The migration runner replayed those files, tracked all six,
and a subsequent zero-file invocation proved that recovered state safely
rerunnable. That interrupted attempt is retained as private failure evidence
but is not the authoritative final reproduction described above.

The migration does not drop, rewrite, or substitute observation/system time
fields. Existing MVCC history and `AS OF SYSTEM TIME` behavior remain intact;
Gate 2 uses retained capture rows plus explicit effective dates rather than
treating system time as business-effective time.

## Changed public modules

- `src/external/gate2_vector_search.py`: tenant/carrier-prefixed ANN query,
  exact primary-index comparison query, candidate provenance, and plan proof.
- `src/external/titan_embeddings.py`: fixed Titan V2 1,024-dimension normalized
  request contract and float32-stable provenance hashing.
- `src/core/vector_retrieval.py`: deterministic scope, temporal, context,
  exact-source, and embedding-integrity filters.
- `src/platform/vector_receipt_pipeline.py`: read-only product orchestration
  from query embedding through exact-version reopening and verification.
- `src/platform/vector_seed.py`: transactional synthetic seeding with
  deterministic exact-source truth-labeling and idempotency checks.
- `scripts/gate2_retrieval.py`: redacted live execution/evaluation runner.
- `scripts/gate2_upload_private.py`: exact-version synthetic fixture uploader.
- `src/platform/private_artifacts.py`: ignored-root, no-follow, mode-`0600`
  private evidence writer.
- `tests/fixtures/gate2/`: committed fictional clauses, queries, and invoice.

## Executed live evidence

The detailed plan, cloud object identities, database identity, stored vectors,
and exact source bindings remain in ignored mode-restricted private evidence.
The public-safe aggregate readback was:

```json
{
  "passed": true,
  "populated_row_count": 7,
  "index_used": true,
  "index_bruteforce_agree": true,
  "hero_selected_250": true,
  "cross_tenant_no_candidate": true,
  "masking_abstains": true,
  "idempotent": true,
  "query_count": 4,
  "selected_count": 2,
  "abstained_count": 2,
  "raw_top1_count": 1,
  "raw_topk_count": 2,
  "seed_idempotent": true,
  "selection_match_count": 4,
  "expected_abstention_count": 2
}
```

The exact runtime query and `EXPLAIN` share one SQL builder. Structural plan
evidence identified `tariff_clause_embedding_search_idx`; the corresponding
schema readback confirmed `VECTOR(1024)`, the tenant/carrier prefix, and all
embedding-provenance fields.

### Fictional hero top-k

| Rank | Fictional clause | Fictional capture | L2 distance | Temporal result | Exact source | Result |
| ---: | --- | --- | ---: | --- | --- | --- |
| 1 | `clause-northstar-250` | `capture-northstar-2026-01` | 0.659242 | applies | verified | selected |
| 2 | `clause-northstar-350` | `capture-northstar-2026-07` | 0.663702 | not yet effective | verified | rejected |
| 3 | `clause-northstar-similar-inapplicable` | `capture-northstar-storage` | 0.784776 | applies | verified | wrong service |
| 4 | `clause-northstar-wrong-route` | `capture-northstar-route` | 0.828813 | applies | verified | wrong route |
| 5 | `clause-northstar-wrong-equipment` | `capture-northstar-equipment` | 0.852044 | applies | verified | wrong equipment |

Exact object Version IDs are represented only as `<private-exact-version>` in
public-safe output.

### Negative and agreement evidence

- An empty carrier prefix was rejected before embedding, database, or source I/O.
- The same carrier UUID was populated in two tenant partitions; the isolation
  clause appeared in its own tenant and never in the authenticated hero tenant.
- Removing the valid historical clause through a non-mutating search mask
  produced abstention.
- Wrong-route input produced five ranked candidates and selected none.
- The later charge selected the fictional `$350/day` version, while the earlier
  charge selected `$250/day` and rejected the later version temporally.
- Index-backed and forced-primary-index exact retrieval produced identical
  deterministic results on every committed evaluation query.
- A corrupted or mismatched vector, model, input hash, object identity, object
  version, source hash, clause hash, or retained body fails closed in tests.

## AWS embedding permission and probe

The runtime identity received only `bedrock:InvokeModel` for the single model
`amazon.titan-embed-text-v2:0` in `us-east-1`. A fictional one-sentence probe
returned 1,024 finite values with a unit norm. No model enumeration or broader
Bedrock permission was used.

The committed invoice is a synthetic template, not a fixed observation-time
claim. The private uploader assigns `received_at` one day after the latest
authoritative tariff object observation, uploads those exact generated bytes,
and the runner verifies both the unchanged template fields and chronology. A
fresh post-review upload and fresh database reproduced the full PASS.

## Executed automated evidence

```text
Gate 2 focused tests: 73 passed
Full Python suite: 430 passed, 1 unrelated upstream deprecation warning
UI suite: 15 passed
Ruff: all checks passed
git diff --check: passed
```

All automated tests replace external paths with fakes. Live execution was
performed separately against an isolated database and private synthetic object
versions.

## Pass criteria

| Criterion | Executed result |
| --- | --- |
| Real vector index exists on populated rows | PASS — named index, 7 rows |
| Hero path queries the index at runtime | PASS |
| Plan shows intended index | PASS |
| Retrieval is tenant-scoped | PASS — same-carrier two-tenant proof |
| Historical applicability is separate | PASS — later version rejected for earlier date |
| Selected clause verifies against exact retained bytes | PASS |
| Removing valid clause abstains | PASS |
| Index and brute-force expected results agree | PASS — 4/4 queries |

## Independent verification

Independent verdict: `PASS`.

The verifier checked every Gate 2 pass criterion against the current code,
fresh private execution evidence, schema and migration readbacks, structural
plan proof, candidate output, negative paths, and full local suite. It found no
remaining functional, acceptance-evidence, or closure blocker.

## Exact limitations

- The fixture is deliberately small and synthetic. Top-1 was `1/2` for the two
  positive retrieval queries and top-k was `2/2`; these are fixture results,
  not production accuracy claims.
- A named index hint keeps the real vector index load-bearing on a seven-row
  fixture where an optimizer could reasonably prefer a table scan.
- Only fictional UTF-8 text fixtures were evaluated; production document
  parsing and corpus-scale retrieval are outside Gate 2.
- Raw execution plans and exact cloud/database evidence remain private because
  they contain prohibited infrastructure and source-binding metadata.
- The IAM permission can incur Bedrock usage charges, although the bounded
  probe and synthetic evaluation were small.

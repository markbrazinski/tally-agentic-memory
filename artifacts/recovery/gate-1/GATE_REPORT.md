# Gate 1 Report — Complete non-empty evidence receipt

## Verdict

PASS

## Commit

Public-safe commit containing this report; the exact SHA is reported after
commit creation. All live identifiers, hashes, object versions, and connection
metadata remain in ignored mode-0600 private evidence files.

## Scope and fixture

Gate 1 uses only clearly labeled synthetic data for fictional Northstar Ocean
Lines. Exact retained bytes produced a verified USD 250/day tariff fact. A
synthetic later invoice claimed USD 350/day for seven days. Python recomputed a
USD 700 overcharge; no model selected the verdict or performed the arithmetic.

The operator explicitly approved this one synthetic receipt. The isolated
database records the existing fictional demo approver identity, while the
private execution record truthfully records that the operator's explicit
approval triggered the action. No real dispute was transmitted.

## Migration

`004_gate1_evidence_receipt.sql` adds:

- exact object-version and byte-size fields for tariff captures;
- structured tariff currency, unit, effective interval, locator, confidence,
  and deterministic verification fields;
- exact invoice-version and claimed-rate fields;
- finding-to-clause, calculation, recommendation, and human-approval fields;
- versioned canonical evidence-manifest fields on cases.

Migration DDL executes one statement per CockroachDB implicit transaction and
is restartable. Tracking uses conflict-safe insertion plus readback. The
migration executed from blank state in a fresh isolated recovery database; all
five repository migrations were tracked. The existing 90-day capture/evidence
GC configuration was not changed.

## Changed modules

- `src/core/receipt.py`: schema validation, deterministic retained-byte
  verification, invoice parsing, and overcharge calculation.
- `src/core/receipt_verifier.py`: pure fail-closed sealed-receipt verifier.
- `src/external/versioned_source.py`: exact S3 Version ID reader.
- `src/external/tariff_extract.py`: structured Bedrock proposal adapter.
- `src/external/migrate.py`: restartable Cockroach schema execution.
- `src/platform/receipt_pipeline.py`: temporal validation, conflict-safe input
  persistence, evidence binding, and idempotent filing.
- `src/platform/clerk_pipeline.py`: canonical evidence-content hashing.
- `src/platform/seal.py`: atomic non-empty canonical seal, human approval,
  ledger event, and validated idempotent replay.
- `src/platform/receipt_verifier.py`: tenant-scoped Cockroach/S3 verifier
  adapter.
- `scripts/gate1_receipt.py`: redacted full workflow replay command that never
  approves or seals.
- `scripts/gate1_verify.py`: redacted independent verification command.
- Synthetic fixtures and positive, negative, rollback, conflict, corruption,
  chronology, and idempotency tests.

## Executed evidence

### Fresh isolated migration

Redacted command:

```text
TALLY_GATE1_CRDB_DSN=<ISOLATED_PRIVATE_DSN> \
  python -c 'apply_all(dsn=<ISOLATED_PRIVATE_DSN>)'
```

Result:

- fresh isolated database created;
- five migrations applied and tracked;
- all required Gate 1 columns and the finding-to-clause constraint present;
- `AS OF SYSTEM TIME follower_read_timestamp()` read succeeded;
- schema changes completed; asynchronous schema-change GC was not treated as
  an application failure.

An earlier isolated attempt exposed a rate-unit/free-time parameter-ordering
defect before approval. That target was preserved privately as failed evidence,
the defect was corrected and regression-tested, and all acceptance evidence
below comes from a second fresh isolated database.

### Exact-version preparation and filing

Redacted command:

```text
AWS_PROFILE=<SCOPED_PROFILE> TALLY_GATE1_CRDB_DSN=<ISOLATED_PRIVATE_DSN> \
  python -m scripts.gate1_receipt \
  --bucket <PRIVATE_VERSIONED_BUCKET> \
  --tariff-key <PRIVATE_SYNTHETIC_OBJECT_KEY> \
  --tariff-version-id <PRIVATE_VERSION_ID> \
  --invoice-key <PRIVATE_SYNTHETIC_OBJECT_KEY> \
  --invoice-version-id <PRIVATE_VERSION_ID> \
  --dispute-date 2026-07-21
```

Both retained sources were reopened by exact Version ID for each attempt. Two
complete retrieval, extraction, deterministic verification, persistence, and
filing attempts returned:

```json
{
  "workflow_replay_passed": true,
  "receipt_state": "PREPARED",
  "full_attempts_executed": 2,
  "idempotent_rerun": true,
  "row_counts": {
    "tariff_snapshots": 1,
    "tariff_clauses": 1,
    "invoices": 1,
    "clerk_runs": 1,
    "findings": 1,
    "cases": 1,
    "case_evidence": 1,
    "ledger_events": 0
  }
}
```

### Explicit approval and atomic seal

The operator supplied the exact approval requested for this synthetic USD 700
receipt. One atomic seal transaction produced:

- case state `FILED`;
- finding approval state `APPROVED`;
- one sealed evidence row;
- canonical manifest version 1 with one evidence entry;
- non-empty manifest hash;
- non-null CockroachDB logical timestamp;
- exactly one ledger event.

An immediate second seal validated the stored receipt and returned idempotently
without a second ledger event.

### Post-seal full replay

The full exact-version workflow was executed twice again after the human seal:

```json
{
  "workflow_replay_passed": true,
  "receipt_state": "SEALED",
  "full_attempts_executed": 2,
  "idempotent_rerun": true,
  "ledger_events": 1
}
```

All receipt-table counts remained exactly one.

### Independent verifier

Redacted command:

```text
python -m scripts.gate1_verify \
  --aws-profile <SCOPED_PROFILE> \
  --tenant-id <PRIVATE_TENANT_ID> --case-id <PRIVATE_CASE_ID>
```

Result:

```json
{
  "passed": true,
  "checks_passed": 59,
  "checks_total": 59,
  "reason_count": 0
}
```

The verifier independently reloaded the sealed case, finding, evidence,
capture, clause, and invoice; reopened both exact object versions; recomputed
source, clause, content, calculation, and manifest hashes; reparsed the invoice
claim from retained bytes; and validated approval/seal state.

### Redacted SQL readback

Executed readback confirmed:

- verified non-null USD 250/day tariff rate;
- exact source version and hash present;
- effective, observed, and committed times present and not substituted for one
  another;
- USD 350/day invoice claim for seven days;
- USD 700 deterministic overcharge and `dispute_overcharge` recommendation;
- one finding, one case, one evidence row, and one ledger event;
- `APPROVED` human state and `FILED` case state;
- non-empty version-1 manifest and Cockroach HLC;
- an exact `AS OF SYSTEM TIME` read at the stored seal HLC returned the sealed
  case and manifest binding.

### Automated tests

```text
Python: 357 passed, 1 deprecation warning
UI: 15 passed, 0 failed
Ruff (all Gate 1 changed Python files): all checks passed
git diff --check: clean
```

The Python suite includes the contract's positive and negative cases for
absent clauses/rates, invalid intervals, source corruption, wrong versions,
empty evidence, altered manifests, tenant/case mismatch, transaction rollback,
conflicting idempotency keys, double seal, and ledger idempotency.

## Acceptance criteria

| Criterion | Result | Executed evidence |
|---|---|---|
| Rate comes from retained bytes | PASS | exact-version S3 read, structured extraction, deterministic source checks |
| Clause and rate verify | PASS | stored `VERIFIED`; negative absence/tamper tests |
| Finding binds exact evidence | PASS | tenant/capture/version/hash/clause/time/invoice/calculation receipt |
| Human approval separate from recommendation | PASS | explicit operator action; finding `APPROVED`; recommendation remains agent output |
| Non-empty canonical seal | PASS | manifest v1, one evidence item, non-empty hash |
| Independent verifier reopens exact versions | PASS | 59/59 checks |
| Negative tests pass | PASS | full 357-test Python suite |
| Second full run is idempotent | PASS | two post-seal full attempts; every row count 1; ledger count 1 |

## Privacy and publication

Private evidence is stored only under ignored `runtime-artifacts/` paths with
directory mode 0700 and file mode 0600. Public artifacts contain no real AWS
account/resource identifiers, bucket names, object keys, Version IDs,
production hashes, private URLs, cluster identifiers, DSNs, raw captures, or
real carrier data.

Pre-commit exact-private-value and generic secret scans passed. A full
reachable-history scan is repeated after commit creation before push.

## Exact remaining limitations

- This is a synthetic isolated recovery receipt; no real carrier dispute was
  sent and no production capture history was mutated.
- The product's current demo authentication model stores the fictional demo
  approver identity. The private execution evidence separately records that
  the operator's explicit approval triggered the seal.
- The one reported warning is an existing Starlette/httpx deprecation warning,
  not a Gate 1 functional failure.

## Independent result

PASS. An independent verifier found no blockers and confirmed:

- the fresh isolated migration and preserved historical-read behavior;
- exact-version source reopening and the deterministic USD 700 calculation;
- explicit approval, one-item canonical seal, one ledger event, double-seal
  idempotency, and exact-seal-HLC historical read;
- two unchanged post-seal full workflow replays;
- 59/59 live receipt-verifier checks;
- 357 full Python tests, 148 focused Gate 1 tests, 15 UI tests, Ruff, and
  `git diff --check` passing;
- ignored, mode-restricted private evidence and public-safe synthetic artifacts.

The reviewer classified the truth-labeled synthetic-only execution, fictional
demo approver identity, and absence of an externally transmitted dispute as
constraints rather than Gate 1 failures.

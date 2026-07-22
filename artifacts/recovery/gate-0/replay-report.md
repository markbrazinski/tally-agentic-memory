# Gate 0 replay report (sanitized)

## Method

The production capture history was replayed into an isolated database using
`restore_live.py --dsn-env-var TALLY_REPLAY_CRDB_DSN`. The DSN value was held
only in the environment and was not printed or placed in process arguments.
The migration runner was corrected so historically pre-runner migrations are
marked applied only if their schema markers already exist; a blank recovery
database executes them.

Redacted executed command:

```text
TALLY_REPLAY_CRDB_DSN=<REDACTED> python restore_live.py \
  --dsn-env-var TALLY_REPLAY_CRDB_DSN \
  --bucket <PRIVATE_VERSIONED_BUCKET> \
  --start-date <PRIVATE_START_DATE> --end-date <PRIVATE_END_DATE>
```

## Executed result

| Dataset | Source | Replay | Symmetric differences |
|---|---:|---:|---:|
| `recordings` | 51 | 51 | 0 |
| `tariff_snapshots` | 51 | 51 | 0 |

The first run committed 51 source-days with zero failed or skipped rows. The
same command exited successfully a second time and both tables remained at 51
rows. The source and replay canonical capture-metadata digests matched; the
production digest value is intentionally withheld from public Git history.

## Exact-version verification

All 51 current dated source bodies were retrieved by their recorded object
version and matched the manifest byte count, manifest SHA-256, and corresponding
database hash. The public executable example demonstrates the identical method
against three synthetic versions in `hash-verification.example.json`.

## Limitation

The verifier identity could not enumerate superseded versions because it did
not have object-version listing permission. Current dated objects were each
addressable and verified. This noncritical limitation was accepted for Gate 0.

# Gate 0 Report — Preservation and recoverability

## Verdict

PASS WITH LIMITATIONS

## Commit

Public-safe orphan commit containing this report; exact SHA is reported after
commit creation. Private evidence snapshot: local-only commit `f19968f` plus a
verified encrypted off-repository backup.

## What changed

- Preserved the complete private Gate 0 evidence without publishing it.
- Added secret-safe isolated replay DSN selection to `restore_live.py`.
- Corrected migration bootstrap detection so blank recovery databases execute
  pre-runner migrations.
- Published a sanitized schema, aggregate replay counts, redacted methodology,
  synthetic exact-version fixtures, and the independent result.
- Replaced real source, carrier, cloud, cluster, and connection identifiers in
  the public tree with fictional examples or explicit placeholders.
- Added private evidence/export patterns to `.gitignore`.

## Migrations and infrastructure changes

- No production migration was added and no production infrastructure was
  changed by the public reconstruction.
- Migration runner behavior changed only for blank-database bootstrap safety.
- Scheduled capture and verifier resources remained enabled during the private
  Gate 0 verification.

## Executed evidence

- Command/query: `TALLY_REPLAY_CRDB_DSN=<REDACTED> python restore_live.py --dsn-env-var TALLY_REPLAY_CRDB_DSN --bucket <PRIVATE_VERSIONED_BUCKET> --start-date <PRIVATE_START_DATE> --end-date <PRIVATE_END_DATE>`
- Environment: isolated recovery database against the retained private capture
  store; public branch tests use only synthetic fixture bytes.
- Result: first replay wrote 51 recordings and 51 tariff snapshots; symmetric
  differences were zero; the identical rerun left both counts unchanged.
- Artifact: `replay-report.md`, `row-counts.json`, and the private encrypted
  evidence snapshot.
- Command/query: `<project-venv>/bin/python -m pytest -q`
- Environment: clean public-safe orphan worktree.
- Result: `245 passed, 1 warning`.
- Artifact: Python test suite including executable public Gate 0 checks.
- Command/query: `node --test ui/src/**/*.test.mjs`
- Environment: clean public-safe orphan worktree.
- Result: `15 passed, 0 failed`.
- Artifact: UI provider test suite.
- Command/query: full-tree exact private-value comparison plus generic secret
  regex scan (commands retained in operator transcript; values never printed).
- Environment: public-safe orphan worktree.
- Result: `PRIVATE_PATTERN_SCAN=PASS`; `PRIVATE_UUID_SCAN=PASS`;
  `GENERIC_SECRET_SCAN=PASS`.
- Artifact: pre-commit scan output; reachable-history scan is repeated after
  commit creation.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Schema and application state exported | PASS | Sanitized `schema.sql` and aggregate `row-counts.json`; complete exports retained privately |
| Three exact object versions hash-verified | PASS | 51 production current versions verified privately; three executable synthetic examples published |
| Replay reconstructs capture metadata in isolation | PASS | 51/51 rows per capture table, zero semantic differences |
| Replay is idempotent | PASS | second run remained 51/51 |
| Scheduled capture plane operational | PASS | capture and verifier jobs enabled at read-back |
| Plan and billing state documented | PASS | complete evidence private; Basic plan disclosed publicly |
| Only-copy risk removed | PASS | local snapshot plus independently verified encrypted backup |
| Public branch excludes prohibited metadata | PASS | exact-value and generic secret scans passed before commit; history scan required before push |

## Negative tests

| Test | Expected | Actual | Result |
|---|---|---|---|
| Blank database bootstrap | pre-runner migrations are not falsely skipped | migration markers absent, migrations remain pending | PASS |
| Isolated DSN variable missing | fail without falling back to the source database | explicit `RuntimeError` | PASS |
| Replay run twice | no duplicate rows | counts unchanged at 51/51 | PASS |
| Synthetic fixture bytes altered | SHA/size check fails | executable checks compare exact bytes | PASS |
| Private-value scan | no production identifier/hash/version match | no matches | PASS |
| Private-export UUID scan | no live database identifier match | two reused fixture IDs replaced; rescan found no matches | PASS |
| Generic secret scan | no likely credential material | no matches | PASS |

## Claim impact

- Claims now supported: retained capture history is recoverable; capture
  metadata replay is equivalent and idempotent; public recovery methodology is
  reproducible without disclosing private evidence.
- Claims still prohibited: public access to production captures; enumeration of
  superseded object versions; full application-state reconstruction by the
  capture replay alone; any Gate 1 hero-receipt claim.

## Limitations

- The recovery identity cannot enumerate superseded object versions because it
  lacks list-version permission.
- Billing values are operator-recorded in the private snapshot.
- Object Lock/delete protection is not enabled.
- Replay reconstructs capture-derived records, while the full logical export
  remains the recovery source for other application tables.

## Recommended next action

Advance to Gate 1 after independent public-branch verification and push.

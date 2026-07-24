# Tally backend runbook — Gates 2–6 integrated hero loop

Public-safe operating procedure for the isolated backend hero loop. Contains no
credentials, cluster identifiers, object bindings, or connection details — those
live only in the operator's private record and in SSM (see the private
provisioning notes).

## What this covers

The integrated backend path exercised by `scripts/gate7_integrated_trace.py`:

```
invoice + claims + representative memory + tariff clause
  → reconstruction (7 sourced days, COMPLETE)
  → applicable rule ($250, real Distributed Vector Indexing; wrong-date rejected)
  → deterministic judgment (DISPUTE $700, 7 rows)
  → human approval + atomic seal (binds all inputs)
  → gated controlled send (fresh MCP/vector/source/no-fallback gates → controlled inbox)
```

## Prerequisites (operator, private channel)

- `TALLY_CRDB_DSN` for the isolated database (never committed; `.env` only).
- An AWS profile with Bedrock (Titan embeddings) access, e.g. `AWS_PROFILE=tally`.
- The isolated database `tally_gate2_iso` (created additively on the shared
  cluster; the protected hero `defaultdb` is never modified).

## Apply migrations (idempotent, additive)

```bash
python -c "from src.external.migrate import apply_all; print(apply_all(dsn=ISO_DSN))"
```

Migrations 001a–015 build the full schema on a blank isolated database. Each is
`CREATE ... IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` and is safe to re-run.

## Run the integrated hero loop

```bash
AWS_PROFILE=tally python scripts/gate7_integrated_trace.py
```

The script performs its own **reset/reseed** (idempotent DELETE by the Gate-7
tenant) before each run, so it is safe to run repeatedly. Expected terminal:
`final_invoice_status: DISPUTED`, `mock_fallback: false`, exit 0.

Three consecutive clean runs were recorded (`artifacts/gate-7/g7_{1,2,3}.json`).

## Per-gate traces (isolated proofs)

| Gate | Script | Proves |
|---|---|---|
| 2 | `gate2_isolated_trace.py` / `gate2_isolated_negative.py` | reconstruction 7/7 COMPLETE; missing source → NEEDS_EVIDENCE |
| 3 | `gate3_isolated_trace.py` | real vector index selected; wrong-date distractor rejected |
| 4 | `gate4_isolated_trace.py` | $700 DISPUTE / $875 APPROVE / REQUEST_EVIDENCE; deterministic replay |
| 5 | `gate5_isolated_trace.py` | approval bound to frozen version; atomic seal; stale/replay handling |
| 6 | `gate6_isolated_trace.py` | gated send to controlled inbox; forced source failure blocks send |

## Privacy scan (before any public push)

```bash
python scripts/gate7_privacy_scan.py    # exit 0 = clean
```

Scans all tracked files for real secret values (credentialed DSNs, AWS keys,
private-key blocks, cluster hostname, bearer values). Prose mentions are not
flagged.

## Test suite (zero network)

```bash
python -m pytest -q      # expected: 815+ passed; all externals mocked
ruff check <changed>     # clean on changed files
```

## Teardown

Isolated teardown is separately approval-gated. Confirm the target is exactly
`tally_gate2_iso`, capture a before/after readback, and never touch `defaultdb`
or any other `tally_*` database.

## Known deferrals (not backend blockers)

- **Live Managed MCP read**: the isolated lineage has no provisioned Managed MCP
  endpoint (SSM holds only hero-scoped MCP config). Reconstruction memory is read
  driver-diagnostic; the live MCP sponsor read is deferred until an isolated MCP
  endpoint exists.
- **Real external send**: no owner-approved external recipient/provider; delivery
  is to the in-process controlled demonstration inbox only.
- **Frontend + video/Devpost**: the queue/workbench UI and submission artifacts
  are the integrated frontend workstream.

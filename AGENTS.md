# Tally engineering governance

This file is the public-safe agent instruction source for the repository. It
preserves the engineering and evidence rules used by other coding assistants
without publishing private infrastructure, account, source, or strategy data.

## Product and execution authority

Tally is a recovery-oriented flight recorder for demurrage and detention
disputes. It records retained source observations and binds later decisions to
the exact evidence that was available at the time.

Recovery work proceeds gate by gate under the authorized P0 recovery execution
contract. Do not broaden product scope or begin a later gate until the current
gate passes or the operator explicitly accepts a noncritical limitation. Each
gate gets one bounded commit and an evidence-backed Gate Report.

## Architecture

- Python 3.12 and FastAPI.
- `src/core`: pure domain functions and models; no external I/O.
- `src/external`: CockroachDB, AWS, Bedrock, S3, MCP, and embedding adapters.
- `src/platform`: application routes and workflow orchestration.
- Raw parameterized SQL; no ORM or query builder.
- Every function belongs to one layer. Cross-layer I/O goes through an explicit
  adapter or protocol.
- Tests mock external calls and make zero network requests.

## Data and proof rules

- Retained bytes come first. Never invent, reconstruct, or silently substitute
  missing evidence.
- Effective time, observed time, and CockroachDB system/commit time are distinct
  domains. Never use one as a substitute for another.
- An exact object Version ID and SHA-256 bind source evidence whenever versioned
  object storage is involved.
- LLM output is always schema-validated. A quoted extraction must occur in the
  retained source; otherwise it is unverified and the workflow abstains.
- Python decides eligibility, calculations, and verdicts. Models may extract or
  draft prose but never make the authoritative decision.
- Findings, cases, evidence, approvals, and ledger effects flow through the real
  workflow. Never seed or hardcode outcome rows.
- Sealed evidence uses deterministic, versioned canonical serialization and a
  non-empty evidence set.
- Every new database action remains tenant-scoped and audit-visible.

## Code and schema style

- Type hints throughout and explicit boundary validation.
- SQL values are bound parameters; never interpolate data into SQL strings.
- CockroachDB retry handling covers the whole transaction body for SQLSTATE
  `40001`.
- Migrations are additive, ordered, idempotently tracked, and executable against
  a blank recovery database.
- Keep changes minimal and load-bearing. A schema field, route, or test is not
  evidence until it has executed in the target or a clean reproducible
  environment.

## Security and public-history rules

Never commit credentials, DSNs, private keys, tokens, raw private captures,
database exports, real source URLs, production hashes or object versions,
private cloud/database identifiers, or live carrier data. Public fixtures must
be clearly labeled synthetic and use fictional names and identifiers.

Private recovery paths and export patterns stay ignored. Before any public
push, scan every commit reachable from the branch for secrets and prohibited
metadata, not just the working tree.

## Scope locks

Do not add:

- UI redesigns or new UI components during recovery gates;
- write access through MCP;
- new integrations, SSO, multi-region configuration, or roadmap features;
- ORM layers;
- LLM-decided verdicts or synthesized evidence;
- client-side persistence as a substitute for durable system state;
- hardcoded demo outputs, carrier/case/filename branches, or retry-until-success
  behavior.

## Testing and operations

- Add positive and negative tests for every gate criterion, including rollback,
  idempotency, corruption, mismatch, and fallback paths where applicable.
- Keep the full test suite green; no test performs live network I/O.
- Scheduled infrastructure is not proven by configuration alone: require an
  observed unattended run when the contract calls for it.
- A deployment is complete only after live state is read back and asserted
  against intent.
- Preserve the capture plane while recovery work proceeds.

## Definition of done

The current gate's implementation, migrations, tests, and executed evidence are
complete; prohibited claims remain truth-labeled; the diff is scoped; the
repository and reachable history are clean; independent verification passes;
and the bounded gate commit and Gate Report are produced.

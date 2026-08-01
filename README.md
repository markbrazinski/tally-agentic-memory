# Tally

Tally remembers the evidence that was true when a freight dispute was filed,
then retrieves and replays that dated record when the carrier contests it
later.

**SYNTHETIC DEMO — FICTIONAL DATA.** No real dispute is sent. No carrier was
contacted, no credit was recovered, and no legal determination is claimed.
All public names, invoices, cases, tariffs, dates, and amounts are fictional.

## Judge access

The hosted demo URL and sign-in credentials are provided with the Devpost
submission, not here — the deployment is access-controlled and this repository
is public.

Sign-in is Amazon Cognito. The browser holds no credential of its own: the
session is an httpOnly cookie and every request — pages, API reads, PDF bytes,
the SSE stream — is validated server-side.

## What the demo shows

The queue holds three fictional invoices that end in three different states,
because the evidence differs in each case:

| Invoice | Outcome | Why |
|---|---|---|
| INV-1041 | `APPROVED FOR PAYMENT` | the charged rate matches the applicable recorded tariff |
| INV-1047 | `NEEDS EVIDENCE` | no governing tariff was recorded, so Tally refuses to conclude |
| INV-1048 | `DISPUTED $700` | invoiced $350/day against a verified $250/day tariff, over 7 sourced days |

The hero case is INV-1048. A $2,450 demurrage invoice arrives; Bedrock extracts
the carrier's claims and versioned S3 preserves the exact original. The
reconstruction agent reads pre-invoice shipment memory through CockroachDB
Managed MCP, constrained to what was on record *before* the invoice existed.
Distributed Vector Indexing retrieves the governing tariff clause, and
deterministic code — not the model — verifies its effective date, scope,
language, and rate. Seven charged days are adjudicated at $100/day difference,
and a human authorizes the $700 dispute in one sealed transaction.

`NEEDS EVIDENCE` is the point, not a gap: incomplete memory withholds authority
rather than guessing.

> Versioned S3 retains the dated source artifact. Within CockroachDB's
> configured MVCC window, Tally can also replay the transactional case state at
> filing.

Tally does not claim indefinite CockroachDB time travel. The configured MVCC
window is 90 days; exact versioned S3 objects provide the longer-lived source
binding.

## Demo limitations

- **All data is fictional.** Carriers, terminals, containers, tariffs, invoices
  and amounts are synthetic. No carrier is contacted and no money moves.
- **Outbound mail is a demonstration provider.** The send path is real and
  gated, and returns a real receipt id, but no email leaves the system.
- **The filmed hero is seeded.** INV-1048's decision chain is seeded to a
  known-good state for reliable filming. The pipeline that produces it is the
  real one; the deployed workers run for genuinely imported invoices.
- **One tenant, one judge account.** This is an isolated demonstration lane.

## Architecture

```text
Logged-out browser
  └── AWS App Runner (one same-origin Vite UI + FastAPI service)
        └── GET /public/demo/hero (fixed inputs, rate-limited, bounded timeout)
              ├── CockroachDB current read + AS OF SYSTEM TIME replay
              ├── CockroachDB Managed MCP fixed select_query
              └── Amazon S3 exact GetObjectVersion receipt verification

Deployment agent
  └── ccloud cluster list -o json
        └── blocks deployment unless one expected Basic cluster is operational

Authenticated intake/vector code (not exposed to the logged-out judge)
  └── Amazon Bedrock bounded extraction/embedding adapters
```

The public endpoint accepts no tenant ID, case ID, contest ID, timestamp, SQL,
object version, or prompt. Generic reads and all mutations remain outside the
logged-out provider. A dependency outage produces `unavailable` with
`mock_fallback: false`.

## Sponsor tools and services

### CockroachDB Cloud Managed MCP Server

The deployed judge server uses an interactive OAuth bootstrap requesting `mcp:read`
only. The renewable token bundle is held only in AWS SSM Parameter Store; no
Cluster Operator/Admin API-key fallback is accepted by the deployed judge
runtime. The older private Gate 3 operator path is not deployed. Application code exposes
only a fixed `select_query` that loads and revalidates the sealed receipt for
the server-selected later contest. It is application-filtered, not database
RLS.

The private Gate 5B proof showed repeated refresh,
real-time near-expiry renewal, the fixed hero read after refresh, and explicit
server denial for a safely shaped `insert_rows` probe against a fresh
nonexistent table. Any ambiguous or executable write path stops publication.
The deployed manager refreshes on demand below five minutes or once after a
401, never after a 403. A one-item DynamoDB conditional lease prevents
overlapping processes from consuming the same rotating refresh token.

The direct historical replay and Managed MCP retrieval are separate proofs:
the former reconstructs the database at the stored transaction timestamp; the
latter retrieves the sealed memory for the later contest. MCP failure never
falls back to direct SQL while being labeled MCP.

### ccloud CLI

`scripts/gate5_ccloud_preflight.py` runs the fixed structured command `ccloud
cluster list -o json`. The deployment agent parses JSON and blocks deployment
unless exactly one private expected cluster is `CREATED` on the `BASIC` plan.
This proves control-plane authentication and target readiness only—not SQL or
MCP data-plane health. The browser cannot invoke ccloud.

### AWS

- **AWS App Runner** is the authorized target for the combined public UI and API.
- **Amazon S3** returns the exact object versions named by the sealed receipt;
  the public route returns only whether all receipt checks passed.
- **Amazon Bedrock** supports the existing bounded extraction and Titan Text
  Embeddings V2 adapters. Models never decide eligibility or a verdict, and the
  public API accepts no prompt.
- **SSM Parameter Store** holds the DSN, renewable OAuth bundle, and private selectors.
- **DynamoDB** holds only the short-lived owner/expiry lease used to serialize OAuth rotation.
- **AWS Budgets** sends investigation-only alerts at $15, $25, $40, and $50
  against the authorized $50 cumulative ceiling. It has no automatic stop
  action.
- **EventBridge Scheduler** initially deletes the App Runner service at the end
  of September 30, 2026; the remaining secrets, image, roles, and OAuth grant
  are removed by the documented teardown checklist that day.

## Local verification

Prerequisites: Python 3.12, Node.js 24, npm, and `make`. The automated suite
makes no network requests and needs no cloud credentials.

```bash
# Backend: full suite, no network, no cloud credentials required.
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest              # 898 tests
.venv/bin/python -m ruff check src/     # shipped runtime is lint-clean

# Frontend: ui-next/ is the deployed SPA (the older ui/ is not shipped).
npm --prefix ui-next ci
npm --prefix ui-next run build
```

Every external call is mocked in the suite — Bedrock, Managed MCP, S3, and
Cognito all have test doubles, so `pytest` runs offline and needs no AWS
account. The frontend has no unit-test suite; `ui-next/tests/audit.spec.js` is
a Playwright accessibility audit that requires a running server.

The SPA defaults to the live provider and talks to the FastAPI app on the same
origin. Append `?provider=mock` to run the offline demo scene without a
backend.

Run the API locally only with an isolated synthetic database:

```bash
cp .env.example .env
# Populate .env privately; never commit it.
set -a; source .env; set +a
TALLY_PUBLIC_DEMO_ENABLED=true .venv/bin/uvicorn src.platform.app:app --reload
```

Open `http://127.0.0.1:8000/public/demo/hero`. Missing dependencies return a
safe 503 response rather than sample memory.

## Database setup and reset

Migrations are additive and tracked in `migrations/`. Apply them to a blank
isolated database with:

```bash
TALLY_CRDB_DSN='<private isolated DSN>' .venv/bin/python -m src.external.migrate
```

The synthetic tenant seed is idempotent:

```bash
TALLY_CRDB_DSN='<private isolated DSN>' .venv/bin/python -m src.external.seed_demo_tenant
```

The public repository intentionally does not include a live DSN, tenant/case
IDs, source keys, object Version IDs, hashes, or source bodies. Re-run the seed
and the Gate 1–4 workflows against your own fictional versioned inputs; do not
invent substitute evidence when a retained object is missing.

For a clean reset, delete only the isolated database/resources you created,
create a new blank database, rerun migrations and seed, then rebuild the
receipt through the real workflow. Never relabel fixtures as executed data.

## Deployment

Deployment requires a scoped AWS deployer, Docker, AWS CLI, ccloud CLI, and
`jq`. Exact identifiers and credentials stay in ignored private environment
configuration.

1. Run `scripts/gate5_ccloud_preflight.py` with the private expected cluster
   ID. A failure stops the deployment.
2. Complete the narrow OAuth bootstrap and the mandatory repeated-refresh,
   near-expiry, MCP write-denial, and fixed-read proof.
3. Export the private server values named in `.env.example` plus a JSON array
   of the exact S3 object ARNs as `TALLY_EVIDENCE_OBJECT_ARNS_JSON`.
4. Run `scripts/gate5_provision_aws.sh`. It creates two bounded App Runner
   roles, six configuration SecureStrings, one tagged ECR repository, and the
   conditional refresh-lease table. The already proven OAuth bundle remains a
   seventh SecureString and is never copied into an environment variable.
5. Commit and scan the exact public tree, then run `deploy_app.sh` from that
   clean commit. It builds `linux/amd64`, deploys the combined service, reads
   back image/readiness/public-ingress state, and verifies the live hero.
6. Set the private budget email and run `scripts/gate5_guardrails.sh` before
   making the URL public.

No deployment script accepts a browser credential or prints a secret. The
OAuth grant is server-side material and must be revoked on teardown.

## Expected cost and teardown

Gate 5 has an owner-authorized AWS ceiling of **$50 total**. The live budget
is one non-resetting `CUSTOM` period beginning July 1 and remaining active
through the eventual teardown. The design uses
one minimum-size App Runner service, a small ECR image, existing versioned S3
objects, and bounded requests. Before deployment, the service-scoped budget
must be read back with investigation-only alerts at $15, $25, $40, and $50;
unrelated account spend must not be presented as Gate 5 spend. The alerts do
not pause, delete, or otherwise interrupt judge access.
No paid CockroachDB plan or AWS purchase is required.

Post-update readback on July 22 showed $4.484 accumulated and an AWS-generated
custom-period forecast of $8.892. A more conservative bottom-up estimate adds
$5.92–$7.17 through the initial September 30 teardown, producing approximately
$10.40–$11.65 cumulative. This is below the current $50 authorization. Alerts
still require investigation, but do not automatically interrupt access. The
budget is service-filtered rather than resource-tag isolated, so it may
conservatively include other account use of the same seven services. See the
official [App Runner pricing](https://aws.amazon.com/apprunner/pricing/),
[ECR pricing](https://aws.amazon.com/ecr/pricing/), [Parameter Store
pricing](https://aws.amazon.com/systems-manager/pricing/), and [AWS Budgets
pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/).

The judge URL is authorized to remain available until seven calendar days after
winners are announced. The initial teardown is September 30, 2026 at 11:59 PM
America/Los_Angeles. If winners have not been announced, rerun the guardrail
before that time with the next seven-day date—for example,
`TALLY_GATE5_TEARDOWN_DATE=2026-10-07`—and continue in seven-day increments.
Once winners are announced, retain the first scheduled increment that is at
least seven calendar days later. On the resulting teardown date:

1. Confirm the scheduled App Runner deletion executed.
2. Delete the Gate 5 ECR repository and its images.
3. Delete `/tally/gate5/*` SSM parameters and the OAuth refresh-lease table.
4. Delete the three `tally-gate5-*` IAM roles and their inline policies, plus
   the Gate 5 budget and schedule.
5. Revoke the Gate 5 CockroachDB OAuth grant and dynamic client.
6. Read back every deletion and preserve only sanitized aggregate evidence.

The existing historical/private Tally repository and recovery evidence are not
part of this teardown.

## Troubleshooting

- `public_demo_disabled` or `configuration_unavailable`: set only the required
  server-side selectors; never put them in Vite variables.
- `cockroach_or_s3_unavailable`: check private DB/S3 connectivity and exact
  version permissions. Do not fall back to current objects.
- `evidence_verification_failed` or `evidence_binding_mismatch`: stop; the
  retained receipt did not verify.
- `mcp_memory_unavailable`: verify the server-side OAuth bundle, refresh lease,
  cluster header, and fixed read. Do not substitute the direct database result
  as MCP output.
- ccloud preflight failure: treat control-plane readiness as false; raw cluster
  details remain private.
- Bedrock failure: the bounded model task abstains/is unavailable; Python still
  owns eligibility and calculations.

## Truth and security boundaries

- No real carrier identities, customer data, external send, credit, recovery,
  resolution, legal ruling, or production-readiness claim.
- No claim of RLS, tenant-scoped DB credentials, physical immutability,
  indefinite time travel, or request-exact MCP server auditing.
- No credentials, DSNs, private IDs, hashes, object versions, source URLs, or
  raw source material in the browser, examples, Git history, or reports.
- Synthetic evaluation is a deterministic test harness, not production
  performance.

Public-safe example outputs are under `artifacts/recovery/`; private executed
evidence stays ignored under mode-restricted `runtime-artifacts/`.

Licensed under the [MIT License](LICENSE).

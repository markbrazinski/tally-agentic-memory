# Tally

Tally remembers the evidence that was true when a freight dispute was filed,
then retrieves and replays that dated record when the carrier contests it
later.

**SYNTHETIC DEMO — FICTIONAL DATA.** No real dispute is sent. No carrier was
contacted, no credit was recovered, and no legal determination is claimed.
All public names, invoices, cases, tariffs, dates, and amounts are fictional.

Demo URL: **https://x69yr3tibq.us-east-1.awsapprunner.com/**

## What the judge path proves

The fixed public hero shows one fictional Northstar/Asterline case. The sealed
case was `FILED`; after a fictional later challenge it is `CONTESTED`. The
recorded tariff is $250/day and the later fictional claim is $350/day.

The server—not the browser—selects the tenant, case, contest, CockroachDB
transaction timestamp, SQL templates, and exact S3 object versions. The public
response exposes none of those identifiers. It returns only the states, rates,
verification booleans, retention wording, and explicit non-claims.

> Versioned S3 retains the dated source artifact. Within CockroachDB's
> configured MVCC window, Tally can also replay the transactional case state at
> filing.

Tally does not claim indefinite CockroachDB time travel. The configured MVCC
window is 90 days; exact versioned S3 objects provide the longer-lived source
binding.

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

Before deployment, the private Gate 5B proof must show repeated refresh,
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
- **AWS Budgets** must alert at 80% and 100% of the authorized $10 ceiling before deployment.
- **EventBridge Scheduler** must delete the App Runner service on September 22,
  2026; the remaining secrets, image, roles, and OAuth grant are removed
  by the documented teardown checklist that day.

## Local verification

Prerequisites: Python 3.12, Node.js 24, npm, and `make`. The automated suite
makes no network requests and needs no cloud credentials.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
npm --prefix ui ci
npm --prefix ui test
npm --prefix ui run build
.venv/bin/python -m scripts.gate4_evaluation
```

The default UI provider is the credential-free public provider. Until live
server configuration exists, it truthfully renders unavailable. Add `?film=1`
to the local URL to inspect the explicitly labeled synthetic film; film data
is never substituted into public live mode.

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

Gate 5 has a hard owner-authorized AWS ceiling of **$10 total**. The design uses
one minimum-size App Runner service, a small ECR image, existing versioned S3
objects, and bounded requests. Before deployment, the service-scoped budget
must be read back with alerts at $8 and $10; unrelated account spend must not
be presented as Gate 5 spend. It is monitoring, not a guarantee that AWS
automatically stops every charge.
No paid CockroachDB plan or AWS purchase is required.

At current us-east-1 pricing, the fixed App Runner idle-memory floor for 0.5 GB
from July 22 through September 22 is about $5.21 (`0.5 × $0.007 × 24 × 62`).
With the service capped at one instance, low judge traffic, a small ECR image,
standard SSM parameters, and bounded model calls, the working estimate is
$5.25–$6.50 total. See the official [App Runner pricing](https://aws.amazon.com/apprunner/pricing/),
[ECR pricing](https://aws.amazon.com/ecr/pricing/), [Parameter Store
pricing](https://aws.amazon.com/systems-manager/pricing/), and [AWS Budgets
pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/).

If Gate 5 passes, the judge URL is intended to remain available through September 16,
2026. On September 22,
2026:

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

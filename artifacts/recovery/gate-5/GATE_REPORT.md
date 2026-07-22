# Gate 5 Report — AWS deployment and judge access

Overall verdict: **BLOCKED**

The Gate 5B renewable OAuth path passes its safety and lifecycle checks. Gate 5
as a whole remains blocked for two independent reasons: the final clean
deployment fails closed because the original CockroachDB seal timestamp is now
below a replica GC threshold, and the designated code repository is private.
Its reachable commit metadata contains a personal author email, beginning with
the parentless root commit, so the complete history does not meet the
authorized public-safety policy. No ref was deleted or rewritten, the
repository was not made public, and neither blocker is treated as a
noncritical limitation.

## Release identity

- Starting public-safe root: `7bde12f14c871406bec9f47988de388311a53eb1`
- Reviewed Gate 5 implementation: `cbc544da4446452735c5a33098b71fe48e3f4a69`
- Final reviewed private-repository tree after the clean-clone fixture fix:
  `7ae11b7571e5d8f2eee4636c17aab07533c8930d`
- Report/publication commit: the commit containing this file; its exact local
  and remote SHA is verified in the post-commit handoff because a commit cannot
  embed its own SHA.
- Designated repository:
  `https://github.com/markbrazinski/tally-agentice-memory`
- Repository visibility at the gate boundary: **PRIVATE**
- Deployed judge URL:
  `https://x69yr3tibq.us-east-1.awsapprunner.com/`

The existing `markbrazinski/tally-agent` repository and its refs were not
changed by Gate 5.

## Official-rule verification

The live official rules, resources, and dates pages were retrieved again on
2026-07-22:

- <https://cockroachdb-ai.devpost.com/rules>
- <https://cockroachdb-ai.devpost.com/resources>
- <https://cockroachdb-ai.devpost.com/details/dates>

No material change from the commission was found. The rules still require an
AWS-deployed agentic application with CockroachDB persistent memory, at least
two named CockroachDB tools, at least one meaningful AWS service, a public
open-source repository, a functional demo URL, and a public video shorter than
three minutes. The submission deadline remains August 18, 2026 at 5:00 PM EDT;
judging remains August 19 through September 15.

## Deployed architecture and eligible-tool use

```text
Logged-out browser
  -> AWS App Runner: fixed Vite UI and FastAPI boundary
       -> CockroachDB: current state and AS OF SYSTEM TIME replay
       -> Managed MCP: fixed read-only sealed-memory query
       -> versioned Amazon S3: exact receipt-object verification
       -> SSM SecureString: server-side OAuth/configuration
       -> DynamoDB: one expiring refresh-rotation lease

Deployment agent
  -> ccloud CLI structured cluster readiness preflight
  -> ECR linux/amd64 image
  -> AWS Budgets and EventBridge Scheduler guardrails
```

The two eligible CockroachDB tools are load-bearing:

1. **CockroachDB Cloud Managed MCP Server** retrieves the fixed sealed receipt
   for the later fictional contest. MCP failure returns unavailable; it is not
   replaced by a hidden SQL result.
2. **ccloud CLI** supplies structured control-plane readiness. Deployment is
   blocked unless exactly one privately selected Basic cluster is operational.

CockroachDB itself persists the case and replays `FILED` at the stored filing
transaction timestamp versus `CONTESTED` now. This is distinct from the MCP
retrieval. Amazon S3 supplies the exact versioned evidence binding, and App
Runner hosts the live agent path. Existing bounded Bedrock extraction and
Titan embedding adapters remain outside the logged-out fixed hero; the browser
cannot submit prompts.

## Credential and permission boundary

- The browser receives no CockroachDB DSN, OAuth bearer, AWS credential,
  private selector, object key/version, private hash, or infrastructure ID.
- The deployed judge accepts only server-selected fixed inputs. It exposes no
  generic SQL, MCP proxy, prompt, mutation, caller-selected tenant/case, or
  replay timestamp.
- OAuth bootstrap requested exactly `mcp:read`. Stored bundles with any other
  scope are rejected. No Cluster Operator/Admin API-key fallback is deployed.
- SSM holds the renewable bundle as a SecureString. App Runner receives only
  its parameter name, not a token environment variable.
- The runtime can get/replace only that bundle, conditionally acquire/release
  the one DynamoDB lease item, read six exact configuration parameters, and
  read the two exact S3 receipt objects.
- One 401 can refresh and replay once; 403 never refreshes. Refresh, persistence,
  rotation, lease, or repeated-authentication failure fails closed.

## Executed judge path and final regression

Before the final clean image update, three consecutive logged-out hero
requests passed with stable public fields:

- classification: `SYNTHETIC DEMO — FICTIONAL DATA`
- status: `executed`
- mock fallback: `false`
- historical/current state: `FILED` / `CONTESTED`
- exact versioned S3 verification: `true`
- Managed MCP status: `verified_read`

Those executed public responses reported the fictional recorded rate of
$250/day against a
later fictional $350/day claim. It makes no real send, recovery, carrier,
credit, resolution, or legal claim. The 90-day CockroachDB MVCC limit and the
longer-lived versioned S3 role remain explicit.

The final commit-SHA App Runner update itself succeeded, `/healthz` and
`/readyz` returned HTTP 200, and exact S3 receipt verification still passed.
The hero then returned the designed safe HTTP 503 projection with
`mock_fallback: false`. A private split diagnostic showed:

- stored seal age: approximately 47 hours;
- all four replay tables currently report 90-day GC TTLs;
- S3 receipt verification: pass;
- historical replay: CockroachDB `XXUUU`, batch timestamp below replica GC
  threshold.

The likely sequence is that the 90-day policies were installed after a prior
shorter GC threshold had already advanced. Raising a TTL cannot resurrect an
already collected MVCC version. The original stored timestamp was not changed,
and no newer timestamp/current row was substituted as historical evidence.
Managed Basic backups exist, but CockroachDB documents Basic restore as a full
cluster operation requiring a completely wiped destination. No destructive
restore or additional cluster was authorized or attempted.

Gate 5B additionally passed two immediate refresh grants, observed refresh
rotation, simulated expiry, a 3,006-second real-clock near-expiry refresh, and
a deployed forced-safety-window refresh. Every checked post-refresh hero read
and sealed receipt passed. The accepted non-mutating write probe remained
denied. See `GATE_5B_REPORT.md` for its bounded limitations.

## Verification matrix

| Check | Executed result |
|---|---|
| Full Python suite | PASS — 663 tests; one dependency deprecation warning |
| Full UI suite | PASS — 23 tests |
| Vite production build | PASS; existing non-module `support.js` warning |
| Locked Gate 4 evaluation | PASS — 10/10 |
| Changed Python Ruff/compile | PASS |
| Git diff integrity | PASS |
| linux/amd64 container build | PASS |
| App Runner clean image update/configuration/readiness | PASS |
| Three pre-final logged-out hero runs | PASS |
| Final clean logged-out hero | BLOCKED — safe 503; original AOST below GC threshold |
| Deployed OAuth expiry/rotation probe | PASS |
| Exact live-token repository scan | PASS — zero token-value findings |
| Candidate tree/all-ref safety scan | BLOCKED — one personal email in root commit metadata |
| Prohibited exact S3 ARN scan | PASS — no exact object ARN in tree/history |
| Synthetic PDF manual review | PASS — exact reviewed digest allowlisted |
| Authenticated clean clone | PASS — 663 Python, 23 UI, production build |
| Unauthenticated repository clone | BLOCKED — repository intentionally remains private |

Unit/integration coverage also exercises dependency failure, no mock fallback,
wrong/unsealed/unknown cases, caller-input impossibility, fixed timeouts/rate
limits, concurrent OAuth refresh, restart, persistence failure, write denial,
scope expansion, and safe errors.

## Cost, monitoring, and teardown

The live budget is scoped to the seven AWS services used by Gate 5 rather than
the entire shared account. Readback on 2026-07-22 showed:

- authorized ceiling: **$10.00**
- current scoped spend: **$4.484**
- current AWS forecast: **$4.558**
- actual-spend alerts: **80% and 100%**
- App Runner maximum instances: **1**
- teardown scheduler: **enabled for September 22, 2026**

The budget is an alert, not an automatic universal spend stop. The judge path
is intended to remain available through September 16. Teardown must also
remove ECR, parameters, the lease table, bounded roles/policies, budget and
schedule, then revoke the OAuth grant and dynamic client.

## Independent reviews

- The independent release audit found no credential, DSN, token, AWS ARN,
  private S3 identity/version, or raw capture material in the reviewed public
  files. It confirmed the fixed browser/API surface and OAuth fail-closed path.
- The independent audit identified the missing aggregate report, clarified the
  historical private Gate 3 token path versus the deployed OAuth-only runtime,
  required exact stored `mcp:read` scope, and corrected teardown wording. All
  four findings were resolved before this report.
- Automated history scanning independently confirms the one remaining
  publication blocker; it is not suppressed or relabeled as safe.
- A first authenticated clean clone exposed one ignored synthetic Gate 0
  inventory fixture. The ignore rule received one exact exception, the
  fictional fixture was scanned and committed, and a second fresh clone passed
  all Python/UI/build checks.

## Limitations and prohibited claims

- Gate 5 is **not complete or eligible for submission** until the original
  AOST proof is truthfully recovered or a separately authorized full synthetic
  reseed/replay is executed from retained exact source evidence, and the final
  logged-out hero passes again.
- The repository must also be public, cloneable without permission, and have
  complete reachable history approved as public-safe.
- OAuth refresh is metadata-discovered and observed live; CockroachDB has not
  published a judging-period refresh-token durability guarantee.
- The observed write denial is an exact accepted in-band MCP error fingerprint,
  not an HTTP `insufficient_scope` response.
- Tenant isolation is application-enforced, not database RLS. This is not
  request-exact server-side MCP auditing or production deployment.
- No paid-plan upgrade, real external sending, production/customer data,
  arbitrary MCP proxy, or private-evidence publication occurred.

Gate 6 demo scripting, video work, Devpost submission, production onboarding,
and new product scope are not authorized by this report.

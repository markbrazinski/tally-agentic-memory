# Gate 3 Report — runtime CockroachDB Managed MCP retrieval

## Status

`PASS WITH LIMITATIONS` — the live functional matrix passed and was
independently verified. CockroachDB Cloud did not expose a data-plane audit
record or server request identifier that could independently correlate the
successful `select_query` call. Gate 4 remains blocked until this noncritical
audit limitation is explicitly accepted.

All identifiers, timestamps, hashes, source versions, actor details, SQL rows,
and infrastructure configuration remain in ignored mode-restricted private
evidence. This report contains only sanitized configuration and aggregate
results.

## Bounded outcome

The later-contest demo path now retrieves the previously filed synthetic case
through the real CockroachDB Cloud Managed MCP endpoint. It verifies the
complete sealed manifest and current database records before projecting only
the case, invoice, finding, exact evidence-version bindings, recorded rate and
clause, approver, seal time, and current state needed to answer the contest.

There is no ordinary database fallback. An MCP outage returns a recoverable
unavailable result with no invented memory. The implementation does not add a
product integration or deployed contest event route; it is the contract's
bounded application/demo runtime path.

## Redacted MCP configuration

```text
endpoint=https://cockroachlabs.cloud/mcp
cluster=<private-target-cluster>
database=<private-recovery-database>
identity=<private-oauth-actor>
oauth_scope=mcp:read
permission_mode=oauth-read-only
token_storage=<ignored-mode-0600-private-artifact>
```

The OAuth helper uses Authorization Code with PKCE S256, requests only
`mcp:read`, rejects `mcp:write`, requires a Bearer token with a positive
lifetime no greater than 24 hours, and never prints the token. The executed
token reported a one-hour lifetime. Its bearer value was destroyed immediately
after the successful live run; the private artifact retains only scope,
lifetime, acquisition, and destruction metadata.

## Permission and tenant model

The provider's cluster-level catalog advertised `select_query`, read-only
inspection tools, and three known write tools. Catalog advertisement is not a
permission grant. The adapter exposes only `select_query`, accepts one
semicolon-free `SELECT` statement, and has no application method for any
advertised write tool. The Gate 3 application query builder further restricts
that surface to its fixed shape and canonical UUID inputs.

An empty `insert_rows` probe contained no database, table, columns, or row data.
The live server returned `isError=true` with explicit `write` + `access` +
`permission` denial semantics. Plain missing/invalid-argument errors do not
count as authorization evidence in the implementation or tests.

Tenant isolation is enforced by the fixed query's tenant predicate and
application-issued tenant context. It is not CockroachDB RLS and is not a
tenant-scoped database credential. The wrong-tenant live query returned zero
rows; this proves application-query isolation only.

## Executed request and structured response

The public-safe command shape was:

```text
TALLY_MCP_CLUSTER_ID=<private> \
TALLY_MCP_DATABASE=<private> \
TALLY_MCP_ACCESS_TOKEN=<private-mode-0600-token> \
TALLY_MCP_SERVICE_IDENTITY=<private> \
TALLY_MCP_PERMISSION_MODE=oauth-read-only \
TALLY_GATE3_*=<private-synthetic-identifiers> \
python -m scripts.gate3_mcp_retrieval
```

The detailed request bindings and structured results remain private. The
sanitized executed output was:

```text
functional_passed=true
hero_receipt_found=true
cross_tenant_query_empty=true
unknown_not_found=true
unsealed_not_presented=true
known_write_tools_not_advertised=false
write_tool_denial_observed=true
outage_recoverable=true
exact_version_bound=true
reference_sealed_receipt_match=true
manifest_bound=true
application_trace_present=true
client_request_id_present=true
server_request_id_present=false
fixture_preconditions_verified=true
```

The two false/diagnostic fields are intentionally not functional pass
requirements: Cockroach advertises provider-wide write tools even though the
read-only OAuth identity cannot execute them, and its MCP response supplied no
server request-ID header. Effective write denial remains mandatory and passed.

The hero call returned one sealed evidence row. The private structured result
matched Gate 1 for the synthetic case, invoice, finding, clause, tariff capture
and exact object version/hash, invoice exact object version/hash, approver,
approval time, evidence hash, and complete evidence-ID set.

## Negative tests

| Executed test | Result |
| --- | --- |
| Correct tenant + sealed hero case + later contest | `found`, one row |
| Wrong tenant + hero case/contest | `not_found`, zero rows, no memory |
| Unknown case/contest | `not_found`, zero rows, no memory |
| Contest bound to an unsealed case | `not_found`, zero rows, no memory |
| Empty known-write-tool invocation | explicit permission/access/write denial |
| Injected MCP outage | `unavailable`, no memory, no trace |

The unsealed negative is not an absent-join shortcut. Preparation first proved
the normal contest workflow rejects an unsealed case, then transactionally
created and audit-logged a synthetic adversarial contest row. The live retrieval
encountered that bound case/contest and excluded it through the seal predicates.

## Audit evidence and limitation

Private application evidence records the request time, `select_query` tool,
private OAuth identity label, cluster/database scope, fixed query template,
client request and correlation IDs, returned identifiers, and later-contest
binding.

CockroachDB Cloud organization audit independently recorded
`AUDIT_LOG_ACTION_MCP_OAUTH_CONSENT` from `AUDIT_LOG_SOURCE_MCP`, including an
actual user and trace ID. That proves the real read-only MCP consent, but the
event contains no cluster/database binding, tool execution, query template,
hero correlation, or returned identifiers.

Further executed checks found:

- no data-plane MCP audit event for `select_query`;
- no server request or trace header on the successful MCP response;
- Basic cluster log export is unsupported.

Cockroach statement statistics did expose one fingerprint in the exact private
database matching the Gate 3 query's unique cases/contests/evidence join shape.
The first read was stale and predated the current live call, so it was rejected
as correlation evidence. After the server's aggregation delay, the same
fingerprint's aggregate count increased from `4` to `9`, with zero mean rows
written and zero failures, and `lastExecAt` fell inside the application trace
window (approximately 5.2 seconds after its earliest request). The aggregate
includes earlier same-shape executions and is not presented as a per-run count.

Therefore the real functional call is proven by the authenticated MCP response
and application trace, while independent server-side correlation of that call
remains unavailable. This is the sole Gate 3 pass-criteria limitation.

## Executed automated evidence

```text
Full Python suite: 499 passed, 1 unrelated upstream deprecation warning
Gate 3 focused suite: 97 passed
Ruff on the complete Gate 3 scope: all checks passed
git diff --check: passed
```

## Pass criteria

| Criterion | Executed result |
| --- | --- |
| Real Managed MCP runtime call | PASS |
| Hero sealed receipt retrieved | PASS — one row |
| Same Gate 1 manifest and exact source versions | PASS |
| Identity cannot mutate case/evidence state | PASS — explicit live write denial |
| Cross-tenant access fails | PASS — fixed query returned zero rows |
| Unknown and unsealed cases abstain | PASS |
| Outage is recoverable and invents no memory | PASS |
| Application trace links later contest and request | PASS |
| Independent server audit correlates the data-plane call | LIMITATION — unavailable |

## Independent verification

Independent verdict: live functional matrix `PASS`; overall Gate 3
`PASS WITH LIMITATIONS`.

The verifier independently recomputed the Gate 1 canonical manifest, compared
every hero receipt/version/evidence binding, checked all private request inputs
against their outcomes and correlations, confirmed every call occurred within
the read-only token window, validated all three negative paths and outage
behavior, and confirmed the write-denial response. It separately confirmed the
absence of a correlating server audit record.

## Public-safety verification

Private evidence is stored only under ignored `runtime-artifacts/` paths. Files
are regular, mode `0600`, inside mode-`0700` directories with no symlinks. The
OAuth bearer value was destroyed after use and is absent from every private and
public artifact; only noncredential verification metadata remains.

Current-tree and reachable-history scans found no bearer tokens, API keys,
private keys, real connection strings, runtime artifacts, production hashes,
object versions, or live Gate 3 identifiers. The only exact private/public
collision is a deliberately committed synthetic invoice number reused from the
public Gate 1 fixture. A static demo recipient now uses the reserved `.example`
domain.

## Exact limitations

- Customer-visible server-side correlation of the successful MCP data-plane
  call is unavailable on the current Basic setup.
- Write tools are visible in provider-wide discovery but denied to the
  `mcp:read` identity and are not exposed by application code.
- Tenant separation is fixed-query/application enforcement, not RLS or a
  tenant-scoped database credential.
- This is a bounded script-driven application/demo path, not automatic deployed
  contest-event wiring.
- Detailed identifiers, query rows, exact versions/hashes, actor information,
  and infrastructure metadata remain private.

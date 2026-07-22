# Gate 5B Report — renewable read-only Managed MCP OAuth

Terminal verdict: **PASS WITH LIMITATIONS**

This report covers only the replacement of the rejected service-account API
key with an unattended OAuth lifecycle. Detailed provider responses and all
credentials remain in ignored, mode-restricted private evidence.

## Provider capability classification

| Claim | Classification | Result |
|---|---|---|
| OAuth is CockroachDB's supported Managed MCP authorization path | DOCUMENTED | Supported |
| Authorization Code, PKCE S256, public-client registration, and refresh grant | METADATA-DISCOVERED | Advertised |
| `offline_access` | METADATA-DISCOVERED | Not advertised and not requested |
| Refresh token issuance | OBSERVED-LIVE | Issued for `mcp:read` |
| Approximate initial access-token TTL | OBSERVED-LIVE | 3,600 seconds |
| Refresh-token rotation | OBSERVED-LIVE | Observed and consumed correctly |
| Provider-published guarantee through the judging period | NOT-DETERMINED | No such durability guarantee was found |

The authorization request included the discovered MCP resource indicator and
requested `mcp:read` only. It did not request or retain `mcp:write`. The
deployed judge runtime does not accept or recreate the rejected API-key path;
the older private Gate 3 operator remains outside the deployment.

## Executed lifecycle proof

- The initial token executed the fixed sealed-memory `select_query`.
- Two immediate refresh grants succeeded, including consumption of a rotated
  refresh token.
- Simulated local expiry triggered refresh before the MCP request.
- Each checked access token retrieved the fixed fictional hero and revalidated
  its sealed receipt.
- Each checked access token received the accepted explicit authorization
  denial from the non-mutating write probe.
- A real-clock soak waited 3,006 seconds until the provider token entered the
  five-minute safety window. On-demand refresh triggered, rotation was
  persisted, the fixed hero succeeded, the receipt verified, and the write
  probe remained denied.
- After deployment, a private operator probe changed only the stored expiry
  timestamp. App Runner acquired the shared lease, refreshed and rotated the
  bundle in SSM, then returned the executed hero with exact S3 verification.

## Runtime architecture

The browser receives no token and no generic MCP or SQL capability. The
deployed FastAPI process uses one shared token manager backed by one SSM SecureString.
It refreshes on demand below five minutes, or once after an MCP 401, and
replays the fixed retrieval at most once. A 403 never triggers refresh. A
conditional DynamoDB item with an owner and TTL serializes rotation across
overlapping App Runner processes. Rotation persistence failure, lease failure,
invalid grant, scope expansion, or a second 401 returns the existing safe
`mcp_memory_unavailable` projection with `mock_fallback: false`.

The runtime role can read six exact configuration parameters, read and replace
only the OAuth bundle parameter, operate only the one refresh-lease item
table, and read only the exact S3 objects derived from the sealed receipt.
No bearer token is mapped into App Runner environment variables.

## AWS readback

- The service-scoped $10 budget and its 80%/100% notifications were read back
  before publication. Unrelated account spend is excluded from the Gate 5
  budget calculation.
- App Runner is `RUNNING`, uses one worker and a one-instance autoscaling
  configuration, and exposes only the fixed public surface.
- Six configuration SecureStrings are mapped to App Runner. The OAuth bundle
  is not mapped to an environment variable; the runtime reads and rotates it
  through exact SSM permissions.
- The one DynamoDB lease table is on-demand, TTL-enabled, tagged for September
  22 teardown, and its runtime IAM statement is restricted by the one bundle
  leading key.
- Two exact S3 object ARNs were derived from the sealed receipt and granted
  `GetObjectVersion`; no bucket inventory permission was added.
- The logged-out live hero returned `executed`, `mock_fallback: false`,
  `verified_read`, and exact-versioned-receipt verification both before and
  after deployed refresh.

## Verification

The final verification matrix includes 663 passing Python tests, 23 passing UI
tests, 10/10 evaluation fixtures, changed-file lint/compile, linux/amd64
container build, three stable live logged-out hero responses, a zero-finding
exact-token scan, and the reachable-history scan. The implementation SHA and
overall publication blocker are recorded in the Gate 5 report.

## Limitations

- CockroachDB documentation recommends OAuth but does not publish a
  refresh-token durability commitment for this Managed MCP flow. Refresh is
  therefore metadata-discovered and observed live, not claimed as a published
  provider guarantee.
- The observed provider write denial is an undocumented in-band MCP error
  shape, not an HTTP 403 `insufficient_scope` response. Tally accepts only the
  exact normalized fingerprint observed in the approved non-mutating probe;
  any different validation, missing-table, or execution error is inconclusive.
- This is platform-enforced `mcp:read`, not database-enforced row-level tenant
  isolation and not request-exact server-side MCP auditing.
- Superseded rotating tokens can remain in SSM parameter version history, but
  the runtime role cannot call parameter-history APIs and uses only the current
  version. Rotated predecessors are not retained in application state.
- No periodic refresh loop or daily canary is deployed. The on-demand path
  avoids unnecessary rotations; a provider outage remains a truthful,
  recoverable unavailable response.

# Closing the live Managed MCP blocker — two steps

The Gate 2 reconstruction already talks to CockroachDB Managed MCP through the
real client and is proven fail-closed. The only thing missing is a **fresh
read-only OAuth token** — the one stored in SSM has expired (its refresh token is
also expired, so it can't self-renew). This is a token-freshness issue, not an
architecture gap: a probe reached CockroachDB and was rejected only for a stale
token, with the cluster + isolated database (`tally_gate2_iso`) + query all
correct.

## What you run (interactive, needs a browser)

From the repo, with the deployer profile:

Run these as modules (`-m`, dots, no `.py`) from the repo root so the internal
`scripts.*` imports resolve:

```bash
# 1. Re-authorize — opens a browser; approve the read-only (mcp:read) grant.
#    Writes a fresh renewable token bundle to SSM.
AWS_PROFILE=gate5-deployer ./.venv/bin/python -m scripts.gate5b_oauth_bootstrap

# 2. Run the LIVE Managed MCP reconstruction trace against tally_gate2_iso.
AWS_PROFILE=gate5-deployer ./.venv/bin/python -m scripts.gate2_live_mcp_trace
```

## What success looks like

Step 2 prints, and exits 0:

```json
{
  "read_path": "LIVE CockroachDB Managed MCP (real sponsor trace)",
  "mcp_write_tool_denied": true,
  "mcp_rows_returned": 5,
  "reconstruction": { "state": "COMPLETE", "days_complete": 7, "days_total": 7 },
  "mock_fallback": false
}
```

That is the real Managed MCP sponsor trace: it verifies the identity is read-only
(write tool denied), reads the 5 pre-invoice events live through Managed MCP, and
drives the reconstruction to 7/7 COMPLETE — all against the isolated database,
never touching the protected `defaultdb`.

## If step 2 prints `MCP_TOKEN_EXPIRED`

The token still isn't fresh — re-run step 1 (the browser approval) and then step 2
again. The script never prints token values.

## After it passes

Tell me and I'll update the Gate 2 report/manifest to mark the live Managed MCP
read **PASSED (live)** instead of driver-diagnostic, and drop it from the deferral
list — closing Gate 2's last open item.

# Run the Tally workbench against the live backend (local)

Three terminals. The UI (Vite :5173) proxies `/api` to the FastAPI backend
(:8000), which reads the isolated `tally_gate2_iso` database. No CORS, no cloud
deploy — this is the local judge experience.

## 0. One-time: seed a completed hero under the app's tenant

The UI shows real data, so a completed hero must exist in the database under the
tenant the backend reads. This runs the full pipeline (reconstruction → rule →
judgment) for INV-1048 under tenant `TALLY_TENANT_ID`:

```bash
cd /private/tmp/tally-gates
AWS_PROFILE=tally ./.venv/bin/python -m scripts.gate7_seed_for_ui
```

It prints the tenant id + invoice id to use below. (Re-runnable; it resets the
UI tenant each time.)

## 1. Terminal A — backend (FastAPI :8000)

```bash
cd /private/tmp/tally-gates
export TALLY_CRDB_DSN="$(grep TALLY_CRDB_DSN '/Users/markbrazinski/Desktop/coding fun/tally-agent/.env' | cut -d= -f2-)"
export TALLY_CRDB_DSN="${TALLY_CRDB_DSN/\/defaultdb/\/tally_gate2_iso}"   # point at the isolated DB
export TALLY_TENANT_ID="10000000-0000-4000-8000-00000000a007"            # the UI hero tenant
export TALLY_DEMO_TOKEN="local-dev-token"                                # any non-empty value
./.venv/bin/python -m uvicorn src.platform.app:app --port 8000
```

Health check: `curl -s localhost:8000/api/invoices` should return JSON with the
hero invoice.

## 2. Terminal B — UI (Vite :5173)

```bash
cd /private/tmp/tally-gates/ui-next
npm run dev
```

Open **http://localhost:5173**.

## What you should see

- The invoice **queue** with the hero **INV-1048** row (plus two neutral rows).
- Click **INV-1048** → the **workbench** opens; because the pipeline already ran
  server-side, it catches up to **READY FOR REVIEW** with the 7-day ledger,
  sourced timeline, the **$250 tariff clause**, and the **DISPUTE $700**
  recommendation — all from the real backend.
- Click **Approve $700 dispute** → it calls the real approve+seal endpoint; on
  success the state advances to sealed → send-gate → SENT (to the controlled
  demonstration inbox). A failure fails **closed** (no fake seal).

## Offline / design mode (no backend)

```bash
cd /private/tmp/tally-gates/ui-next && npm run dev
# then open http://localhost:5173/?provider=mock
```

`?provider=mock` runs the original timer-driven hero with representative data —
useful for design work and screenshots without a backend.

## Notes

- The live provider **never falls back to mock data** — if the backend is down,
  the queue is empty and errors are logged, per the fail-closed rule.
- The controlled external send is intentionally paused; the send-gate is backed
  by the real seal, and no external mailbox is contacted.

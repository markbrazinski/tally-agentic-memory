# Contract Fixtures

One JSON file per §3 route, copied verbatim from `docs/tech-design-doc.md`'s
examples. These are the frozen API contract Bundle 1-FE builds its mock
layer against, per `docs/bundle-0.md`'s Concurrency Contract — no live
endpoint exists yet; these files ARE the interface until Bundle 0's later
sessions ship the real routes.

Gate 4 recovery truth-labeling replaced placeholder ellipses in the replay
and evaluation examples. The replay example contains fictional identifiers,
hashes, and timestamps and no claimed query execution; the evaluation example
reports unavailable rather than inventing totals. The executed synthetic
evaluation result lives under `artifacts/recovery/gate-4/`.

Every JSON file in this directory is a synthetic demonstration contract
example. Names, identifiers, dates, amounts, and event shapes are fictional
fixtures, not live rows or executed evidence. Where the recovery build has no
computed result, the fixture reports unavailable or uses an explicit
placeholder instead of inventing an outcome.

**Freeze lock (bundle-0.md #4):** changing any shipped fixture's shape is
a strategy escalation + TDD §3 edit + a note to the FE session — never a
silent edit. A shape gap discovered by FE routes to strategy, never
patched locally on either side.

| File | Route |
|---|---|
| `POST_invoices.json` | `POST /invoices` |
| `GET_invoices.json` | `GET /invoices` |
| `GET_invoices_id_fields.json` | `GET /invoices/{id}/fields` |
| `POST_cases_id_approve.json` | `POST /cases/{id}/approve` |
| `GET_cases_id_rebuttal.json` | `GET /cases/{id}/rebuttal` |
| `GET_cases_id_replay.json` | `GET /cases/{case_id}/replay` |
| `GET_recordings_coverage.json` | `GET /recordings/coverage` |
| `GET_recordings_tariff_at_date.json` | `GET /recordings/tariff-at-date` (found) |
| `GET_recordings_tariff_at_date_empty.json` | `GET /recordings/tariff-at-date` (pre-coverage, Law 3 empty state) |
| `GET_ledger_summary.json` | `GET /ledger/summary` |
| `GET_carriers_id_conduct.json` | `GET /carriers/{id}` conduct block |
| `GET_clerk_runs_id.json` | `GET /clerk/runs/{id}` |
| `GET_evals_latest.json` | `GET /evals/latest` |
| `GET_healthz.json` | `GET /healthz` |
| `GET_meta_identity.json` | `GET /meta/identity` |
| `WS_events.json` | `wss://…/ws` — all 8 frame shapes (§3.11) |

**Routes with no example JSON in the TDD** (shape implied by the schema/
prose only, not fixture'd here — FE should treat these as "shape TBD,
ask strategy before hardcoding"): `GET /invoices/{id}`, `GET /cases`,
`GET /cases/{id}`, `GET /cases/{id}/evidence`, `POST /contests`,
`POST /contests/{id}/resolve`, `GET /recordings/log`,
`GET /recordings/terminal-at-date`, `GET /recordings/containers-at-date`,
`GET /recordings/preload`, `GET /ledger/drill`, `GET /carriers`,
`POST /clerk/runs`, `POST /cases/{id}/rebuttal/send`. Not a gap in this
session's work — the TDD itself doesn't give these routes worked JSON
examples, only field lists in prose. Escalate before FE hardcodes a
guessed shape for any of these.

**`cases.state` vocabulary** (bundle-2-S0): `ANALYZED`, `FILED`,
`CONTESTED`, `RESOLVED`, `NOT_PRESSED`, `ACCEPTED`. The last two joined
this session (migration `003_not_pressed.sql`) alongside a new nullable
`cases.decision_reason` column, both now returned by the still-unfrozen
`GET /cases/{id}` (see above — this route has no worked TDD example, so
adding a field to it is not a freeze-lock violation). Transitions INTO
`NOT_PRESSED`/`ACCEPTED` are Bundle 3 scope; this session only adds the
column and the vocabulary entry, verified via `src/platform/seal.py`'s
existing `state not in ALREADY_SEALABLE_STATES` catch-all, which already
treats any unrecognized non-blocked state as an idempotent no-op — no
code change needed there.

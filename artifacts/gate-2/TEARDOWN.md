# Gate 2 isolated teardown

Applies only to the isolated Gate-2 resources. Contains no live infrastructure
identifiers, credentials, object bindings, or connection details.

## Safety boundary

- The only Gate-2 mutation target is the isolated database **`tally_gate2_iso`**
  on the existing cluster.
- The protected hero database **`defaultdb`** and every other `tally_*` database
  MUST NOT be dropped or altered. `defaultdb` was verified unchanged (21 tables
  before and after).
- Obtain explicit destructive-operation approval immediately before any drop.
- Capture a before/after readback for every target.
- Do not treat an absent list result as proof when the read itself failed.

## Ordered teardown

1. Confirm the target is exactly `tally_gate2_iso` and no other database.
2. Preserve the public-safe execution manifest and live trace artifacts under
   `artifacts/gate-2/` before dropping any rows.
3. `DROP DATABASE tally_gate2_iso;` (removes the isolated Gate-2 lineage only).
4. Read back `SHOW DATABASES;` and confirm `tally_gate2_iso` is absent while
   `defaultdb` and all other `tally_*` databases remain present.

## Required readback

- `tally_gate2_iso` absent after teardown;
- `defaultdb` present and unchanged (21 public tables);
- all other existing `tally_*` databases present and unchanged.

## Current state

The isolated database `tally_gate2_iso` is **RETAINED** (migrations 001–011
applied; representative seed present; positive and negative traces executed).
Deletion is **NOT EXECUTED**. Every destructive step is separately approval
gated.

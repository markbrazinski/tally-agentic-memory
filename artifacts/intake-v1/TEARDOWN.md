# Intake v1 isolated teardown

Scope: resources tagged `Environment=intake-v1-isolated` only.

After the demo evidence has been retained, remove:

1. App Runner service `tally-intake-v1`.
2. ECR repository `tally-intake-v1`.
3. SSM parameters under `/tally/intake-v1/`.
4. IAM roles `tally-intake-v1-runtime` and `tally-intake-v1-ecr-access`.
5. CockroachDB database `tally_intake_gate6_20260723`.
6. S3 objects under `s3://tally-record/isolated-intake-v1/`, including versions.

Every deletion requires explicit destructive-operation approval and exact
readback. This iteration does not delete these resources automatically.

The existing Gate 5 service, database, lineage, S3 objects, roles, parameters,
and judged routing are excluded from teardown.

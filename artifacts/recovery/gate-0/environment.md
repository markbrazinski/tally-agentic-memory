# Gate 0 environment (sanitized)

The complete environment inventory is retained only in private commit
`f19968fc6653fe15761040bdba6b52ef61b4d0ad` and in its encrypted off-repository
backup. This public record intentionally excludes account, cluster, bucket,
object, connection, and source identifiers.

| Item | Public-safe value |
|---|---|
| Source database | `<PRIVATE_SOURCE_DATABASE>` |
| Isolated replay target | `<DISPOSABLE_RECOVERY_DATABASE>` |
| CockroachDB plan | Basic |
| AWS account and region | `<PRIVATE_AWS_ACCOUNT>` / `<PRIVATE_REGION>` |
| Capture store | `<PRIVATE_VERSIONED_BUCKET>` |
| Scheduled capture | Enabled at verification time |
| Scheduled verifier | Enabled at verification time |
| Detailed settings and billing evidence | Private snapshot only |

No credential, DSN, private URL, cloud resource identifier, or raw capture is
present in this public artifact.

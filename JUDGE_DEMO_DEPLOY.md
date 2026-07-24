# Judge Demo — Deploy, Rollback, Teardown, Cost

The judge demo is the auth-gated Tally workbench running on the **isolated
`tally-intake-v1` App Runner lane**. It reuses that lane's existing isolation
(separate service, ECR repo, runtime/access roles, its own CockroachDB database
`tally_intake_deployed_20260723`, its own `/tally/intake-v1/*` SSM params, and
the `isolated-intake-v1/` S3 key prefix inside `tally-record`) and layers
Amazon Cognito auth on top. **No hero resource is touched.**

## Live facts

| Thing | Value |
|---|---|
| Live URL | `https://r3n3ixixr3.us-east-1.awsapprunner.com` |
| App Runner service | `tally-intake-v1` (`arn:…:service/tally-intake-v1/f7002f01444343d6878cb92591aa7e53`) |
| ECR repo | `352720962539.dkr.ecr.us-east-1.amazonaws.com/tally-intake-v1` |
| CockroachDB | database `tally_intake_deployed_20260723` (isolated; hero `defaultdb` untouched) |
| S3 | `tally-record` bucket, `isolated-intake-v1/` prefix (versioned) |
| Cognito pool | `us-east-1_avnXdxC10` |
| Cognito app client | `45flk5tc649ujf2tdf6epvfof9` (USER_PASSWORD_AUTH, no secret, 8h token) |
| Judge login | `judge@tally-demo.example` (password in SSM `/tally/intake-v1/judge-password`) |
| Deploy profile | `gate5-deployer` (scoped to the `tally-intake-v1-*` namespace) |

## Auth model

- One Cognito User Pool, self-registration disabled (`AllowAdminCreateUserOnly`).
- One manually provisioned judge account, permanent password, no invite email,
  no social login, no password recovery, no roles/admin UI.
- The app runs with `TALLY_JUDGE_AUTH_ENABLED=true`. A global middleware
  (`src/platform/judge_auth.py`) requires a valid Cognito JWT (RS256, verified
  against the pool's JWKS) on **every** request except a small public allowlist:
  `/login`, `/api/login`, `/api/logout`, `/healthz`, `/readyz`, static assets.
- Unauthenticated API/SSE/PDF/import requests get `401`; unauthenticated page
  navigations `302 → /login`.
- The JWT is carried as an httpOnly, Secure, SameSite=Strict `tally_session`
  cookie so SSE / PDF / page GETs (which can't set an Authorization header) are
  authenticated too. Session expiry = the token's own expiry (8h).

## Deploy

```bash
# From the repo root. Builds Dockerfile.judge (ui-next/ + FastAPI), pushes to
# ECR, updates the tally-intake-v1 service with the Cognito env, and reads back
# live state (RUNNING, image match, auth env true, /healthz 200, unauth API 401,
# /login 200). Requires the gate5-deployer profile.
./deploy_judge.sh
```

The script ends in read-back assertions (a deploy is "done" only when live state
is re-read and matches intent). It will **fail** rather than report success if
auth isn't enforcing or `/login` is shadowed.

### App Runner note (important)

`update-service` changes the service *config* but does **not** always roll the
running task. A real deployment requires `apprunner:StartDeployment`. The
deployer role's `intake-v1-deploy-trigger` inline policy grants it, scoped to
this service only. `deploy_judge.sh` triggers a deployment and waits for it to
reach `RUNNING`. Pinning the image by **digest** (not tag) additionally defeats
App Runner's tag-level image cache.

## Rollback

App Runner keeps the previous image. To roll back, redeploy the prior image
digest:

```bash
# List recent images (newest first) and pick the prior digest:
aws ecr describe-images --repository-name tally-intake-v1 --profile gate5-deployer \
  --region us-east-1 --query 'reverse(sort_by(imageDetails,&imagePushedAt))[].{d:imageDigest,pushed:imagePushedAt,tags:imageTags}' --output table

# Re-point the service at that digest and trigger a deployment:
TALLY_IMAGE_DIGEST=<sha256:…prior…> ./deploy_judge.sh   # (digest override supported)
aws apprunner start-deployment --profile gate5-deployer --region us-east-1 \
  --service-arn arn:aws:apprunner:us-east-1:352720962539:service/tally-intake-v1/f7002f01444343d6878cb92591aa7e53
```

To fully disable auth-gated judge mode without a rollback, set
`TALLY_JUDGE_AUTH_ENABLED=false` in the service env and redeploy (reverts to the
static-bearer path).

## Teardown

The lane's compute cost is dominated by App Runner. To stop billing without
destroying anything:

```bash
# Pause (keeps config + data; ~$0 compute while paused):
aws apprunner pause-service --profile gate5-deployer --region us-east-1 \
  --service-arn arn:aws:apprunner:us-east-1:352720962539:service/tally-intake-v1/f7002f01444343d6878cb92591aa7e53
```

Full teardown (irreversible — only after judging):

```bash
# 1. Delete the App Runner service
aws apprunner delete-service --profile gate5-deployer --region us-east-1 --service-arn <arn>
# 2. Delete the Cognito pool (removes the judge account + client)
aws cognito-idp delete-user-pool --profile gate5-deployer --region us-east-1 --user-pool-id us-east-1_avnXdxC10
# 3. (optional) delete the isolated SSM params
for p in crdb-dsn demo-token tenant-id cognito-pool-id cognito-client-id judge-username judge-password; do
  aws ssm delete-parameter --name /tally/intake-v1/$p --profile gate5-deployer --region us-east-1 || true
done
# 4. (optional) drop the isolated DB — leaves hero DBs untouched:
#    DROP DATABASE tally_intake_deployed_20260723;
```

Do **not** delete the `tally-record` bucket (shared with hero evidence) — only
the judge objects live under `isolated-intake-v1/`.

## Estimated ongoing cost

Rough, us-east-1, at demo/judging scale (near-idle, occasional imports):

| Resource | Basis | Est. / month |
|---|---|---|
| App Runner (1 vCPU / 2 GB, always-on) | ~$0.064/hr compute + $0.007/GB-hr mem when active; provisioned-but-idle billed at a reduced rate | **~$5–25** depending on how long it stays RUNNING vs PAUSED |
| Cognito | First 10k MAU free tier | **$0** |
| S3 (judge objects) | a handful of small PDFs, versioned | **< $0.10** |
| SSM Standard params | free tier | **$0** |
| CockroachDB | shared existing cluster; isolated DB adds negligible storage | **$0 incremental** |
| Bedrock (extraction) | ~1 Sonnet call per import | **cents per import** |

**Pause the service between judging sessions** to keep this near $0.

## Known limitations

- Single judge account (no per-judge isolation); all judges share the sandbox
  DB, which is acceptable post-recording (their imports can't touch hero data).
- No password recovery / no self-registration by design.
- Import currently accepts the locked `INV-1048` scenario shape; a true inbound
  email adapter can later POST to the same `/api/demo/invoices` intake contract
  with `import_source=forwarded_email_simulation` without changing downstream
  processing (the contract is already in place).
- `update-service` alone won't roll the task — always trigger a deployment
  (the deploy script does this).

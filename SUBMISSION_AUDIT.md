# Tally — Submission Audit

**Verdict: PASS WITH LIMITATIONS**
**Safe to make public: YES** (after the two operator actions in "Requires Mark")

Audited on branch `authority-transition-v1` in `markbrazinski/tally-agentic-memory`.
Scope was submission-readiness only: no features, no refactors, no UI changes, no
demo-data changes, no deploys, no pushes.

---

## Secret scan — CLEAN

Scanned the working tree and **all 108 commits** of history.

| Check | Result |
|---|---|
| Live demo bearer token (value read from SSM) | absent from tree and history |
| Live CockroachDB password / full DSN | absent from tree and history |
| `AKIA`/`ASIA` access-key literals | none (2 history hits are **detector regexes** in the repo's own privacy-scan scripts, not keys) |
| `BEGIN … PRIVATE KEY` blocks | none |
| Inline-credentialed `postgres://` URLs | none (1 hit is a detection pattern in `scripts/gate7_privacy_scan.py`) |
| Hardcoded `secret=`/`password=`/`api_key=` literals | none outside tests and regexes |

No history rewrite is required. No credential rotation is required.

**Infrastructure identifiers removed from the tracked tree** (second pass, after
the operator asked that the hosted link not be published). These were not
secrets — no password or key value was ever committed — but they are needless
detail for a public repo:

| Identifier | Was in | Now |
|---|---|---|
| Hosted judge URL | `README.md`, `JUDGE_DEMO_DEPLOY.md`, `scripts/demo_fresh_hero.py`, `SUBMISSION_AUDIT.md` | absent; supplied with the Devpost submission |
| AWS account id `3527…` | `JUDGE_DEMO_DEPLOY.md`, `deploy_judge.sh`, `scripts/cloudshell_deploy_oauth_canary.sh` | derived at runtime via `sts get-caller-identity` |
| App Runner service ARN, ECR URI, Cognito pool/client ids | `JUDGE_DEMO_DEPLOY.md` | file untracked (kept on disk, gitignored) |

`JUDGE_DEMO_DEPLOY.md` was also **stale** — it documented Cognito pool
`us-east-1_avnXdxC10` / client `45flk5tc649…`, which are not the live ones.
It contained no password value, only an SSM pointer.

The URL remains in three historical commits. It is a public App Runner hostname
behind Cognito, not a credential, so no history rewrite was performed.

**Representative data is fictional.** Carriers (Asterline, Seabright, Harborline),
containers, B/Ls, tariffs and amounts are synthetic; fixtures carry an explicit
`SYNTHETIC DEMO — FICTIONAL DATA` / `FICTIONAL DEMONSTRATION DOCUMENT` marking.

---

## License — PASS

Root `LICENSE` is MIT (`Copyright (c) 2026 Mark Brazinski`), standard text, at the
repository root where GitHub detects it. README links it.

No `THIRD_PARTY_NOTICES.md` added: dependencies are ordinary MIT/BSD/Apache
PyPI and npm packages consumed unmodified via `requirements*.txt` and
`package.json`, which is the customary attribution surface. No vendored or
modified third-party source was found.

---

## Files changed (3)

| File | Change |
|---|---|
| `README.md` | Replaced the stale lead. Was advertising a hosted URL for a **different, un-hardened live service** and describing a Northstar/Asterline FILED/CONTESTED case that is not what was filmed. The README now carries **no hosted URL and no credentials** — both are delivered with the Devpost submission, since the deployment is access-controlled and this repository is public. Leads instead with the three-outcome demo and a demo-limitations section. Fixed local-verification commands: they documented `npm --prefix ui`, but the Dockerfile ships `ui-next` and states `ui/` "is not shipped" — a judge following the old README would have tested dead code. |
| `.gitignore` | Added `node_modules/`, `dist/`, `build/`, `*.log`, `logs/`, coverage outputs. Nothing already tracked became ignored. |
| `docs/TALLY_BE_HANDOFF_AFTER_INTAKE_2026-07-23.md` | Untracked (`git rm --cached`; file kept on disk). `.gitignore` already declares `/docs/` internal, but this file predated the rule. Reviewed first: it self-declares public-safe and contains no credentials — removed for consistency, not because it leaked. |

No runtime code, test, migration, fixture, or UI file was modified.

---

## Tests — PASS

```
.venv/bin/python -m pytest        →  898 passed  (working tree)
.venv/bin/python -m ruff check src/  →  All checks passed!
```

Nothing was skipped or xfailed into a pass. `ruff check .` (whole repo) reports
**10 pre-existing lint errors** in helper scripts and tests — untouched, since
fixing them is outside submission-critical scope. The README documents
`ruff check src/`, which passes, rather than a command that fails.

---

## Clean-clone verification — PASS

Fresh `git clone` of `authority-transition-v1` into an empty directory, then only
the documented commands:

| Step | Result |
|---|---|
| `python3.12 -m venv .venv` + `pip install -r requirements.txt -r requirements-dev.txt` | OK |
| `pytest` | **898 passed** — offline, no AWS credentials |
| `npm --prefix ui-next ci` | OK |
| `npm --prefix ui-next run build` | OK → `index-BdETJvZd.js` |

The clean-clone bundle hash is **identical to the deployed bundle**, so the live
artifact is reproducible from public source.

Bundle credential scan (clean clone): literal token **0**, `VITE_DEMO_TOKEN`
**0**, `Bearer` construct **0**.

Migrations and seeding were **not** run: they need a live CockroachDB DSN, which
is a private resource. This is the one documented dependency a judge cannot
satisfy locally — the hosted app is the intended evaluation path, and the test
suite covers the logic offline.

---

## Hosted smoke test — PASS

Against the deployed judge lane (URL held out of this public repo):

| Check | Expected | Actual |
|---|---|---|
| `/login` | 200 | **200** |
| `/api/invoices` unauthenticated | 401 | **401** |
| `/` browser navigation | 302 → `/login` | **302** |
| Invalid bearer token | 401 | **401** |
| SSE `/api/invoices/…/events` unauthenticated | 401 | **401** |
| Login as `judge` → authenticated read | 200 | **200** |

**Bedrock correspondence** (verified earlier this session on a fresh draft):
`generator_kind = BEDROCK`, `generator_model_id = us.anthropic.claude-sonnet-4-6`,
`prose_validation_state = VALIDATED`. Tampering the amount `$700 → $980` in that
same prose yields `UNSUPPORTED_NUMBER:$980.00`, which marks the draft INVALID and
fails the `LOCKED_FIELDS` send gate.

**Three terminal states persisted** (read from the live database):

```
INV-1041.pdf  APPROVED_FOR_PAYMENT
INV-1047.pdf  NEEDS_EVIDENCE
INV-1048.pdf  DISPUTED
HERO REC: v1 DISPUTE FROZEN  $2,450 / $1,750 / $700
```

**Managed MCP and Distributed Vector Indexing are real runtime integrations:**
reconstruction issues `select_query` against `mcp_reconstruction_memory_v1` with
OAuth read-only enforcement (write tools provably denied) and a
`recorded_at <= knowledge_cutoff` constraint re-checked deterministically; the
rule worker embeds with Titan V2 (1024-d) and queries
`tariff_clause_embedding_search_idx` (`vector_l2_ops`), with applicability decided
by deterministic code, never by similarity.

---

## Limitations (why not a clean PASS)

1. **The filmed video shows no login screen; the hosted app now requires one.**
   Cognito was added after filming. The flows are otherwise identical, but a
   judge comparing them will meet a sign-in page the video does not show.
2. **The filmed hero is seeded.** INV-1048's decision chain is seeded to a
   known-good state (`demo_restore_hero`). The pipeline is real and the deployed
   workers run for genuinely imported invoices, but *this* invoice's chain was
   not produced by a live end-to-end run. Disclosed in the README.
3. **Outbound mail is a demonstration provider.** Real receipt id, no email
   leaves. Disclosed in the README.
4. **A second live App Runner service (`x69yr3tibq…`) is still running** and
   still returns 200. It is not the hardened lane. Two historical gate reports
   under `artifacts/recovery/gate-5/` still reference it; those are accurate
   records of what was true then and were left alone.
5. **Migrations/seed cannot run from a clean clone** without a private DSN.

---

## Unresolved blockers

None that prevent publication.

---

## Requires Mark

1. **Rotate the judge password.** It is currently the literal placeholder
   `ReplaceMe-Str0ng!Pass` from the provisioning script. Functional, and not weak,
   but it reads as an oversight to a judge who looks.
   ```
   aws cognito-idp admin-set-user-password --user-pool-id us-east-1_KOV7rz8Yu \
     --username judge --password 'YourRealPassword' --permanent --region us-east-1
   ```
   No redeploy needed — the app validates JWTs, not passwords.
2. **Add the demo video link** to the README's Judge access table (placeholder
   in place).
3. **Decide `main`.** `main` is 0 commits ahead and 68 behind
   `authority-transition-v1` — a clean fast-forward, no conflicts. Judges landing
   on the repo root currently see `main`, which has the stale README. Merging is
   an operator action and was not performed.
4. **Decide the fate of the second live service** (`x69yr3tibq…`).

---

## Not done, deliberately

No push, no merge, no deploy, no visibility change, no history rewrite, no
credential rotation — all outside the authorization for this pass. The three
changed files are committed locally only.

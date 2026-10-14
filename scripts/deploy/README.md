# KromaRAG -- Production deploy runbook (Cloud Run + Vercel + Neon, europe-west1)

Target: web `https://kromarag.vercel.app`, API Cloud Run `europe-west1`, Neon pooled prod + `staging` branch.
Scripts here are the executable form of this runbook. `.sh` = Cloud Shell/bash (4.4+;
the scripts use arrays), `.ps1` = local Windows PowerShell 5.1+. Both do the same.
All scripts are ASCII-only on purpose: PowerShell 5.1 misparses non-ASCII bytes.

## Deploy order (do not reorder)

```
1. 00-secrets   -> Secret Manager (prod + staging)
2. 10-cloudrun  -> backend PROD, copy API_URL
   11-cloudrun  -> backend STAGING, copy STAGING_API_URL
3. 20-vercel-env-> Vercel envs (needs both API URLs) -> vercel --prod
4. smoke        -> verify prod, then staging
```

`NEXT_PUBLIC_*` is baked at Vercel **build** time -- set envs before `vercel --prod`.
`JWT_ISSUER` on Cloud Run is the Clerk issuer (`https://<app>.clerk.accounts.dev`,
per env: `kromarag-prod` / `kromarag-dev`); `CORS_ORIGINS` stays the web URL.
First deploy already uses `https://kromarag.vercel.app` for CORS, so no second
backend pass is needed unless you switch to a custom domain.

Tagging (specs/08 §2.2): `--source` builds produce no registry tags, so the git SHA
is carried as an immutable Cloud Run `--revision-suffix git-<sha>`; traffic shift is
the floating pointer. Override `REVISION_SUFFIX`/`$env:REVISION_SUFFIX` to re-deploy
an unchanged SHA.

## Step 0 -- one-time console setup

1. GCP: create project, enable `run.googleapis.com`, `secretmanager.googleapis.com`, `artifactregistry.googleapis.com`. `gcloud auth login`.
2. GCP billing alert (specs/08 SCALE-02/NFRs: `max-instances` capped **with cost alert**):
   Billing -> Budgets & alerts -> create budget on the project (e.g. 50/100/200% of
   expected monthly) with email + Pub/Sub notifications. Deploy scripts cap
   `--max-instances 3` (prod + staging, override via `MAX_INSTANCES`/`$env:MAX_INSTANCES`);
   the budget alert is the second rail. The cap of 3 is load-bearing for the
   in-memory rate limiter (specs/08 §3.2: effective global limit = per-instance
   limit x instances) -- raise it only together with the Redis limiter swap.
3. Neon: create project in `europe-west1` (or closest), then create branch `staging` from `main`.
   For each branch copy TWO URLs from the Neon dashboard:
   - Pooled (runtime): `...-pooler.<region>.neon.tech:5432/<db>?sslmode=require` -> prefix `postgresql+asyncpg://`
   - Direct (jobs): non-pooled host -> plain `postgresql://`
4. Clerk (dashboard -- do first, everything references it): create TWO applications,
   `kromarag-dev` and `kromarag-prod` (separate keys; never share).
   In each:
   - Enable Google OAuth (replaces `GOOGLE_CLIENT_ID/SECRET` on Vercel -- delete those).
   - Allowed redirect origins: `http://localhost:3000`, staging URL, `https://kromarag.vercel.app`.
   - Create JWT template named `kromarag`: custom claims `email: {{user.primary_email_address}}`,
     `roles: {{user.public_metadata.roles}}`, `aud: kromarag-web`, lifetime 60s.
   - Record the issuer (`https://<app>.clerk.accounts.dev` or custom domain) -- this becomes
     backend `JWT_ISSUER`, and its `/.well-known/jwks.json` the JWKS URL.
   - Role convention: `publicMetadata = { "roles": ["viewer"] }` default; editors/admins via
     Dashboard -> Users -> Metadata (click path: Users -> select user -> Metadata -> edit
     Public metadata -> `{ "roles": ["editor"] }` -> Save). Create smoke-test users
     (`smoke-viewer`/`-editor`/`-admin`) with fixed roles.
5. Vercel: import the repo, set Root Directory `apps/web`, Framework Next.js, Node `22.x`.
   Connect the `staging` git branch so it gets the stable `kromarag-staging.vercel.app` URL.

## Step 1 -- secrets (PowerShell shown; bash equivalent beside each file)

```powershell
$env:GCP_PROJECT_ID = "my-gcp-project"
$env:PROD_DATABASE_URL = "postgresql+asyncpg://user:pw@xxx-pooler.eu-west-1.neon.tech:5432/db?sslmode=require"
$env:PROD_DATABASE_URL_SYNC = "postgresql://user:pw@xxx.eu-west-1.neon.tech:5432/db?sslmode=require"
$env:PROD_GOOGLE_API_KEY = "AIza..."; $env:PROD_GROQ_API_KEY = "gsk_..."; $env:PROD_LANGSMITH_API_KEY = "lsv2_..."
# ... same STAGING_* vars with the staging branch URLs ...
.\scripts\deploy\00-secrets.ps1
# dev-bypass AUTH_SECRET (local .env only, never Vercel/Cloud Run):
# openssl rand -base64 48  (Git Bash) or
# [Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Max 256 }))
# paste the SAME value into apps/web/.env.local AUTH_SECRET + apps/api/.env AUTH_SECRET
```

`00-secrets` fail-fasts on non-pooled runtime URLs (mirrors the prod boot guard).
No `AUTH_SECRET_*` lives in Secret Manager anymore (Clerk cutover); the HS256
dev bypass uses the local `.env` secret only.

## Step 2 -- backend

```powershell
$env:GCP_PROJECT_ID = "my-gcp-project"   # WEB_HOST defaults to kromarag.vercel.app
$env:CLERK_ISSUER = "https://kromarag-prod.clerk.accounts.dev"
.\scripts\deploy\10-cloudrun-prod.ps1     # -> copy PROD API_URL
$env:STAGING_WEB_HOST = "kromarag-staging.vercel.app"
$env:CLERK_ISSUER_STAGING = "https://kromarag-dev.clerk.accounts.dev"
.\scripts\deploy\11-cloudrun-staging.ps1  # -> copy STAGING_API_URL
# Optional external Prometheus scraping (specs/09 §3.2):
#   $env:METRICS_SCRAPE_ALLOWLIST = "203.0.113.7,198.51.100.0/24"  # scraper egress IP/CIDR
# Optional explicit JWKS pin: $env:CLERK_JWKS_URL = "https://<app>.clerk.accounts.dev/.well-known/jwks.json"
```

Entry point runs `001->002->003` migrations then serves (`scripts/docker-entrypoint.sh`).
Watch the first revision logs: `migrations ... ok` then `/readyz 200`. A migration failure aborts boot and the prior revision keeps traffic -- by design.

Custom domain later: redeploy backend with `$env:WEB_HOST = "app.example.com"` so `CORS_ORIGINS`
follows the domain (`JWT_ISSUER` stays the Clerk issuer), then redeploy web.

## Step 3 -- frontend envs + deploy

```powershell
Set-Location apps/web
vercel login; vercel link
$env:PROD_API_URL = "https://kromarag-api-xxx-ew.a.run.app"
$env:STAGING_API_URL = "https://kromarag-api-staging-xxx-ew.a.run.app"
$env:PROD_CLERK_PUBLISHABLE_KEY = "pk_live_..."; $env:PROD_CLERK_SECRET_KEY = "sk_live_..."
$env:DEV_CLERK_PUBLISHABLE_KEY = "pk_test_..."; $env:DEV_CLERK_SECRET_KEY = "sk_test_..."
..\..\scripts\deploy\20-vercel-env.ps1   # path from apps/web; or run from root with equatorial adjust
vercel --prod
```

Dashboard double-check (Production): `NEXT_PUBLIC_API_URL` + `API_URL` = prod Cloud Run URL (no trailing slash),
`NEXT_PUBLIC_USE_PROXY=0`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` = prod key, `CLERK_SECRET_KEY` = prod secret.
Preview: same but staging API URL + Clerk dev keys. Development: local `127.0.0.1:8000` + `USE_PROXY=1` + dev keys.

## Step 4 -- smoke test

```powershell
$env:API_URL = "https://kromarag-api-xxx-ew.a.run.app"
$env:VIEWER_JWT = "..."; $env:EDITOR_JWT = "..."; $env:ADMIN_JWT = "..."   # Clerk session JWTs (template kromarag)
$env:WEB_URL = "https://kromarag.vercel.app"   # enables the /login CSP check
.\scripts\deploy\smoke.ps1
# staging: repeat with staging API URL + Clerk dev-app session JWTs + staging WEB_URL
# tickets (when you cannot copy a session JWT): $env:CLERK_SECRET_KEY='sk_...' python scripts/deploy/mint-clerk-token.py --user-id user_xxx
```

Checks: `/healthz` 200, `/readyz` 200, security headers, CORS evil-origin, unauth 401,
SSE `sourcex1 -> donex1` + `X-Trace-Id` (informational `error` events only INFO-logged:
a fallback notice is stream-continuing, not a failure), viewer upload 403, editor
upload 202 + status + cleanup, guessed-UUID 404, and the `/metrics` dual gate with
trust-derived expectations -- anon 404 means the network gate is closed for your runner
(fail-closed healthy with empty allowlist; authed probes must also 404), anon 401
means it is open (runner allowlisted; viewer 403 + admin 200 follow). To exercise the
open path externally, deploy with `METRICS_SCRAPE_ALLOWLIST` containing your egress IP.

## Step 5 -- extended manual gates (before marking green)

Smoke covers the fast path; specs/08 §7 + exit gates require these staged drills:

```bash
# a. Rate-burst replay (expect 429 + Retry-After + X-RateLimit-*, probes stay 200):
hey -z 60s -q 5 -m POST -H "Authorization: Bearer $VIEWER_JWT" \
  -H 'Content-Type: application/json' -d '{"query":"burst","stream":false}' "$API_URL/api/v1/query"
curl -i "$API_URL/healthz"   # still 200 under burst
# b. Forced fallback (chaos flags boot-fail under ENV=production, so use a temp revision):
gcloud run services update-traffic kromarag-api-staging --region europe-west1 --to-latest  # pin first
# deploy temp revision with ENV=staging + CHAOS_GEMINI_DOWN=true, query once, expect
# source -> token(groq) -> error(LLM_TIMEOUT) -> done(fallback_used=true), one history pair; then delete it.
# c. Zero-secret leak: gcloud logging read --freshness=1h --project $GCP_PROJECT_ID | grep -E '://.*@'  # zero hits
# d. DB post-verify: orphan chunks = 0, vector_dims sample = 768 (psql direct URL).
# e. Kid-rollover drain: after Clerk publishes a new signing kid, grep logs for
#    jwks_previous_key_used draining to ~0 (no operator action needed).
```

## Kid-rollover (no operator action)

Clerk rotates signing keys automatically. The backend fetches JWKS (cache 600s)
and falls back to the cached prev-set on `kid` miss, so overlap is automatic.
Confirm `jwks_previous_key_used` logs drain after rotation. No Secret Manager /
Vercel secret swap is involved (HS256 rotation is gone).

## Rollout (flag-day control)

Land backend first with dual-accept (Clerk RS256 + HS256-current, 1 revision, no
traffic shift) -> deploy web (Clerk) -> confirm -> follow-up revision removing
HS256-accept (prod already rejects kid-less tokens). This avoids a hard
flag-day 401 cliff.

## Backfill (one-time, Clerk sub cutover)

Clerk `sub` is `user_xxx`, replacing `demo|email` / `google-oauth2|…`. Rows keyed
by old subs orphan unless mapped. One-off SQL from the Clerk user export
(join old `demo|email` -> Clerk `user_id` by email; new users start clean):

```sql
-- repeat per table: kroma_documents(owner_sub), kroma_conversations(user_sub), kroma_messages(user_sub)
UPDATE kroma_documents SET owner_sub = map.new_sub FROM (VALUES
  ('demo|viewer@example.com', 'user_xxx_viewer'),
  ('demo|editor@example.com', 'user_xxx_editor')
) AS map(old_sub, new_sub) WHERE owner_sub = map.old_sub;
```

## Rollback

Cloud Run: `gcloud run services update-traffic kromarag-api --region europe-west1 --to-revisions <prior>=100`.
Vercel: Deployments -> prior production deployment -> Promote. DB rollback is forward-fix only (never DROP).

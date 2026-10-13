#!/bin/bash
# KromaRAG -- Vercel env push via CLI (run from apps/web).
# Installs nothing; requires `vercel` CLI logged in (`vercel login`) and linked (`vercel link`).
# Values come from env vars -- never hardcoded. NEXT_PUBLIC_* is baked at build
# time, so this script must run BEFORE `vercel --prod` / before Git auto-builds.
# Identity (Clerk cutover): Google OAuth now lives in the Clerk dashboard
# (kromarag-dev / kromarag-prod apps) — no GOOGLE_CLIENT_ID/SECRET on Vercel.
# AUTH_SECRET is dev-only (local .env files); Vercel never receives it.
#
# Usage:
#   cd apps/web
#   export PROD_API_URL='https://kromarag-api-xxx-ew.a.run.app'       # from 10-cloudrun-prod.sh output
#   export STAGING_API_URL='https://kromarag-api-staging-xxx-ew.a.run.app'
#   export PROD_CLERK_PUBLISHABLE_KEY='pk_live_...' PROD_CLERK_SECRET_KEY='sk_live_...'
#   export DEV_CLERK_PUBLISHABLE_KEY='pk_test_...' DEV_CLERK_SECRET_KEY='sk_test_...'
#   ../scripts/deploy/20-vercel-env.sh
set -euo pipefail

: "${PROD_API_URL:?Set PROD_API_URL to Cloud Run prod URL (no trailing slash)}"
: "${STAGING_API_URL:?Set STAGING_API_URL to Cloud Run staging URL}"
: "${PROD_CLERK_PUBLISHABLE_KEY:?Set PROD_CLERK_PUBLISHABLE_KEY (Clerk kromarag-prod publishable key)}"
: "${PROD_CLERK_SECRET_KEY:?Set PROD_CLERK_SECRET_KEY (Clerk kromarag-prod secret key)}"
: "${DEV_CLERK_PUBLISHABLE_KEY:?Set DEV_CLERK_PUBLISHABLE_KEY (Clerk kromarag-dev publishable key)}"
: "${DEV_CLERK_SECRET_KEY:?Set DEV_CLERK_SECRET_KEY (Clerk kromarag-dev secret key)}"

PROD_HOST="kromarag.vercel.app"

add_env() { # $1=name $2=value $3=targets... (production|preview|development)
  local name="$1" value="$2"; shift 2
  for target in "$@"; do
    printf '%s' "${value}" | vercel env add "${name}" "${target}" --force 2>/dev/null \
      || printf '%s' "${value}" | vercel env add "${name}" "${target}"  # fallthrough for older CLI (interactive overwrite prompt)
  done
}

echo "==> Production envs (NEXT_PUBLIC_* baked at build -- redeploy web after this)"
add_env NEXT_PUBLIC_API_URL "${PROD_API_URL}" production
add_env API_URL "${PROD_API_URL}" production
add_env NEXT_PUBLIC_USE_PROXY "0" production
add_env NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY "${PROD_CLERK_PUBLISHABLE_KEY}" production
add_env CLERK_SECRET_KEY "${PROD_CLERK_SECRET_KEY}" production

echo "==> Preview envs (staging API + Clerk dev app; fill STAGING host after first staging web deploy)"
# If you use a stable staging domain (recommended: kromarag-staging.vercel.app
# via a `staging` git branch), set STAGING_WEB_HOST accordingly; otherwise the
# first preview deploy will print its dynamic URL -- copy it here and re-run.
STAGING_WEB_HOST="${STAGING_WEB_HOST:-kromarag-staging.vercel.app}"
add_env NEXT_PUBLIC_API_URL "${STAGING_API_URL}" preview
add_env API_URL "${STAGING_API_URL}" preview
add_env NEXT_PUBLIC_USE_PROXY "0" preview
add_env NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY "${DEV_CLERK_PUBLISHABLE_KEY}" preview
add_env CLERK_SECRET_KEY "${DEV_CLERK_SECRET_KEY}" preview

echo "==> Development envs (local)"
add_env NEXT_PUBLIC_API_URL "http://127.0.0.1:8000" development
add_env API_URL "http://127.0.0.1:8000" development
add_env NEXT_PUBLIC_USE_PROXY "1" development
add_env NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY "${DEV_CLERK_PUBLISHABLE_KEY}" development
add_env CLERK_SECRET_KEY "${DEV_CLERK_SECRET_KEY}" development

echo ""
echo "done. Now deploy web: vercel --prod  (then verify JWT_ISSUER/CORS on Cloud Run match the Clerk issuer + web URL)"
echo "Clerk dashboard checklist (per app: kromarag-dev, kromarag-prod):"
echo "  - Google OAuth enabled; allowed redirect origins include prod + staging + http://localhost:3000"
echo "  - JWT template 'kromarag': email={{user.primary_email_address}}, roles={{user.public_metadata.roles}}, aud=kromarag-web, 60s"
echo "  - Users -> Metadata: publicMetadata = { \"roles\": [\"viewer\"] } default; smoke users per role"

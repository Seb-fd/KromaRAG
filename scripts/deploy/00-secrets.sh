#!/bin/bash
# KromaRAG -- create/fetch GCP Secret Manager secrets for PROD + STAGING.
# Values are read from env vars (never hardcoded). Clerk is the identity
# provider (kromarag-dev / kromarag-prod apps); no AUTH_SECRET_* lives in
# Secret Manager anymore — the HS256 dev bypass uses a local .env secret only:
#   openssl rand -base64 48   # paste SAME value into apps/web/.env.local
#                             # AUTH_SECRET + apps/api/.env AUTH_SECRET (dev only)
#
# Usage (bash):
#   export GCP_PROJECT_ID=my-gcp-project
#   export PROD_DATABASE_URL='postgresql+asyncpg://user:pw@xxx-pooler.eu-west-1.neon.tech:5432/db?sslmode=require'
#   export PROD_DATABASE_URL_SYNC='postgresql://user:pw@xxx.eu-west-1.neon.tech:5432/db?sslmode=require'
#   export PROD_GOOGLE_API_KEY='AIza...' PROD_GROQ_API_KEY='gsk_...' PROD_LANGSMITH_API_KEY='lsv2_...'
#   export STAGING_DATABASE_URL='...' STAGING_DATABASE_URL_SYNC='...'
#   export STAGING_GOOGLE_API_KEY='...' STAGING_GROQ_API_KEY='...' STAGING_LANGSMITH_API_KEY='...'
#   ./scripts/deploy/00-secrets.sh
set -euo pipefail
: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

warn_not_pooled() { # $1=label $2=url — mirrors the prod boot guard (fail fast here, not at Cloud Run)
  case "$2" in *-pooler*) ;; *) echo "WARN: $1 missing '-pooler' (prod boot requires pooled host)";; esac
  case "$2" in *sslmode=require*) ;; *) echo "WARN: $1 missing 'sslmode=require' (prod boot requires TLS)";; esac
}

put_secret() { # $1=name $2=value
  local name="$1" value="$2"
  if [ -z "${value}" ]; then echo "SKIP empty ${name}"; return 0; fi
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null
    echo "updated ${name}"
  else
    printf '%s' "${value}" | gcloud secrets create "${name}" --data-file=- --replication-policy=automatic >/dev/null
    echo "created ${name}"
  fi
}

[ -n "${PROD_DATABASE_URL:-}" ] && warn_not_pooled "PROD_DATABASE_URL" "${PROD_DATABASE_URL}"
[ -n "${STAGING_DATABASE_URL:-}" ] && warn_not_pooled "STAGING_DATABASE_URL" "${STAGING_DATABASE_URL}"

# Prod (fail-closed: pooled Neon + TLS; Clerk issuer is env, not a secret)
put_secret kromarag-DATABASE-URL-prod "${PROD_DATABASE_URL:-}"
put_secret kromarag-DATABASE-URL-SYNC-prod "${PROD_DATABASE_URL_SYNC:-}"
put_secret kromarag-GOOGLE-API-KEY-prod "${PROD_GOOGLE_API_KEY:-}"
put_secret kromarag-GROQ-API-KEY-prod "${PROD_GROQ_API_KEY:-}"
put_secret kromarag-LANGSMITH-API-KEY-prod "${PROD_LANGSMITH_API_KEY:-}"

# Staging (separate secrets, staging Neon branch)
put_secret kromarag-DATABASE-URL-staging "${STAGING_DATABASE_URL:-}"
put_secret kromarag-DATABASE-URL-SYNC-staging "${STAGING_DATABASE_URL_SYNC:-}"
put_secret kromarag-GOOGLE-API-KEY-staging "${STAGING_GOOGLE_API_KEY:-}"
put_secret kromarag-GROQ-API-KEY-staging "${STAGING_GROQ_API_KEY:-}"
put_secret kromarag-LANGSMITH-API-KEY-staging "${STAGING_LANGSMITH_API_KEY:-}"

echo "done. Verify with: gcloud secrets list --filter='name:kromarag-'"
echo "note: dev-bypass AUTH_SECRET lives only in local .env files (openssl rand -base64 48);"
echo "      Clerk keys (publishable/secret) live in the Clerk dashboard + Vercel env, never here."

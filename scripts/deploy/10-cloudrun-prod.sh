#!/bin/bash
# KromaRAG -- Cloud Run PROD deploy (europe-west1).
# Run from repo root in Cloud Shell (bash) or any shell with `gcloud` authed.
# Prerequisites: secrets already created (see 00-secrets.sh), Neon prod
# pooled + direct URLs ready, Vercel hostname decided, Clerk prod issuer known
# (Clerk dashboard -> kromarag-prod -> API Keys / JWT templates).
#
# Identity (Clerk cutover): JWT_ISSUER is the Clerk prod issuer
# (https://<app>.clerk.accounts.dev), NOT the Vercel URL. CORS stays the web URL.
# Optional JWT_JWKS_URL pin: export CLERK_JWKS_URL='https://<app>.clerk.accounts.dev/.well-known/jwks.json'.
#
# Tagging (specs/08 §2.2): --source builds have no registry tags, so the git
# SHA is carried as an immutable Cloud Run revision suffix (--revision-suffix
# git-<sha>); traffic shift is the floating pointer. Rollback = update-traffic
# to the prior revision (see README.md). Override REVISION_SUFFIX to re-deploy
# the same SHA.
#
# Metrics (specs/09 §3.2): set METRICS_SCRAPE_ALLOWLIST to your scraper egress
# IP/CIDR to allow external Prometheus scraping. Empty (default) = prod
# fail-closed deny-all on /metrics (admin JWT alone is then insufficient).
#
# Usage:
#   GCP_PROJECT_ID=my-gcp-project CLERK_ISSUER='https://kromarag-prod.clerk.accounts.dev' ./scripts/deploy/10-cloudrun-prod.sh
#   # optional overrides:
#   # WEB_HOST=kromarag.vercel.app CLERK_JWKS_URL='https://.../.well-known/jwks.json' GCP_PROJECT_ID=... IMAGE_TAG=git-$(git rev-parse --short HEAD) ./scripts/deploy/10-cloudrun-prod.sh
set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your GCP project id}"
: "${CLERK_ISSUER:?Set CLERK_ISSUER to the Clerk prod issuer (https://<app>.clerk.accounts.dev)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-kromarag-api}"
WEB_HOST="${WEB_HOST:-kromarag.vercel.app}"
WEB_URL="https://${WEB_HOST}"
IMAGE_TAG="${IMAGE_TAG:-git-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
# Cloud Run revision suffix: lowercase alnum + hyphens only.
REVISION_SUFFIX="${REVISION_SUFFIX:-$(printf '%s' "${IMAGE_TAG}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-32 | sed 's/^-*//;s/-*$//')}"
[ -z "${REVISION_SUFFIX}" ] && REVISION_SUFFIX="manual"
# Optional external metrics scraping (specs/09 §3.2); empty = deny-all in prod.
if [ -n "${METRICS_SCRAPE_ALLOWLIST:-}" ]; then
  METRICS_ENV=(--set-env-vars "METRICS_SCRAPE_ALLOWLIST=${METRICS_SCRAPE_ALLOWLIST}")
else
  METRICS_ENV=()
fi
# Optional explicit JWKS pin (default derives {JWT_ISSUER}/.well-known/jwks.json).
if [ -n "${CLERK_JWKS_URL:-}" ]; then
  JWKS_ENV=(--set-env-vars "CLERK_JWKS_URL=${CLERK_JWKS_URL}")
else
  JWKS_ENV=()
fi

echo "==> project=${GCP_PROJECT_ID} region=${REGION} service=${SERVICE} tag=${IMAGE_TAG} revision-suffix=${REVISION_SUFFIX}"
echo "==> web=${WEB_URL} clerk_issuer=${CLERK_ISSUER}"
gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

echo "==> deploying ${SERVICE} (source: apps/api) ..."
gcloud run deploy "${SERVICE}" \
  --source apps/api \
  --region "${REGION}" \
  --revision-suffix "${REVISION_SUFFIX}" \
  --concurrency 80 \
  --timeout 300s \
  --min-instances 1 \
  --max-instances 20 \
  --cpu 2 \
  --memory 2Gi \
  --no-cpu-throttling \
  --allow-unauthenticated \
  --set-env-vars "ENV=production" \
  --set-env-vars "PGVECTOR_DIM=768" \
  --set-env-vars "GEMINI_MODEL=gemini-3.6-flash,EMBEDDING_MODEL=gemini-embedding-001,GROQ_MODEL=openai/gpt-oss-120b,LLM_PRIMARY=gemini,LLM_FALLBACK=groq" \
  --set-env-vars "JWT_ISSUER=${CLERK_ISSUER},JWT_AUDIENCE=kromarag-web,CORS_ORIGINS=${WEB_URL}" \
  --set-env-vars "LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=KromaRAG,LANGCHAIN_ENDPOINT=https://api.smith.langchain.com" \
  --set-env-vars "OTEL_ENABLED=true,OTEL_SAMPLING_OK=0.1,OTEL_SAMPLING_ERROR=1.0,METRICS_ENABLED=true" \
  --set-env-vars "POOL_MIN_SIZE=2,POOL_MAX_SIZE=20,POOL_COMMAND_TIMEOUT=10" \
  --set-env-vars "RATE_LIMIT_QUERY_PER_MIN=100,RATE_LIMIT_INGEST_PER_MIN=20,RATE_LIMIT_UNAUTH_PER_MIN=10,RATE_LIMIT_ADMIN_PER_MIN=300,RATE_LIMIT_READ_PER_MIN=300" \
  --set-env-vars "LLM_TIMEOUT_S=25,LLM_FALLBACK_TIMEOUT_S=12,EMBED_TIMEOUT_S=8,RETRIEVER_TIMEOUT_MS=3000" \
  "${METRICS_ENV[@]}" \
  "${JWKS_ENV[@]}" \
  --set-secrets "DATABASE_URL=kromarag-DATABASE-URL-prod:latest" \
  --set-secrets "DATABASE_URL_SYNC=kromarag-DATABASE-URL-SYNC-prod:latest" \
  --set-secrets "GOOGLE_API_KEY=kromarag-GOOGLE-API-KEY-prod:latest" \
  --set-secrets "GEMINI_API_KEY=kromarag-GOOGLE-API-KEY-prod:latest" \
  --set-secrets "GROQ_API_KEY=kromarag-GROQ-API-KEY-prod:latest" \
  --set-secrets "LANGSMITH_API_KEY=kromarag-LANGSMITH-API-KEY-prod:latest"

API_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)')"
echo ""
echo "==> PROD API: ${API_URL}"
echo "==> next: set Vercel NEXT_PUBLIC_API_URL + API_URL to ${API_URL}"
echo "==> verify: curl -i ${API_URL}/healthz && curl -i ${API_URL}/readyz"

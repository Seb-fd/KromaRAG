#!/bin/bash
# KromaRAG -- Cloud Run STAGING deploy (europe-west1).
# Mirrors prod but: min-instances=0 (save cost), LANGCHAIN_PROJECT=KromaRAG-staging,
# staging Neon branch URLs, staging secret names, Clerk DEV issuer + staging web host.
# Identity (Clerk cutover): JWT_ISSUER is the Clerk dev issuer
# (https://<dev-app>.clerk.accounts.dev), NOT the staging web URL.
# Tagging/metrics semantics match 10-cloudrun-prod.sh (specs/08 §2.2, 09 §3.2).
#
# Usage:
#   GCP_PROJECT_ID=my-gcp-project CLERK_ISSUER_STAGING='https://kromarag-dev.clerk.accounts.dev' STAGING_WEB_HOST=kromarag-staging.vercel.app ./scripts/deploy/11-cloudrun-staging.sh
set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${CLERK_ISSUER_STAGING:?Set CLERK_ISSUER_STAGING to the Clerk dev issuer (https://<dev-app>.clerk.accounts.dev)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-kromarag-api-staging}"
# Same in-memory-limiter cap as prod (specs/08 §3.2): max 3 until Redis lands.
MAX_INSTANCES="${MAX_INSTANCES:-3}"
STAGING_WEB_HOST="${STAGING_WEB_HOST:-kromarag-staging.vercel.app}"
STAGING_WEB_URL="https://${STAGING_WEB_HOST}"
IMAGE_TAG="${IMAGE_TAG:-git-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
REVISION_SUFFIX="${REVISION_SUFFIX:-$(printf '%s' "${IMAGE_TAG}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-32 | sed 's/^-*//;s/-*$//')}"
[ -z "${REVISION_SUFFIX}" ] && REVISION_SUFFIX="manual"
if [ -n "${METRICS_SCRAPE_ALLOWLIST:-}" ]; then
  METRICS_ENV=(--set-env-vars "METRICS_SCRAPE_ALLOWLIST=${METRICS_SCRAPE_ALLOWLIST}")
else
  METRICS_ENV=()
fi
if [ -n "${CLERK_JWKS_URL_STAGING:-}" ]; then
  JWKS_ENV=(--set-env-vars "CLERK_JWKS_URL=${CLERK_JWKS_URL_STAGING}")
else
  JWKS_ENV=()
fi

echo "==> project=${GCP_PROJECT_ID} region=${REGION} service=${SERVICE} tag=${IMAGE_TAG} revision-suffix=${REVISION_SUFFIX} max-instances=${MAX_INSTANCES}"
echo "==> staging web=${STAGING_WEB_URL} clerk_issuer=${CLERK_ISSUER_STAGING}"
gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

gcloud run deploy "${SERVICE}" \
  --source apps/api \
  --region "${REGION}" \
  --revision-suffix "${REVISION_SUFFIX}" \
  --concurrency 80 \
  --timeout 300s \
  --min-instances 0 \
  --max-instances "${MAX_INSTANCES}" \
  --cpu 2 \
  --memory 2Gi \
  --no-cpu-throttling \
  --allow-unauthenticated \
  --set-env-vars "ENV=production" \
  --set-env-vars "PGVECTOR_DIM=768" \
  --set-env-vars "GEMINI_MODEL=gemini-3.6-flash,EMBEDDING_MODEL=gemini-embedding-001,GROQ_MODEL=openai/gpt-oss-120b,LLM_PRIMARY=gemini,LLM_FALLBACK=groq" \
  --set-env-vars "JWT_ISSUER=${CLERK_ISSUER_STAGING},JWT_AUDIENCE=kromarag-web,CORS_ORIGINS=${STAGING_WEB_URL}" \
  --set-env-vars "LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=KromaRAG-staging,LANGCHAIN_ENDPOINT=https://api.smith.langchain.com" \
  --set-env-vars "OTEL_ENABLED=true,OTEL_SAMPLING_OK=0.1,OTEL_SAMPLING_ERROR=1.0,METRICS_ENABLED=true" \
  --set-env-vars "POOL_MIN_SIZE=2,POOL_MAX_SIZE=20,POOL_COMMAND_TIMEOUT=10" \
  --set-env-vars "RATE_LIMIT_QUERY_PER_MIN=100,RATE_LIMIT_INGEST_PER_MIN=20,RATE_LIMIT_UNAUTH_PER_MIN=10,RATE_LIMIT_ADMIN_PER_MIN=300,RATE_LIMIT_READ_PER_MIN=300" \
  --set-env-vars "LLM_TIMEOUT_S=25,LLM_FALLBACK_TIMEOUT_S=12,EMBED_TIMEOUT_S=8,RETRIEVER_TIMEOUT_MS=3000" \
  "${METRICS_ENV[@]}" \
  "${JWKS_ENV[@]}" \
  --set-secrets "DATABASE_URL=kromarag-DATABASE-URL-staging:latest" \
  --set-secrets "DATABASE_URL_SYNC=kromarag-DATABASE-URL-SYNC-staging:latest" \
  --set-secrets "GOOGLE_API_KEY=kromarag-GOOGLE-API-KEY-staging:latest" \
  --set-secrets "GEMINI_API_KEY=kromarag-GOOGLE-API-KEY-staging:latest" \
  --set-secrets "GROQ_API_KEY=kromarag-GROQ-API-KEY-staging:latest" \
  --set-secrets "LANGSMITH_API_KEY=kromarag-LANGSMITH-API-KEY-staging:latest"

API_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)')"
echo ""
echo "==> STAGING API: ${API_URL}"
echo "==> next: set Vercel Preview NEXT_PUBLIC_API_URL + API_URL to ${API_URL}"

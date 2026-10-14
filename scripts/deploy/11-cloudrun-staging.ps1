# KromaRAG -- Cloud Run STAGING deploy (europe-west1), PowerShell version.
# Tagging/metrics semantics match 10-cloudrun-prod.ps1 (specs/08 §2.2, 09 §3.2).
# Identity (Clerk cutover): JWT_ISSUER is the Clerk dev issuer, NOT the staging web URL.
# Usage:
#   $env:GCP_PROJECT_ID = "my-gcp-project"
#   $env:CLERK_ISSUER_STAGING = "https://kromarag-dev.clerk.accounts.dev"
#   $env:STAGING_WEB_HOST = "kromarag-staging.vercel.app"
#   .\scripts\deploy\11-cloudrun-staging.ps1
$ErrorActionPreference = "Stop"

if (-not $env:GCP_PROJECT_ID) { throw "Set `$env:GCP_PROJECT_ID" }
if (-not $env:CLERK_ISSUER_STAGING) { throw "Set `$env:CLERK_ISSUER_STAGING to the Clerk dev issuer (https://<dev-app>.clerk.accounts.dev)" }
$Region  = if ($env:REGION)  { $env:REGION }  else { "europe-west1" }
$Service = if ($env:SERVICE) { $env:SERVICE } else { "kromarag-api-staging" }
$WebHost = if ($env:STAGING_WEB_HOST) { $env:STAGING_WEB_HOST } else { "kromarag-staging.vercel.app" }
# Same in-memory-limiter cap as prod (specs/08 §3.2): max 3 until Redis lands.
$MaxInstances = if ($env:MAX_INSTANCES) { $env:MAX_INSTANCES } else { "3" }
$WebUrl  = "https://$WebHost"
$ClerkIssuer = $env:CLERK_ISSUER_STAGING
$ImageTag = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { "git-manual" }
try { $sha = (git rev-parse --short HEAD 2>$null).Trim(); if ($sha) { $ImageTag = "git-$sha" } } catch {}
$RevisionSuffix = if ($env:REVISION_SUFFIX) { $env:REVISION_SUFFIX } else { ($ImageTag.ToLower() -replace '[^a-z0-9-]', '-').Trim('-') }
if ([string]::IsNullOrEmpty($RevisionSuffix)) { $RevisionSuffix = "manual" }
if ($RevisionSuffix.Length -gt 32) { $RevisionSuffix = $RevisionSuffix.Substring(0, 32).TrimEnd('-') }
$MetricsEnv = @()
if ($env:METRICS_SCRAPE_ALLOWLIST) { $MetricsEnv = @("--set-env-vars", "METRICS_SCRAPE_ALLOWLIST=$($env:METRICS_SCRAPE_ALLOWLIST)") }
$JwksEnv = @()
if ($env:CLERK_JWKS_URL_STAGING) { $JwksEnv = @("--set-env-vars", "CLERK_JWKS_URL=$($env:CLERK_JWKS_URL_STAGING)") }

Write-Host "==> project=$($env:GCP_PROJECT_ID) region=$Region service=$Service staging-web=$WebUrl clerk_issuer=$ClerkIssuer tag=$ImageTag revision-suffix=$RevisionSuffix max-instances=$MaxInstances"
gcloud config set project $env:GCP_PROJECT_ID | Out-Null

& gcloud run deploy $Service `
  --source apps/api `
  --region $Region `
  --revision-suffix $RevisionSuffix `
  --concurrency 80 `
  --timeout 300s `
  --min-instances 0 `
  --max-instances $MaxInstances `
  --cpu 2 `
  --memory 2Gi `
  --no-cpu-throttling `
  --allow-unauthenticated `
  --set-env-vars "ENV=production" `
  --set-env-vars "PGVECTOR_DIM=768" `
  --set-env-vars "GEMINI_MODEL=gemini-3.6-flash,EMBEDDING_MODEL=gemini-embedding-001,GROQ_MODEL=openai/gpt-oss-120b,LLM_PRIMARY=gemini,LLM_FALLBACK=groq" `
  --set-env-vars "JWT_ISSUER=$ClerkIssuer,JWT_AUDIENCE=kromarag-web,CORS_ORIGINS=$WebUrl" `
  --set-env-vars "LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=KromaRAG-staging,LANGCHAIN_ENDPOINT=https://api.smith.langchain.com" `
  --set-env-vars "OTEL_ENABLED=true,OTEL_SAMPLING_OK=0.1,OTEL_SAMPLING_ERROR=1.0,METRICS_ENABLED=true" `
  --set-env-vars "POOL_MIN_SIZE=2,POOL_MAX_SIZE=20,POOL_COMMAND_TIMEOUT=10" `
  --set-env-vars "RATE_LIMIT_QUERY_PER_MIN=100,RATE_LIMIT_INGEST_PER_MIN=20,RATE_LIMIT_UNAUTH_PER_MIN=10,RATE_LIMIT_ADMIN_PER_MIN=300,RATE_LIMIT_READ_PER_MIN=300" `
  --set-env-vars "LLM_TIMEOUT_S=25,LLM_FALLBACK_TIMEOUT_S=12,EMBED_TIMEOUT_S=8,RETRIEVER_TIMEOUT_MS=3000" `
  @MetricsEnv `
  @JwksEnv `
  --set-secrets "DATABASE_URL=kromarag-DATABASE-URL-staging:latest" `
  --set-secrets "DATABASE_URL_SYNC=kromarag-DATABASE-URL-SYNC-staging:latest" `
  --set-secrets "GOOGLE_API_KEY=kromarag-GOOGLE-API-KEY-staging:latest" `
  --set-secrets "GEMINI_API_KEY=kromarag-GOOGLE-API-KEY-staging:latest" `
  --set-secrets "GROQ_API_KEY=kromarag-GROQ-API-KEY-staging:latest" `
  --set-secrets "LANGSMITH_API_KEY=kromarag-LANGSMITH-API-KEY-staging:latest"
if ($LASTEXITCODE -ne 0) { throw "gcloud run deploy failed" }

$ApiUrl = (gcloud run services describe $Service --region $Region --format 'value(status.url)').Trim()
""
Write-Host "==> STAGING API: $ApiUrl"
Write-Host "==> next: set Vercel Preview NEXT_PUBLIC_API_URL + API_URL to $ApiUrl"

# KromaRAG -- Cloud Run PROD deploy (europe-west1), PowerShell version.
# Run from repo root in Windows PowerShell 5.1+ with `gcloud` authed.
# Identity (Clerk cutover): JWT_ISSUER is the Clerk prod issuer
# (https://<app>.clerk.accounts.dev), NOT the Vercel URL. CORS stays the web URL.
# Tagging (specs/08 §2.2): --source builds have no registry tags, so the git
# SHA is carried as an immutable Cloud Run revision suffix (git-<sha>); traffic
# shift is the floating pointer. Rollback = update-traffic to the prior
# revision (see README.md). Override $env:REVISION_SUFFIX to re-deploy same SHA.
# Metrics (specs/09 §3.2): set $env:METRICS_SCRAPE_ALLOWLIST to your scraper
# egress IP/CIDR to allow external scraping. Empty (default) = prod fail-closed
# deny-all on /metrics (admin JWT alone is then insufficient).
#
# Usage:
#   $env:GCP_PROJECT_ID = "my-gcp-project"
#   $env:CLERK_ISSUER = "https://kromarag-prod.clerk.accounts.dev"
#   .\scripts\deploy\10-cloudrun-prod.ps1
#   # optional overrides: $env:WEB_HOST, $env:SERVICE, $env:REGION, $env:REVISION_SUFFIX, $env:METRICS_SCRAPE_ALLOWLIST, $env:CLERK_JWKS_URL
$ErrorActionPreference = "Stop"

if (-not $env:GCP_PROJECT_ID) { throw "Set `$env:GCP_PROJECT_ID to your GCP project id" }
if (-not $env:CLERK_ISSUER) { throw "Set `$env:CLERK_ISSUER to the Clerk prod issuer (https://<app>.clerk.accounts.dev)" }
$Region  = if ($env:REGION)  { $env:REGION }  else { "europe-west1" }
$Service = if ($env:SERVICE) { $env:SERVICE } else { "kromarag-api" }
# In-memory rate limiter (specs/08 §3.2, rate_limit.py): effective global limit
# = per-instance limit x instances. Cap at 3 until the Redis swap lands.
$MaxInstances = if ($env:MAX_INSTANCES) { $env:MAX_INSTANCES } else { "3" }
$WebHost = if ($env:WEB_HOST) { $env:WEB_HOST } else { "kromarag.vercel.app" }
$WebUrl  = "https://$WebHost"
$ClerkIssuer = $env:CLERK_ISSUER
$ImageTag = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { "git-manual" }
try { $sha = (git rev-parse --short HEAD 2>$null).Trim(); if ($sha) { $ImageTag = "git-$sha" } } catch {}
$RevisionSuffix = if ($env:REVISION_SUFFIX) { $env:REVISION_SUFFIX } else { ($ImageTag.ToLower() -replace '[^a-z0-9-]', '-').Trim('-') }
if ([string]::IsNullOrEmpty($RevisionSuffix)) { $RevisionSuffix = "manual" }
if ($RevisionSuffix.Length -gt 32) { $RevisionSuffix = $RevisionSuffix.Substring(0, 32).TrimEnd('-') }
$MetricsEnv = @()
if ($env:METRICS_SCRAPE_ALLOWLIST) { $MetricsEnv = @("--set-env-vars", "METRICS_SCRAPE_ALLOWLIST=$($env:METRICS_SCRAPE_ALLOWLIST)") }
$JwksEnv = @()
if ($env:CLERK_JWKS_URL) { $JwksEnv = @("--set-env-vars", "CLERK_JWKS_URL=$($env:CLERK_JWKS_URL)") }

Write-Host "==> project=$($env:GCP_PROJECT_ID) region=$Region service=$Service web=$WebUrl clerk_issuer=$ClerkIssuer tag=$ImageTag revision-suffix=$RevisionSuffix max-instances=$MaxInstances"
gcloud config set project $env:GCP_PROJECT_ID | Out-Null

Write-Host "==> deploying $Service (source: apps/api) ..."
& gcloud run deploy $Service `
  --source apps/api `
  --region $Region `
  --revision-suffix $RevisionSuffix `
  --concurrency 80 `
  --timeout 300s `
  --min-instances 1 `
  --max-instances $MaxInstances `
  --cpu 2 `
  --memory 2Gi `
  --no-cpu-throttling `
  --allow-unauthenticated `
  --set-env-vars "ENV=production" `
  --set-env-vars "PGVECTOR_DIM=768" `
  --set-env-vars "GEMINI_MODEL=gemini-3.6-flash,EMBEDDING_MODEL=gemini-embedding-001,GROQ_MODEL=openai/gpt-oss-120b,LLM_PRIMARY=gemini,LLM_FALLBACK=groq" `
  --set-env-vars "JWT_ISSUER=$ClerkIssuer,JWT_AUDIENCE=kromarag-web,CORS_ORIGINS=$WebUrl" `
  --set-env-vars "LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=KromaRAG,LANGCHAIN_ENDPOINT=https://api.smith.langchain.com" `
  --set-env-vars "OTEL_ENABLED=true,OTEL_SAMPLING_OK=0.1,OTEL_SAMPLING_ERROR=1.0,METRICS_ENABLED=true" `
  --set-env-vars "POOL_MIN_SIZE=2,POOL_MAX_SIZE=20,POOL_COMMAND_TIMEOUT=10" `
  --set-env-vars "RATE_LIMIT_QUERY_PER_MIN=100,RATE_LIMIT_INGEST_PER_MIN=20,RATE_LIMIT_UNAUTH_PER_MIN=10,RATE_LIMIT_ADMIN_PER_MIN=300,RATE_LIMIT_READ_PER_MIN=300" `
  --set-env-vars "LLM_TIMEOUT_S=25,LLM_FALLBACK_TIMEOUT_S=12,EMBED_TIMEOUT_S=8,RETRIEVER_TIMEOUT_MS=3000" `
  @MetricsEnv `
  @JwksEnv `
  --set-secrets "DATABASE_URL=kromarag-DATABASE-URL-prod:latest" `
  --set-secrets "DATABASE_URL_SYNC=kromarag-DATABASE-URL-SYNC-prod:latest" `
  --set-secrets "GOOGLE_API_KEY=kromarag-GOOGLE-API-KEY-prod:latest" `
  --set-secrets "GEMINI_API_KEY=kromarag-GOOGLE-API-KEY-prod:latest" `
  --set-secrets "GROQ_API_KEY=kromarag-GROQ-API-KEY-prod:latest" `
  --set-secrets "LANGSMITH_API_KEY=kromarag-LANGSMITH-API-KEY-prod:latest"
if ($LASTEXITCODE -ne 0) { throw "gcloud run deploy failed" }

$ApiUrl = (gcloud run services describe $Service --region $Region --format 'value(status.url)').Trim()
""
Write-Host "==> PROD API: $ApiUrl"
Write-Host "==> next: set Vercel NEXT_PUBLIC_API_URL + API_URL to $ApiUrl"
Write-Host "==> verify: curl -i $ApiUrl/healthz"

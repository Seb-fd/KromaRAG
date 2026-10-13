# KromaRAG -- create/fetch GCP Secret Manager secrets (PowerShell version).
# Values come from env vars (never hardcoded). Clerk is the identity provider
# (kromarag-dev / kromarag-prod apps); no AUTH_SECRET_* lives in Secret Manager
# anymore — the HS256 dev bypass uses a local .env secret only.
# Generate one with: openssl rand -base64 48  (Git Bash) or
# [Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Max 256 }))
# and paste the SAME value into apps/web/.env.local AUTH_SECRET + apps/api/.env
# AUTH_SECRET (dev only, never Vercel/Cloud Run).
# Usage:
#   $env:GCP_PROJECT_ID = "my-gcp-project"
#   $env:PROD_DATABASE_URL = "postgresql+asyncpg://user:pw@xxx-pooler.eu-west-1.neon.tech:5432/db?sslmode=require"
#   $env:PROD_DATABASE_URL_SYNC = "postgresql://user:pw@xxx.eu-west-1.neon.tech:5432/db?sslmode=require"
#   $env:PROD_GOOGLE_API_KEY = "AIza..."; $env:PROD_GROQ_API_KEY = "gsk_..."; $env:PROD_LANGSMITH_API_KEY = "lsv2_..."
#   # ... same STAGING_* vars for staging ...
#   .\scripts\deploy\00-secrets.ps1
$ErrorActionPreference = "Stop"
if (-not $env:GCP_PROJECT_ID) { throw "Set `$env:GCP_PROJECT_ID" }
gcloud config set project $env:GCP_PROJECT_ID | Out-Null

function Test-PooledUrl($Label, $Value) {
  # Mirrors the prod boot guard (fail fast here, not at Cloud Run).
  if ([string]::IsNullOrEmpty($Value)) { return }
  if ($Value -notlike "*-pooler*") { Write-Host "WARN: $Label missing '-pooler' (prod boot requires pooled host)" }
  if ($Value -notlike "*sslmode=require*") { Write-Host "WARN: $Label missing 'sslmode=require' (prod boot requires TLS)" }
}

function Put-Secret($Name, $Value) {
  if ([string]::IsNullOrEmpty($Value)) { Write-Host "SKIP empty $Name"; return }
  $exists = $true
  & gcloud secrets describe $Name 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { $exists = $false }
  # Use temp file to avoid secret appearing in process args
  $tmp = [System.IO.Path]::GetTempFileName()
  try {
    [System.IO.File]::WriteAllText($tmp, $Value)
    if ($exists) { & gcloud secrets versions add $Name --data-file="$tmp" | Out-Null; Write-Host "updated $Name" }
    else { & gcloud secrets create $Name --data-file="$tmp" --replication-policy=automatic | Out-Null; Write-Host "created $Name" }
  } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

Test-PooledUrl "PROD_DATABASE_URL" $env:PROD_DATABASE_URL
Test-PooledUrl "STAGING_DATABASE_URL" $env:STAGING_DATABASE_URL

Put-Secret "kromarag-DATABASE-URL-prod" $env:PROD_DATABASE_URL
Put-Secret "kromarag-DATABASE-URL-SYNC-prod" $env:PROD_DATABASE_URL_SYNC
Put-Secret "kromarag-GOOGLE-API-KEY-prod" $env:PROD_GOOGLE_API_KEY
Put-Secret "kromarag-GROQ-API-KEY-prod" $env:PROD_GROQ_API_KEY
Put-Secret "kromarag-LANGSMITH-API-KEY-prod" $env:PROD_LANGSMITH_API_KEY

Put-Secret "kromarag-DATABASE-URL-staging" $env:STAGING_DATABASE_URL
Put-Secret "kromarag-DATABASE-URL-SYNC-staging" $env:STAGING_DATABASE_URL_SYNC
Put-Secret "kromarag-GOOGLE-API-KEY-staging" $env:STAGING_GOOGLE_API_KEY
Put-Secret "kromarag-GROQ-API-KEY-staging" $env:STAGING_GROQ_API_KEY
Put-Secret "kromarag-LANGSMITH-API-KEY-staging" $env:STAGING_LANGSMITH_API_KEY

Write-Host "done. Verify with: gcloud secrets list --filter=name:kromarag-"
Write-Host "note: dev-bypass AUTH_SECRET lives only in local .env files (see header);"
Write-Host "      Clerk keys (publishable/secret) live in the Clerk dashboard + Vercel env, never here."

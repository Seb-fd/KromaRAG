# KromaRAG -- Vercel env push via CLI (PowerShell version, run from repo root or apps/web).
# Requires `vercel` CLI logged in (`vercel login`) and linked (`vercel link` from apps/web).
# Identity (Clerk cutover): Google OAuth lives in the Clerk dashboard — no
# GOOGLE_CLIENT_ID/SECRET on Vercel. AUTH_SECRET is dev-only (local .env files).
#
# Usage:
#   Set-Location apps/web
#   $env:PROD_API_URL = "https://kromarag-api-xxx-ew.a.run.app"
#   $env:STAGING_API_URL = "https://kromarag-api-staging-xxx-ew.a.run.app"
#   $env:PROD_CLERK_PUBLISHABLE_KEY = "pk_live_..."; $env:PROD_CLERK_SECRET_KEY = "sk_live_..."
#   $env:DEV_CLERK_PUBLISHABLE_KEY = "pk_test_..."; $env:DEV_CLERK_SECRET_KEY = "sk_test_..."
#   ..\..\scripts\deploy\20-vercel-env.ps1  (adjust path to where you run from)
$ErrorActionPreference = "Stop"

foreach ($v in @("PROD_API_URL","STAGING_API_URL","PROD_CLERK_PUBLISHABLE_KEY","PROD_CLERK_SECRET_KEY","DEV_CLERK_PUBLISHABLE_KEY","DEV_CLERK_SECRET_KEY")) {
  if ([string]::IsNullOrEmpty((Get-Item "env:$v" -ErrorAction SilentlyContinue).Value)) { throw "Set `$env:$v" }
}

$ProdHost = if ($env:PROD_HOST) { $env:PROD_HOST } else { "kromarag.vercel.app" }
$StagingWebHost = if ($env:STAGING_WEB_HOST) { $env:STAGING_WEB_HOST } else { "kromarag-staging.vercel.app" }

function Add-VercelEnv($Name, $Value, [string[]]$Targets) {
  foreach ($t in $Targets) {
    # --force overwrites without prompt (newer CLI); older CLI falls back to piped stdin
    $Value | vercel env add $Name $t --force
    if ($LASTEXITCODE -ne 0) { $Value | vercel env add $Name $t }
  }
}

Write-Host "==> Production envs (baked at build -- redeploy web after this)"
Add-VercelEnv "NEXT_PUBLIC_API_URL" $env:PROD_API_URL @("production")
Add-VercelEnv "API_URL" $env:PROD_API_URL @("production")
Add-VercelEnv "NEXT_PUBLIC_USE_PROXY" "0" @("production")
Add-VercelEnv "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" $env:PROD_CLERK_PUBLISHABLE_KEY @("production")
Add-VercelEnv "CLERK_SECRET_KEY" $env:PROD_CLERK_SECRET_KEY @("production")

Write-Host "==> Preview envs (staging)"
Add-VercelEnv "NEXT_PUBLIC_API_URL" $env:STAGING_API_URL @("preview")
Add-VercelEnv "API_URL" $env:STAGING_API_URL @("preview")
Add-VercelEnv "NEXT_PUBLIC_USE_PROXY" "0" @("preview")
Add-VercelEnv "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" $env:DEV_CLERK_PUBLISHABLE_KEY @("preview")
Add-VercelEnv "CLERK_SECRET_KEY" $env:DEV_CLERK_SECRET_KEY @("preview")

Write-Host "==> Development envs (local)"
Add-VercelEnv "NEXT_PUBLIC_API_URL" "http://127.0.0.1:8000" @("development")
Add-VercelEnv "API_URL" "http://127.0.0.1:8000" @("development")
Add-VercelEnv "NEXT_PUBLIC_USE_PROXY" "1" @("development")
Add-VercelEnv "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" $env:DEV_CLERK_PUBLISHABLE_KEY @("development")
Add-VercelEnv "CLERK_SECRET_KEY" $env:DEV_CLERK_SECRET_KEY @("development")

""
Write-Host "done. Now: vercel --prod"
Write-Host "Clerk dashboard checklist (kromarag-dev + kromarag-prod): Google OAuth, JWT template 'kromarag' (email/roles/aud, 60s), publicMetadata roles."

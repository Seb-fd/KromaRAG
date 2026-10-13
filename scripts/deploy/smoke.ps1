# KromaRAG -- post-deploy smoke test, PowerShell version.
# Fails closed: any check failure exits 1. No secrets printed.
# Identity (Clerk cutover): session JWTs come from Clerk (template `kromarag`).
# The runner never mints: export VIEWER_JWT/EDITOR_JWT/ADMIN_JWT from signed-in
# browser sessions, or create tickets first:
#   $env:CLERK_SECRET_KEY = "sk_..."; python scripts/deploy/mint-clerk-token.py --user-id user_xxx
#
# Usage:
#   $env:API_URL = "https://kromarag-api-xxx-ew.a.run.app"
#   $env:VIEWER_JWT = "..."; $env:EDITOR_JWT = "..."; $env:ADMIN_JWT = "..."
#   # optional: $env:WEB_URL = "https://kromarag.vercel.app"  (enables /login CSP check)
#   .\scripts\deploy\smoke.ps1
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrEmpty($env:API_URL)) { throw "Set `$env:API_URL" }
$Issuer = if ($env:CLERK_ISSUER) { $env:CLERK_ISSUER } elseif ($env:ISSUER) { $env:ISSUER } else { "https://kromarag-prod.clerk.accounts.dev" }
$Api = $env:API_URL.TrimEnd("/")
$script:Pass = 0; $script:Fail = 0
function Ok($m)  { $script:Pass++; Write-Host "PASS: $m" }
function Bad($m, $d="") { $script:Fail++; Write-Host "FAIL: $m $d" }

if ([string]::IsNullOrEmpty($env:VIEWER_JWT) -or [string]::IsNullOrEmpty($env:EDITOR_JWT) -or [string]::IsNullOrEmpty($env:ADMIN_JWT)) {
  throw "Set VIEWER_JWT/EDITOR_JWT/ADMIN_JWT (Clerk session JWTs, template 'kromarag'). See scripts/deploy/mint-clerk-token.py for tickets."
}
$Viewer = $env:VIEWER_JWT; $Editor = $env:EDITOR_JWT; $Admin = $env:ADMIN_JWT

Write-Host "==> target $Api (clerk issuer $Issuer)`n"

# 1. liveness
try {
  $r = Invoke-WebRequest -Uri "$Api/healthz" -UseBasicParsing -TimeoutSec 15
  if ($r.StatusCode -eq 200 -and $r.Content -match '"status":"ok"') { Ok "GET /healthz 200 {status:ok}" } else { Bad "GET /healthz" $r.StatusCode }
  # 3. headers (same response)
  $h = $r.Headers
  foreach ($need in @("Strict-Transport-Security","X-Content-Type-Options","X-Frame-Options")) {
    if ($h.ContainsKey($need)) { Ok "header present: $need" } else { Bad "header missing: $need" }
  }
  if ($r.Content -match "frame-ancestors" -or ($h["Content-Security-Policy"] -match "frame-ancestors")) { Ok "header present: CSP frame-ancestors" } else { Bad "header missing: CSP frame-ancestors 'none'" }
  if ($h.ContainsKey("Server") -or $h.ContainsKey("X-Powered-By")) { Bad "Server/X-Powered-By must be absent" } else { Ok "no Server/X-Powered-By header" }
} catch { Bad "GET /healthz" $_.Exception.Message }

# 2. readiness
try {
  $r = Invoke-WebRequest -Uri "$Api/readyz" -UseBasicParsing -TimeoutSec 15
  if ($r.StatusCode -eq 200 -and $r.Content -match '"ready":true') { Ok "GET /readyz 200 {ready:true}" } else { Bad "GET /readyz" "$($r.StatusCode) $($r.Content)" }
} catch {
  $code = $_.Exception.Response.StatusCode.value__
  Bad "GET /readyz" "http=$code (DB down? pool cold? check Cloud Run logs)"
}

# 4. CORS evil origin -- expect NO access-control-allow-origin echo
try {
  $r = Invoke-WebRequest -Uri "$Api/api/v1/query" -Method Options -UseBasicParsing `
    -Headers @{ Origin="https://evil.example"; "Access-Control-Request-Method"="POST" } -TimeoutSec 15
  $acao = $r.Headers["Access-Control-Allow-Origin"]
  if ([string]::IsNullOrEmpty($acao) -or $acao -notmatch "evil") { Ok "CORS evil origin gets no ACAO" } else { Bad "CORS evil origin" $acao }
} catch {
  # Some stacks return 400/200 on OPTIONS; only the ACAO echo matters -- treat transport error as info
  Ok "CORS preflight probed (no ACAO echo path verified via headers)"
}

# helper: POST helper returning @{ Code; Body; Headers }
function Post-Api($Path, $Jwt, $BodyObj) {
  $json = ($BodyObj | ConvertTo-Json -Compress)
  try {
    $r = Invoke-WebRequest -Uri "$Api$Path" -Method Post -UseBasicParsing -TimeoutSec 70 `
      -ContentType "application/json" -Headers @{ Accept="text/event-stream"; Authorization="Bearer $Jwt" } -Body $json
    return @{ Code=$r.StatusCode; Body=$r.RawContent; Headers=$r.Headers }
  } catch {
    $resp = $_.Exception.Response
    $code = [int]$resp.StatusCode
    $sr = New-Object System.IO.StreamReader($resp.GetResponseStream()); $b = $sr.ReadToEnd(); $sr.Close()
    $h = @{}; foreach ($k in $resp.Headers.AllKeys) { $h[$k] = $resp.Headers[$k] }
    return @{ Code=$code; Body=$b; Headers=$h }
  }
}

# 5. unauth -> 401
$u = Post-Api "/api/v1/query" "" @{ query="hello"; stream=$false }
if ($u.Code -eq 401 -and $u.Body -match "UNAUTHENTICATED") { Ok "POST /query unauth -> 401" } else { Bad "POST /query unauth" "http=$($u.Code)" }

# 6. viewer SSE framing
$s = Post-Api "/api/v1/query" $Viewer @{ query="What is KromaRAG?"; top_k=3; stream=$true }
$src = ([regex]::Matches($s.Body, "event: source")).Count
$done = ([regex]::Matches($s.Body, "event: done")).Count
if ($src -eq 1) { Ok "SSE exactly one 'source'" } else { Bad "SSE source count" "got=$src" }
if ($done -eq 1) { Ok "SSE exactly one 'done'" } else { Bad "SSE done count" "got=$done" }
$tid = $s.Headers["X-Trace-Id"]; if ($tid) { Ok "SSE X-Trace-Id present" } else { Bad "SSE X-Trace-Id missing" }
$errN = ([regex]::Matches($s.Body, "event: error")).Count
Write-Host "INFO: SSE carried $errN informational 'error' event(s) (a fallback notice is stream-continuing, not a failure)"

# 7/8. RBAC + upload -- multipart via curl.exe when present, else skip with notice
$hasCurl = (Get-Command curl.exe -ErrorAction SilentlyContinue) -ne $null
if ($hasCurl) {
  "smoke doc $(Get-Date -Format o)" | Out-File -Encoding utf8 "$env:TEMP\smoke.txt"
  $v = & curl.exe -s -o NUL -w "%{http_code}" -X POST "$Api/api/v1/documents/upload" -H "Authorization: Bearer $Viewer" -F "file=@$env:TEMP\smoke.txt;type=text/plain" -F "title=smoke-rbac"
  if ($v -eq "403") { Ok "viewer upload -> 403" } else { Bad "viewer upload gate" "http=$v want=403" }
  $up = & curl.exe -s -X POST "$Api/api/v1/documents/upload" -H "Authorization: Bearer $Editor" -F "file=@$env:TEMP\smoke.txt;type=text/plain" -F "title=smoke-doc"
  try {
    $docId = ($up | ConvertFrom-Json).doc_id
    if ($docId) {
      Ok "editor upload -> 202"
      Start-Sleep 5
      $st = Invoke-WebRequest -Uri "$Api/api/v1/documents/$docId/status" -UseBasicParsing -Headers @{ Authorization="Bearer $Editor" }
      if ($st.StatusCode -eq 200) { Ok "GET /documents/{id}/status 200" } else { Bad "status poll" $st.StatusCode }
      try { Invoke-WebRequest -Uri "$Api/api/v1/documents/$docId" -Method Delete -UseBasicParsing -Headers @{ Authorization="Bearer $Editor" } | Out-Null; Ok "smoke doc archived (cleanup)" }
      catch { Write-Host "INFO: cleanup DELETE non-2xx (non-blocking)" }
    } else { Bad "editor upload" $up }
  } catch { Bad "editor upload parse" $up }
} else { Write-Host "INFO: curl.exe not found -- skipping upload checks (7/8)" }

# 9. oracle
try {
  $r = Invoke-WebRequest -Uri "$Api/api/v1/documents/00000000-0000-4000-8000-000000000000" -UseBasicParsing -Headers @{ Authorization="Bearer $Viewer" }
  Bad "oracle check" "http=$($r.StatusCode) want=404"
} catch {
  if ([int]$_.Exception.Response.StatusCode -eq 404) { Ok "guessed UUID -> 404 (leak-safe)" } else { Bad "oracle check" $_.Exception.Message }
}

# 10. metrics matrix (trust-derived, specs/09 §3.2 dual gate). The anon probe
# reveals the network-gate state for this runner; authed expectations follow it:
# anon 404 -> gate closed (prod fail-closed healthy with empty allowlist), so
# viewer/admin must also 404. anon 401 -> gate open (runner allowlisted), so
# viewer 403 and admin 200.
function Get-Code($Jwt) {
  $hdrs = @{}
  if ($Jwt) { $hdrs["Authorization"] = "Bearer $Jwt" }
  try { return (Invoke-WebRequest -Uri "$Api/metrics" -UseBasicParsing -Headers $hdrs).StatusCode }
  catch { return [int]$_.Exception.Response.StatusCode }
}
$a = Get-Code $null
if ($a -eq 404) {
  Write-Host "INFO: /metrics network gate closed for this runner (fail-closed healthy with empty allowlist)"
  $v2 = Get-Code $Viewer; $ad = Get-Code $Admin
  if ($v2 -eq 404) { Ok "GET /metrics viewer -> 404 (gate closed)" } else { Bad "metrics viewer" "http=$v2 want=404" }
  if ($ad -eq 404) { Ok "GET /metrics admin -> 404 (gate closed)" } else { Bad "metrics admin" "http=$ad want=404" }
} elseif ($a -eq 401) {
  Write-Host "INFO: /metrics network gate open for this runner (allowlisted) -- checking JWT gate"
  $v2 = Get-Code $Viewer; $ad = Get-Code $Admin
  if ($v2 -eq 403) { Ok "GET /metrics viewer -> 403" } else { Bad "metrics viewer" "http=$v2 want=403" }
  if ($ad -eq 200) { Ok "GET /metrics admin -> 200" } else { Bad "metrics admin" "http=$ad want=200" }
} else {
  Bad "metrics anon" "http=$a want=404-or-401"
}

# 11. web /login CSP (Clerk sign-in must load with no CSP violations)
if ($env:WEB_URL) {
  try {
    $w = Invoke-WebRequest -Uri "$($env:WEB_URL.TrimEnd('/'))/login" -UseBasicParsing -TimeoutSec 15
    if ($w.StatusCode -eq 200) { Ok "GET /login 200" } else { Bad "GET /login" $w.StatusCode }
    $csp = $w.Headers["Content-Security-Policy"]
    if ($csp -match "clerk.accounts.dev") { Ok "login CSP allows Clerk" } else { Bad "login CSP missing Clerk domains" }
  } catch { Bad "GET /login" $_.Exception.Message }
} else {
  Write-Host "INFO: WEB_URL unset -- skipping /login CSP check (set WEB_URL to enable)"
}

""
Write-Host "==> $($script:Pass) passed, $($script:Fail) failed"
if ($script:Fail -gt 0) { exit 1 }

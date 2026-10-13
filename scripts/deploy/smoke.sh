#!/bin/bash
# KromaRAG -- post-deploy smoke test (prod or staging Cloud Run URL).
# Fails closed: any check failure exits non-zero. No secrets printed.
#
# /metrics expectations are trust-derived (specs/09 §3.2 dual gate): the anon
# probe determines the network-gate state for this runner, and the authed
# expectations follow it -- no METRICS_SCRAPE_ALLOWLIST input needed.
#   anon 404 -> gate closed (prod fail-closed healthy with empty allowlist):
#               viewer/admin must also 404.
#   anon 401 -> gate open (runner allowlisted or internal): viewer 403, admin 200.
#
# Identity (Clerk cutover): session JWTs come from Clerk (template `kromarag`:
# email+roles+aud, 60s TTL). The runner never mints: sign in as each fixed
# smoke user (smoke-viewer/-editor/-admin) and export the `Authorization: Bearer`
# session JWTs (DevTools), or create one-time tickets first:
#   CLERK_SECRET_KEY='sk_...' python scripts/deploy/mint-clerk-token.py \
#     --user-id user_xxx --frontend https://kromarag.vercel.app
# CLERK_SECRET_KEY lives ONLY on the smoke runner — never in Cloud Run env
# (the backend needs no Clerk secret for JWKS).
#
# Usage:
#   API_URL='https://kromarag-api-xxx-ew.a.run.app' \
#   VIEWER_JWT='...' EDITOR_JWT='...' ADMIN_JWT='...' ./scripts/deploy/smoke.sh
#   # optional web CSP check (Clerk sign-in must not be CSP-blocked):
#   WEB_URL='https://kromarag.vercel.app' ./scripts/deploy/smoke.sh
set -uo pipefail

: "${API_URL:?Set API_URL to the Cloud Run service URL (no trailing slash)}"
ISSUER="${CLERK_ISSUER:-${ISSUER:-https://kromarag-prod.clerk.accounts.dev}}"
WEB_URL="${WEB_URL:-}"
TMP="$(mktemp -d)"; trap 'rm -rf "${TMP}"' EXIT
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1${2:+ -- $2}"; }

# --- Clerk session JWTs (no local minting) ------------------------------------
if [ -z "${VIEWER_JWT:-}" ] || [ -z "${EDITOR_JWT:-}" ] || [ -z "${ADMIN_JWT:-}" ]; then
  echo "error: VIEWER_JWT/EDITOR_JWT/ADMIN_JWT are required (Clerk session JWTs, template 'kromarag')." >&2
  echo "  Sign in as each smoke user in the browser and copy the Authorization: Bearer header, or" >&2
  echo "  mint tickets: CLERK_SECRET_KEY='sk_...' python scripts/deploy/mint-clerk-token.py --user-id user_xxx" >&2
  exit 2
fi

echo "==> target ${API_URL} (clerk issuer ${ISSUER})"
echo ""

# 1. liveness ------------------------------------------------------------------
code="$(curl -sS -o "${TMP}/health.json" -w '%{http_code}' "${API_URL}/healthz")"
body="$(cat "${TMP}/health.json")"
if [ "${code}" = 200 ] && echo "${body}" | grep -q '"status":"ok"'; then ok "GET /healthz 200 {status:ok}"; else bad "GET /healthz" "http=${code} body=${body}"; fi

# 2. readiness -----------------------------------------------------------------
code="$(curl -sS -o "${TMP}/ready.json" -w '%{http_code}' "${API_URL}/readyz")"
body="$(cat "${TMP}/ready.json")"
if [ "${code}" = 200 ] && echo "${body}" | grep -q '"ready":true'; then ok "GET /readyz 200 {ready:true, db:ok}"; else bad "GET /readyz" "http=${code} body=${body}"; fi

# 3. security headers on /healthz ----------------------------------------------
curl -sSI "${API_URL}/healthz" -o "${TMP}/h.headers" >/dev/null
H="$(tr '[:upper:]' '[:lower:]' < "${TMP}/h.headers")"
for need in "strict-transport-security:" "x-content-type-options: nosniff" "x-frame-options: deny" "frame-ancestors 'none'"; do
  if echo "${H}" | grep -qF "${need}"; then ok "header present: ${need}"; else bad "header missing: ${need}"; fi
done
if echo "${H}" | grep -qE '^server:|^x-powered-by:'; then bad "Server/X-Powered-By must be absent"; else ok "no Server/X-Powered-By header"; fi

# 4. CORS: evil origin gets no ACAO ---------------------------------------------
acao="$(curl -sS -o /dev/null -D - -X OPTIONS "${API_URL}/api/v1/query" \
  -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' | tr -d '\r' | grep -i '^access-control-allow-origin:' || true)"
if [ -z "${acao}" ] || echo "${acao}" | grep -q 'https://evil.example'; then
  if echo "${acao}" | grep -q 'evil'; then bad "CORS evil origin" "${acao}"; else ok "CORS evil origin gets no ACAO"; fi
else ok "CORS evil origin gets no ACAO"; fi

# 5. unauth query -> 401 ---------------------------------------------------------
code="$(curl -sS -o "${TMP}/unauth.json" -w '%{http_code}' -X POST "${API_URL}/api/v1/query" \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"hello","stream":false}')"
if [ "${code}" = 401 ] && grep -q 'UNAUTHENTICATED' "${TMP}/unauth.json"; then ok "POST /query unauth -> 401 UNAUTHENTICATED"; else bad "POST /query unauth" "http=${code}"; fi

# 6. viewer SSE framing: exactly 1x source, 1x done, X-Trace-Id ------------------
curl -sS -D "${TMP}/sse.headers" -o "${TMP}/sse.txt" -X POST "${API_URL}/api/v1/query" \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -H "Authorization: Bearer ${VIEWER_JWT}" \
  -d '{"query":"What is KromaRAG?","top_k":3,"stream":true}' --max-time 60
src="$(grep -c '^event: source' "${TMP}/sse.txt" || true)"
done_n="$(grep -c '^event: done' "${TMP}/sse.txt" || true)"
tid="$(tr -d '\r' < "${TMP}/sse.headers" | grep -i '^x-trace-id:' | awk '{print $2}')"
if [ "${src}" = 1 ]; then ok "SSE exactly one 'source'"; else bad "SSE source count" "got=${src}"; fi
if [ "${done_n}" = 1 ]; then ok "SSE exactly one 'done'"; else bad "SSE done count" "got=${done_n}"; fi
if [ -n "${tid}" ]; then ok "SSE X-Trace-Id present"; else bad "SSE X-Trace-Id missing"; fi
err_n="$(grep -c '^event: error' "${TMP}/sse.txt" || true)"
echo "INFO: SSE carried ${err_n} informational 'error' event(s) (a fallback notice is stream-continuing, not a failure)"

# 7. RBAC: viewer upload -> 403 ---------------------------------------------------
code="$(curl -sS -o "${TMP}/rbac.json" -w '%{http_code}' -X POST "${API_URL}/api/v1/documents/upload" \
  -H "Authorization: Bearer ${VIEWER_JWT}" -F 'file=@README.md;type=text/markdown' -F 'title=smoke-rbac')"
if [ "${code}" = 403 ]; then ok "viewer upload -> 403 (role gate)"; else bad "viewer upload gate" "http=${code} want=403"; fi

# 8. editor upload -> 202 + status poll -------------------------------------------
printf 'smoke doc %s\n' "$(date -u +%FT%TZ)" > "${TMP}/smoke.txt"
code="$(curl -sS -o "${TMP}/up.json" -w '%{http_code}' -X POST "${API_URL}/api/v1/documents/upload" \
  -H "Authorization: Bearer ${EDITOR_JWT}" -F "file=@${TMP}/smoke.txt;type=text/plain" -F 'title=smoke-doc')"
doc_id="$(python3 -c "import json;print(json.load(open('${TMP}/up.json')).get('doc_id',''))" 2>/dev/null || true)"
if [ "${code}" = 202 ] && [ -n "${doc_id}" ]; then ok "editor upload -> 202"; else bad "editor upload" "http=${code}"; doc_id=""; fi
if [ -n "${doc_id}" ]; then
  sleep 5
  scode="$(curl -sS -o "${TMP}/st.json" -w '%{http_code}' "${API_URL}/api/v1/documents/${doc_id}/status" -H "Authorization: Bearer ${EDITOR_JWT}")"
  if [ "${scode}" = 200 ]; then ok "GET /documents/{id}/status 200"; else bad "status poll" "http=${scode}"; fi
  # cleanup: archive (soft-delete) so smoke leaves no visible doc
  curl -sS -o /dev/null -w '%{http_code}' -X DELETE "${API_URL}/api/v1/documents/${doc_id}" -H "Authorization: Bearer ${EDITOR_JWT}" > "${TMP}/del.code"
  if grep -q '204' "${TMP}/del.code"; then ok "smoke doc archived (cleanup)"; else echo "INFO: cleanup DELETE -> $(cat "${TMP}/del.code") (non-blocking)"; fi
fi

# 9. oracle: guessed UUID -> 404 (never 403) ---------------------------------------
code="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/api/v1/documents/00000000-0000-4000-8000-000000000000" -H "Authorization: Bearer ${VIEWER_JWT}")"
if [ "${code}" = 404 ]; then ok "guessed UUID -> 404 (leak-safe)"; else bad "oracle check" "http=${code} want=404"; fi

# 10. metrics matrix (trust-derived, specs/09 §3.2) --------------------------------
c_anon="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/metrics")"
if [ "${c_anon}" = 404 ]; then
  echo "INFO: /metrics network gate closed for this runner (fail-closed healthy with empty allowlist)"
  c_viewer="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/metrics" -H "Authorization: Bearer ${VIEWER_JWT}")"
  c_admin="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/metrics" -H "Authorization: Bearer ${ADMIN_JWT}")"
  if [ "${c_viewer}" = 404 ]; then ok "GET /metrics viewer -> 404 (gate closed)"; else bad "metrics viewer" "http=${c_viewer} want=404"; fi
  if [ "${c_admin}" = 404 ]; then ok "GET /metrics admin -> 404 (gate closed)"; else bad "metrics admin" "http=${c_admin} want=404"; fi
elif [ "${c_anon}" = 401 ]; then
  echo "INFO: /metrics network gate open for this runner (allowlisted) -- checking JWT gate"
  c_viewer="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/metrics" -H "Authorization: Bearer ${VIEWER_JWT}")"
  c_admin="$(curl -sS -o /dev/null -w '%{http_code}' "${API_URL}/metrics" -H "Authorization: Bearer ${ADMIN_JWT}")"
  if [ "${c_viewer}" = 403 ]; then ok "GET /metrics viewer -> 403"; else bad "metrics viewer" "http=${c_viewer} want=403"; fi
  if [ "${c_admin}" = 200 ]; then ok "GET /metrics admin -> 200"; else bad "metrics admin" "http=${c_admin} want=200"; fi
else
  bad "GET /metrics anon" "http=${c_anon} want=404-or-401"
fi

# 11. web /login CSP (Clerk sign-in must load with no CSP violations) -------------
if [ -n "${WEB_URL}" ]; then
  wcode="$(curl -sS -o /dev/null -w '%{http_code}' "${WEB_URL}/login")"
  if [ "${wcode}" = 200 ]; then ok "GET /login 200"; else bad "GET /login" "http=${wcode}"; fi
  curl -sSI "${WEB_URL}/login" -o "${TMP}/login.headers" >/dev/null
  LH="$(tr '[:upper:]' '[:lower:]' < "${TMP}/login.headers")"
  if echo "${LH}" | grep -qF "clerk.accounts.dev"; then ok "login CSP allows Clerk"; else bad "login CSP missing Clerk domains"; fi
else
  echo "INFO: WEB_URL unset -- skipping /login CSP check (set WEB_URL to enable)"
fi

echo ""
echo "==> ${PASS} passed, ${FAIL} failed"
[ "${FAIL}" = 0 ]

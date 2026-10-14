/** Minimal SDD validator: specs exist, DB generic, JWT claims defined. */
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..');
let fail = 0;
const need = [
  'specs/01-system-architecture.md',
  'specs/02-api-contracts.md',
  'specs/03-rag-and-agent-workflow.md',
  'specs/04-observability-and-evals.md',
  'specs/05-development-roadmap.md',
  'specs/06_conversations_and_history_spec.md',
  'specs/07_document_lifecycle_mgmt_spec.md',
  'specs/08_production_hardening_and_deployment_spec.md',
  'specs/09_telemetry_and_operations_spec.md',
  '.env.example', '.env.staging.example', 'render.yaml', 'README.md', 'apps/api/app/main.py', 'apps/web/app/page.tsx',
  'apps/api/migrations/001_kroma_schema.sql',
  'apps/api/migrations/002_conversations_history.sql',
  'apps/api/migrations/003_kroma_documents_lifecycle.sql',
];
for (const f of need) {
  if (!fs.existsSync(path.join(root, f))) { console.error(`MISSING ${f}`); fail = 1; }
}
const env = fs.readFileSync(path.join(root, '.env.example'), 'utf8');
if (!env.includes('DATABASE_URL=postgresql+asyncpg://')) { console.error('FAIL: DATABASE_URL must be generic postgresql+asyncpg://'); fail = 1; }
if (!/^ENV=/m.test(env)) { console.error('FAIL: .env.example must document ENV (development/production switch, specs/08 §6)'); fail = 1; }
if (/SUPABASE_(URL|ANON|SERVICE)/.test(env)) { console.error('FAIL: .env.example must not hard-depend on Supabase keys'); fail = 1; }
// Staging matrix (specs/08 §6): template exists, runs prod guards, points at staging backends.
const stgPath = path.join(root, '.env.staging.example');
if (!fs.existsSync(stgPath)) { console.error('MISSING .env.staging.example'); fail = 1; }
else {
  const stg = fs.readFileSync(stgPath, 'utf8');
  for (const k of ['ENV=production', '-pooler', 'KromaRAG-staging', 'kromarag-dev.clerk.accounts.dev', 'MAX_INSTANCES=3']) {
    if (!stg.includes(k)) { console.error(`FAIL: .env.staging.example missing ${k}`); fail = 1; }
  }
  if (/sk_(live|test)_[A-Za-z0-9]{10,}/.test(stg)) { console.error('FAIL: .env.staging.example must not contain real-looking secrets'); fail = 1; }
}
// Render Blueprint (staging API): health probe, Docker contract, secrets via dashboard only.
const bpPath = path.join(root, 'render.yaml');
if (!fs.existsSync(bpPath)) { console.error('MISSING render.yaml'); fail = 1; }
else {
  const bp = fs.readFileSync(bpPath, 'utf8');
  for (const k of ['healthCheckPath: /healthz', './apps/api/Dockerfile', 'ENV', 'DATABASE_URL', 'JWT_ISSUER', 'CORS_ORIGINS', 'KromaRAG-staging']) {
    if (!bp.includes(k)) { console.error(`FAIL: render.yaml missing ${k}`); fail = 1; }
  }
  // Free tier pinned: Starter triggers Render payment info. Raise only with a billing decision.
  if (!/plan:\s*free\b/.test(bp)) { console.error('FAIL: render.yaml must pin plan: free (no-card staging)'); fail = 1; }
  if (/plan:\s*starter\b/.test(bp)) { console.error('FAIL: render.yaml must not use plan: starter (requires payment info)'); fail = 1; }
  for (const k of ['DATABASE_URL', 'DATABASE_URL_SYNC', 'GOOGLE_API_KEY', 'GROQ_API_KEY', 'LANGSMITH_API_KEY']) {
    const re = new RegExp(`- key: ${k}\\s+sync: false`);
    if (!re.test(bp)) { console.error(`FAIL: render.yaml must declare ${k} with sync:false (dashboard-only secret)`); fail = 1; }
  }
  if (/value:\s*(sk_|AIza|gsk_|npg_)/.test(bp)) { console.error('FAIL: render.yaml must not embed secret values'); fail = 1; }
}
const c2 = fs.readFileSync(path.join(root, 'specs/02-api-contracts.md'), 'utf8');
for (const k of ['"sub"', '"roles"', 'JWT_ISSUER', 'get_current_principal']) {
  if (!c2.includes(k)) { console.error(`FAIL: 02-api-contracts.md missing ${k}`); fail = 1; }
}
// Rewrites must resolve via lib/api-base (bare-host normalization + safe
// fallback) — raw env concatenation breaks `next build` on scheme-less values.
const nc = fs.readFileSync(path.join(root, 'apps/web/next.config.js'), 'utf8');
if (!nc.includes('resolveApiBase')) { console.error('FAIL: next.config.js must resolve rewrite bases via lib/api-base'); fail = 1; }
if (!fs.existsSync(path.join(root, 'apps/web/lib/api-base.js'))) { console.error('MISSING apps/web/lib/api-base.js'); fail = 1; }
// Client fetch bases must share the normalizer — raw `NEXT_PUBLIC_API_URL ?? "http..."`
// fallbacks misparse single-slash values as relative URLs (origin prepend 404s).
// apiBase() itself must delegate (and warn on unset) rather than inline its own logic.
for (const f of ['apps/web/lib/api-client.ts', 'apps/web/lib/store.tsx']) {
  const src = fs.readFileSync(path.join(root, f), 'utf8');
  if (src.includes('NEXT_PUBLIC_API_URL ?? "http')) { console.error(`FAIL: ${f} must not raw-fallback NEXT_PUBLIC_API_URL (use lib/api-base normalization)`); fail = 1; }
}
{
  const client = fs.readFileSync(path.join(root, 'apps/web/lib/api-client.ts'), 'utf8');
  if (!client.includes('normalizeApiBaseUrl') || !client.includes('unset at build')) { console.error('FAIL: lib/api-client.ts apiBase() must normalize and warn on unset env'); fail = 1; }
}
// Spec 06 — conversations & history deltas present
const c6 = fs.readFileSync(path.join(root, 'specs/06_conversations_and_history_spec.md'), 'utf8');
for (const k of ['conv_id', 'HISTORY_MAX_TURNS', '404']) {
  if (!c6.includes(k)) { console.error(`FAIL: 06_conversations_and_history_spec.md missing ${k}`); fail = 1; }
}
// Spec 07 — document lifecycle deltas present
const c7 = fs.readFileSync(path.join(root, 'specs/07_document_lifecycle_mgmt_spec.md'), 'utf8');
for (const k of ['reindex', 'archived', '003']) {
  if (!c7.includes(k)) { console.error(`FAIL: 07_document_lifecycle_mgmt_spec.md missing ${k}`); fail = 1; }
}
// Spec 08 — production hardening & deployment deltas present (Clerk cutover)
const c8 = fs.readFileSync(path.join(root, 'specs/08_production_hardening_and_deployment_spec.md'), 'utf8');
for (const k of ['/healthz', '/readyz', 'RATE_LIMITED', 'DB_UNAVAILABLE', 'protect()', 'clerk', 'kromarag', 'pgBouncer', '001', '002', '003', 'gemini-3.6-flash', 'openai/gpt-oss-120b']) {
  if (!c8.includes(k)) { console.error(`FAIL: 08_production_hardening_and_deployment_spec.md missing ${k}`); fail = 1; }
}
// Spec 09 — telemetry & operations deltas present
const c9 = fs.readFileSync(path.join(root, 'specs/09_telemetry_and_operations_spec.md'), 'utf8');
for (const k of ['traceparent', '/metrics', 'prometheus', 'k6', 'fallback_used', 'p95']) {
  if (!c9.includes(k)) { console.error(`FAIL: 09_telemetry_and_operations_spec.md missing ${k}`); fail = 1; }
}
console.log(fail ? 'spec:check FAILED' : 'spec:check OK — 9 specs, generic DB, JWT claims present');
process.exit(fail);

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
  '.env.example', 'README.md', 'apps/api/app/main.py', 'apps/web/app/page.tsx',
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
const c2 = fs.readFileSync(path.join(root, 'specs/02-api-contracts.md'), 'utf8');
for (const k of ['"sub"', '"roles"', 'JWT_ISSUER', 'get_current_principal']) {
  if (!c2.includes(k)) { console.error(`FAIL: 02-api-contracts.md missing ${k}`); fail = 1; }
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

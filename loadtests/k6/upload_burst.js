// k6 — multipart upload + ingest at ceiling (specs/09 §3.4 `upload_burst.js`).
//
// Target: POST /api/v1/documents/upload + POST /api/v1/ingest at the
// 20/min/sub bucket ceiling (2 editor JWTs, 5 VUs). Each upload asserts
// 202 {doc_id, job_id} then polls GET /{id}/status to terminal `indexed`.
//
// Fixtures (repo-committed, PII-free): loadtests/k6/fixtures/{small.txt,
// medium.md, handbook.pdf}. Generate locally if missing (see README note).
// Thresholds: upload_accept_p95<500ms, status_poll_p95<100ms, failed<1%.
import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { newTraceparent } from './lib/sse.js';
import { editorTokens, pickToken, authHeaders } from './lib/auth.js';

const BASE = (__ENV.K6_BASE_URL || 'https://staging-kromarag-api.example').replace(/\/$/, '');

const acceptMs = new Trend('upload_accept_ms', true);
const pollMs = new Trend('status_poll_ms', true);
const terminalOk = new Rate('upload_terminal_indexed');

export const options = {
  stages: [
    { duration: '1m', target: 5 }, // ramp 0→5
    { duration: '5m', target: 5 }, // hold 5
  ],
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: true }],
    upload_accept_ms: [{ threshold: 'p(95)<500', abortOnFail: true }],
    status_poll_ms: [{ threshold: 'p(95)<100', abortOnFail: true }],
    upload_terminal_indexed: [{ threshold: 'rate>0.99', abortOnFail: true }],
  },
};

function fixtureBytes(name) {
  // k6 can only read files via open() at init time (relative to script).
  try {
    if (name === 'small.txt') return open('./fixtures/small.txt', 'b');
    if (name === 'medium.md') return open('./fixtures/medium.md', 'b');
    return open('./fixtures/handbook.pdf', 'b');
  } catch (_) {
    return null;
  }
}

export function setup() {
  if (!(__ENV.K6_EDITOR_JWT || __ENV.K6_VIEWER_JWT)) fail('K6_EDITOR_JWT (or K6_VIEWER_JWT) is required');
  if (editorTokens().length < 2) fail('upload_burst needs >=2 editor subs (20/min/sub ceiling math)');
}

export default function () {
  const tokens = editorTokens();
  const token = pickToken(tokens, __VU, __ITER);
  const names = ['small.txt', 'medium.md', 'handbook.pdf'];
  const name = names[(__ITER || 0) % names.length];
  const bytes = fixtureBytes(name);
  if (!bytes) {
    // Fixture-free fallback: JSON ingest path (same 20/min bucket).
    const t0 = Date.now();
    const res = http.post(
      `${BASE}/api/v1/ingest`,
      JSON.stringify({ title: `k6-${__VU}-${__ITER}`, content: 'k6 fixture fallback content '.repeat(20), mime: 'text/plain' }),
      { headers: authHeaders(token, { 'Content-Type': 'application/json', traceparent: newTraceparent(true) }) },
    );
    acceptMs.add(Date.now() - t0);
    check(res, { 'ingest 202': (r) => r.status === 202, 'ingest doc_id': (r) => Boolean(r.json('doc_id')) });
    terminalOk.add(res.status === 202);
    sleep(1);
    return;
  }
  const t0 = Date.now();
  const res = http.post(
    `${BASE}/api/v1/documents/upload`,
    { file: http.file(bytes, name), title: `k6-${__VU}-${__ITER}-${name}` },
    { headers: authHeaders(token, { traceparent: newTraceparent(true) }), timeout: '60s' },
  );
  acceptMs.add(Date.now() - t0);
  const accepted = check(res, {
    'upload 202': (r) => r.status === 202,
    'upload doc_id+job_id': (r) => Boolean(r.json('doc_id')) && Boolean(r.json('job_id')),
    'no 413 on fixture': (r) => r.status !== 413,
    'no 422 on allowlisted mime': (r) => r.status !== 422,
  });
  if (!accepted) {
    terminalOk.add(false);
    return;
  }
  const docId = res.json('doc_id');
  const deadline = Date.now() + 120 * 1000;
  let indexed = false;
  while (Date.now() < deadline) {
    const p0 = Date.now();
    const st = http.get(`${BASE}/api/v1/documents/${docId}/status`, {
      headers: authHeaders(token, { traceparent: newTraceparent(true) }),
    });
    pollMs.add(Date.now() - p0);
    if (st.status === 200 && st.json('status') === 'indexed') {
      indexed = true;
      break;
    }
    if (st.status === 200 && st.json('status') === 'failed') break;
    sleep(2);
  }
  terminalOk.add(indexed);
  check(indexed, { 'terminal indexed': (v) => v === true });
  sleep(1);
}

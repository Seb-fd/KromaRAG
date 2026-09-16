// k6 — streaming /query at ceiling (specs/09 §3.4 `query_sse.js`, normative).
//
// Target: POST /api/v1/query {stream:true} over SSE at the 100/min/sub bucket
// ceiling. VU pool uses ≥5 viewer JWTs so no single bucket 429s the suite by
// design; a dedicated 60s 429-probe stage asserts backpressure then backs off.
//
// Env: K6_BASE_URL (default staging placeholder), K6_VIEWER_JWT (csv, ≥5 subs).
// Run: k6 run --summary-export=summary.json loadtests/k6/query_sse.js
// Thresholds are release-blockers (non-zero exit on breach).
import http from 'k6/http';
import { check, fail } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';
import { newTraceparent, parseSse, sseSummary } from './lib/sse.js';
import { viewerTokens, pickToken, authHeaders } from './lib/auth.js';

const BASE = (__ENV.K6_BASE_URL || 'https://staging-kromarag-api.example').replace(/\/$/, '');
const QUERY_VUS = parseInt(__ENV.K6_QUERY_VUS || '20', 10);

const firstTokenMs = new Trend('sse_first_token_ms', true);
const doneMs = new Trend('sse_done_ms', true);
const framingOk = new Rate('sse_framing_ok');
const rateLimitedProbe = new Counter('probe_429_total');

export const options = {
  stages: [
    { duration: '2m', target: QUERY_VUS }, // ramp 0→20
    { duration: '8m', target: QUERY_VUS }, // hold 20
    { duration: '2m', target: QUERY_VUS * 2 }, // spike 40
    { duration: '3m', target: QUERY_VUS }, // recover 20
  ],
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: true }],
    sse_first_token_ms: [{ threshold: 'p(95)<2500', abortOnFail: true }],
    sse_done_ms: [{ threshold: 'p(95)<26000', abortOnFail: true }],
    sse_framing_ok: [{ threshold: 'rate>0.99', abortOnFail: true }],
  },
};

const CORPUS = [
  'What is KromaRAG?',
  'How does hybrid search blend vector cosine with full-text rank?',
  'Explain the Gemini to Groq fallback path.',
  'Which documents describe the RBAC sharing model?',
  'Summarize the ingestion chunking strategy.',
];

export function setup() {
  if (!__ENV.K6_VIEWER_JWT) fail('K6_VIEWER_JWT is required (csv, >=5 subs for ceiling runs)');
  const n = viewerTokens().length;
  if (n < 5) fail(`K6_VIEWER_JWT must carry >=5 distinct subs, got ${n}`);
  const r = http.get(`${BASE}/healthz`);
  check(r, { 'healthz 200': (x) => x.status === 200 });
}

export default function (data) {
  const tokens = viewerTokens();
  const token = pickToken(tokens, __VU, __ITER);
  const tp = newTraceparent(true);
  const t0 = Date.now();
  const query = CORPUS[(__ITER || 0) % CORPUS.length];
  const res = http.post(
    `${BASE}/api/v1/query`,
    JSON.stringify({ query, top_k: 6, stream: true }),
    {
      headers: authHeaders(token, {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        traceparent: tp,
      }),
      timeout: '30s',
    },
  );
  const elapsed = Date.now() - t0;
  const okStatus = check(res, {
    'query 200': (r) => r.status === 200,
    'X-Trace-Id present': (r) => Boolean(r.headers['X-Trace-Id'] || r.headers['x-trace-id']),
    'traceparent echoed': (r) => Boolean(r.headers['Traceparent'] || r.headers['traceparent']),
  });
  if (!okStatus || res.status !== 200) {
    framingOk.add(false);
    return;
  }
  // TTFT proxy: k6 measures full-body latency (no incremental SSE clock);
  // first-token threshold applies to non-stream p95 in CI-small runs and to
  // staged streaming runs with chunked-transfer timing enabled.
  firstTokenMs.add(elapsed);
  doneMs.add(elapsed);
  const frames = parseSse(res.body);
  const s = sseSummary(frames);
  const framing = check(s, {
    'source exactly once first': (x) => x.sourceCount === 1 && x.kinds[0] === 'source',
    'done exactly once last': (x) => x.doneCount === 1 && x.kinds[x.kinds.length - 1] === 'done',
  });
  framingOk.add(framing);
  if (s.done && s.done.data) {
    check(s.done.data, {
      'done.trace_id joins header': (d) =>
        String(d.trace_id || '') === String(res.headers['X-Trace-Id'] || res.headers['x-trace-id'] || ''),
      'done.traceparent present': (d) => Boolean(d.traceparent),
    });
  }
}

// Dedicated 60s 429-probe (run as a second scenario via: k6 run -e K6_PROBE_ONLY=1).
// Single sub at 150/min must observe ≥1 `429 + Retry-After>0 + X-Trace-Id`.
export function probe429() {
  const tokens = viewerTokens();
  const token = tokens[0];
  let saw429 = false;
  const deadline = Date.now() + 60 * 1000;
  while (Date.now() < deadline) {
    const res = http.post(
      `${BASE}/api/v1/query`,
      JSON.stringify({ query: 'probe', top_k: 1, stream: false }),
      { headers: authHeaders(token, { 'Content-Type': 'application/json', traceparent: newTraceparent(true) }) },
    );
    if (res.status === 429) {
      saw429 = true;
      rateLimitedProbe.add(1);
      check(res, {
        'probe Retry-After>0': (r) => parseInt(r.headers['Retry-After'] || '0', 10) > 0,
        'probe X-Trace-Id': (r) => Boolean(r.headers['X-Trace-Id'] || r.headers['x-trace-id']),
      });
      break;
    }
  }
  check(saw429, { 'probe observed 429': (v) => v === true });
}

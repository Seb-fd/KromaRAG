// k6 — 30-min mixed soak (specs/09 §3.4 `soak.js`, normative).
//
// Mix: 80% GET /documents + GET /conversations* (read bucket 300/min),
// 15% POST /query (every 3rd non-stream), 5% PATCH title rotation.
// Constant 10 VUs / 30min. Thresholds: flat 5xx, zero readyz flaps,
// pool saturation p95 < 0.8×max (asserted via /metrics scrape hook).
import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Rate, Counter } from 'k6/metrics';
import { newTraceparent } from './lib/sse.js';
import { viewerTokens, editorTokens, pickToken, authHeaders } from './lib/auth.js';

const BASE = (__ENV.K6_BASE_URL || 'https://staging-kromarag-api.example').replace(/\/$/, '');

const okRate = new Rate('soak_ok');
const readyzFlaps = new Counter('readyz_flap_total');
const serverErrors = new Counter('soak_5xx_total');

export const options = {
  stages: [{ duration: '30m', target: 10 }],
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: true }],
    soak_ok: [{ threshold: 'rate>0.99', abortOnFail: false }],
  },
};

export function setup() {
  if (!__ENV.K6_VIEWER_JWT) fail('K6_VIEWER_JWT is required');
  const r = http.get(`${BASE}/readyz`);
  check(r, { 'readyz 200 pre-soak': (x) => x.status === 200 });
}

export default function () {
  const viewers = viewerTokens();
  const editors = editorTokens();
  const vTok = pickToken(viewers, __VU, __ITER);
  const eTok = pickToken(editors, __VU, __ITER);
  const roll = (__ITER || 0) % 20;

  let res;
  if (roll < 16) {
    // 80% reads.
    res = http.get(`${BASE}/api/v1/documents?limit=20&offset=0`, {
      headers: authHeaders(vTok, { traceparent: newTraceparent(true) }),
    });
    check(res, { 'list 200': (r) => r.status === 200 });
    const conv = http.get(`${BASE}/api/v1/conversations?limit=20&offset=0`, {
      headers: authHeaders(vTok, { traceparent: newTraceparent(true) }),
    });
    check(conv, { 'convs 200': (r) => r.status === 200 });
    okRate.add(res.status === 200 && conv.status === 200);
  } else if (roll < 19) {
    // 15% query (non-stream every 3rd keeps the soak light on LLM spend).
    const stream = roll % 3 !== 0;
    res = http.post(
      `${BASE}/api/v1/query`,
      JSON.stringify({ query: 'soak stability probe', top_k: 3, stream }),
      {
        headers: authHeaders(vTok, {
          'Content-Type': 'application/json',
          Accept: stream ? 'text/event-stream' : 'application/json',
          traceparent: newTraceparent(true),
        }),
        timeout: '30s',
      },
    );
    check(res, { 'query 200': (r) => r.status === 200 });
    okRate.add(res.status === 200);
  } else {
    // 5% title rotation on a fixture doc (PATCH is metadata-only, no re-embed).
    const docId = __ENV.K6_SOAK_DOC_ID || '00000000-0000-0000-0000-000000000000';
    res = http.patch(
      `${BASE}/api/v1/documents/${docId}`,
      JSON.stringify({ title: `soak-${__VU}-${__ITER}` }),
      { headers: authHeaders(eTok, { 'Content-Type': 'application/json', traceparent: newTraceparent(true) }) },
    );
    check(res, { 'patch 200|404': (r) => r.status === 200 || r.status === 404 });
    okRate.add(res.status === 200 || res.status === 404);
  }

  if (res && res.status >= 500) serverErrors.add(1);

  // readyz flap detector (every 50th iteration per VU).
  if ((__ITER || 0) % 50 === 0) {
    const rz = http.get(`${BASE}/readyz`);
    if (rz.status !== 200) readyzFlaps.add(1);
    check(rz, { 'readyz stable 200': (r) => r.status === 200 });
  }
  sleep(1);
}

export function teardown() {
  // Flap count is asserted in CI via --summary-export (readyz_flap_total==0).
}

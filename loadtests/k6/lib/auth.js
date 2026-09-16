// k6 auth helper: rotate through supplied JWTs (specs/09 §3.4).
// Env: K6_VIEWER_JWT (comma-separated for ≥5 subs), K6_EDITOR_JWT, K6_ADMIN_JWT.
function splitList(v) {
  return String(v || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export function viewerTokens() {
  const list = splitList(__ENV.K6_VIEWER_JWT);
  return list.length ? list : [''];
}

export function editorTokens() {
  const list = splitList(__ENV.K6_EDITOR_JWT || __ENV.K6_VIEWER_JWT);
  return list.length ? list : [''];
}

export function pickToken(list, vuId, iterId) {
  if (!list || !list.length) return '';
  const idx = ((vuId || 1) - 1 + (iterId || 0)) % list.length;
  return list[idx];
}

export function authHeaders(token, extra = {}) {
  const h = { ...extra };
  if (token) h['Authorization'] = `Bearer ${token}`;
  return h;
}

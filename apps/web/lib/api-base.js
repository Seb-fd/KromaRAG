/** Resolve the API base URL for next.config.js rewrites (specs/02 §1).
 *
 * Plain JS (no TS) so both `next.config.js` (CJS, no transform) and Vitest
 * can import it. Rules:
 * - `API_URL` wins, then `NEXT_PUBLIC_API_URL`, else localhost dev default.
 * - Trims whitespace, strips trailing slashes.
 * - Missing scheme (`host.tld/path`) is upgraded to `https://` — a bare host
 *   otherwise produces an invalid rewrite destination and fails `next build`.
 * - Unparseable / non-http(s) values warn loudly and fall back, so the build
 *   never fails on a malformed env var (misrouting fails visibly at runtime).
 * Returns an origin (`https://host`) or a `/`-relative base, never "".
 */
const FALLBACK = "http://127.0.0.1:8000";

function warn(msg) {
  if (typeof console !== "undefined" && console.warn) console.warn(`[kroma] ${msg}`);
}

/**
 * Normalize a raw API base value to an absolute origin (or `/`-relative base).
 * Repairs the exact production failure modes seen on Vercel:
 * - bare hosts (`api.example.com` -> `https://api.example.com`)
 * - single-slash schemes (`https:/host` -> `https://host`, via URL parsing)
 * - trailing slashes / whitespace / embedded paths (origin only)
 * - edge paste corruption: surrounding quotes, trailing slashes/whitespace,
 *   and invisible chars (DEL/ZWSP/BOM/controls) at the EDGES are stripped.
 * Security rule: invisible chars in the INTERIOR are NOT stripped — removing
 * them would forge a novel hostname (e.g. `stag<ZWSP>ing.x` collapsing to a
 * different host) that could receive the Clerk Bearer token. Interior
 * corruption fails closed (warn + FALLBACK) instead.
 * Returns "" for empty input (same-origin proxy mode) and FALLBACK otherwise
 * unusable (with a loud warning). Never returns a scheme-less
 * absolute-looking string — those misparse as relative URLs and get
 * `window.location.origin` prepended by fetch (`/https:/host/...` 404s).
 */
const EDGE_LEAD = /^[\u0000-\u0020\u007F-\u009F\u200B-\u200D\uFEFF'"]+/;
const EDGE_TRAIL = /[\u0000-\u0020\u007F-\u009F\u200B-\u200D\uFEFF'"/]+$/;
const INTERIOR_JUNK = /[\u0000-\u001F\u007F-\u009F\u200B-\u200D\uFEFF]/;

function normalizeApiBaseUrl(value) {
  const raw = String(value ?? "").replace(EDGE_LEAD, "").replace(EDGE_TRAIL, "");
  if (!raw) return "";
  if (raw.startsWith("/")) return raw;
  if (INTERIOR_JUNK.test(raw)) {
    warn(`refusing API base with interior control/invisible chars ${JSON.stringify(raw)} — falling back to ${FALLBACK}`);
    return FALLBACK;
  }
  if (raw.startsWith("/")) return raw;
  // Repair single-slash schemes (`https:/host` — the exact baked value that
  // broke the Vercel build): URL parsing alone mangles these, so restore the
  // missing slash before parsing.
  const repaired = raw.replace(/^([a-z][a-z0-9+.-]*:)\/(?!\/)/i, "$1//");
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(repaired) ? repaired : `https://${repaired}`;
  try {
    const u = new URL(withScheme);
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("bad scheme");
    return u.origin;
  } catch {
    warn(`ignoring invalid API base ${JSON.stringify(raw)} — falling back to ${FALLBACK}`);
    return FALLBACK;
  }
}

function resolveApiBase(env) {
  const raw =
    (env && (env.API_URL || env.NEXT_PUBLIC_API_URL)) ||
    (typeof process !== "undefined" && (process.env.API_URL || process.env.NEXT_PUBLIC_API_URL)) ||
    "";
  if (!String(raw).trim()) {
    warn("API_URL/NEXT_PUBLIC_API_URL unset — rewrites fall back to http://127.0.0.1:8000");
    return FALLBACK;
  }
  const out = normalizeApiBaseUrl(raw);
  return out === "" ? FALLBACK : out;
}

module.exports = { normalizeApiBaseUrl, resolveApiBase, API_BASE_FALLBACK: FALLBACK };

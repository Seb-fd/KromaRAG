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

function resolveApiBase(env) {
  const raw = String(
    (env && (env.API_URL || env.NEXT_PUBLIC_API_URL)) ||
      (typeof process !== "undefined" && (process.env.API_URL || process.env.NEXT_PUBLIC_API_URL)) ||
      ""
  )
    .trim()
    .replace(/\/+$/, "");
  if (!raw) {
    warn("API_URL/NEXT_PUBLIC_API_URL unset — rewrites fall back to http://127.0.0.1:8000");
    return FALLBACK;
  }
  if (raw.startsWith("/")) return raw;
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    const u = new URL(withScheme);
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("bad scheme");
    return u.origin;
  } catch {
    warn(`ignoring invalid API base ${JSON.stringify(raw)} — falling back to http://127.0.0.1:8000`);
    return FALLBACK;
  }
}

module.exports = { resolveApiBase, API_BASE_FALLBACK: FALLBACK };

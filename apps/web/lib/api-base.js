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
 * - edge paste corruption: ANYTHING outside printable ASCII at the edges is
 *   stripped (Unicode spaces incl. NBSP, ZWSP/BOM/joiners, controls, DEL),
 *   plus surrounding quotes and trailing slashes. Broad by design — a
 *   trailing NBSP once produced a visually-perfect URL that still threw.
 * Security rule: invisible chars in the INTERIOR are NOT stripped — removing
 * them would forge a novel hostname (e.g. `stag<ZWSP>ing.x` collapsing to a
 * different host) that could receive the Clerk Bearer token. Interior
 * corruption fails closed (warn + FALLBACK) instead.
 * Returns "" for empty input (same-origin proxy mode) and FALLBACK otherwise
 * unusable (with a loud warning). Never returns a scheme-less
 * absolute-looking string — those misparse as relative URLs and get
 * `window.location.origin` prepended by fetch (`/https:/host/...` 404s).
 */
const EDGE_LEAD = /^[^!-~]+/;
const EDGE_TRAIL = /[^!-~]+$/;
const INTERIOR_JUNK = /[^!-~/]/;

/** Copy-pasteable evidence for any rejection: visible length vs actual length
 *  instantly proves a hidden char; the code list identifies it. */
function diag(s) {
  return `len=${s.length} codes[${Array.from(s).map((c) => (c.codePointAt(0) ?? 0).toString(16)).join(" ")}]`;
}

function normalizeApiBaseUrl(value) {
  // Edge-sanitize: ANYTHING outside printable ASCII at the edges goes
  // (Unicode spaces incl. NBSP, ZWSP/BOM/joiners, controls, DEL) plus
  // surrounding quotes and trailing slashes. Broad by design: paste
  // corruption takes forms we cannot enumerate (a trailing NBSP once
  // produced a visually-perfect URL that still threw in `new URL`).
  const raw = String(value ?? "")
    .replace(EDGE_LEAD, "")
    .replace(/^['"]+|['"]+$/g, "")
    .replace(EDGE_TRAIL, "")
    .replace(/\/+$/, "");
  if (!raw) return "";
  if (raw.startsWith("/")) return raw;
  // Interior rule: anything outside printable ASCII (except `/` path
  // separators) fails closed. Rationale: intent is unrecoverable — stripping
  // would forge a novel hostname that could receive the Clerk Bearer token,
  // and guessing is not a strategy. IDN hosts are therefore unsupported
  // (operator-configured API bases are ASCII-only); the diagnostic below
  // makes any rejection a seconds-long diagnosis instead.
  if (INTERIOR_JUNK.test(raw)) {
    warn(`refusing API base with non-URL-safe chars ${JSON.stringify(raw)} ${diag(raw)} — falling back to ${FALLBACK}`);
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
    warn(`ignoring invalid API base ${JSON.stringify(raw)} ${diag(raw)} — falling back to ${FALLBACK}`);
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

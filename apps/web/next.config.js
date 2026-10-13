/** @type {import('next').NextConfig} */
// Dev-aware connect-src: strict in production ('self' + https: only), relaxed
// in dev to allow same-machine API over http + HMR websockets. Previously
// `connect-src 'self' https:` blocked http://127.0.0.1:8000 fetches from
// http://localhost:3000, surfacing as API_UNREACHABLE even with API up.
const isProd = process.env.NODE_ENV === "production";
// Clerk (kromarag-dev / kromarag-prod apps): API + accounts + captcha + avatars.
// Without these, Clerk sign-in silently breaks in prod (blocked connect/script/
// frame). Smoke step loads /login and asserts no CSP violations.
// Per https://clerk.com/docs/security/clerk-csp (manual config):
// - script-src needs Cloudflare Turnstile (https://challenges.cloudflare.com,
//   required for transfer-to-signup captcha on new OAuth users) + abuse/fraud
//   hosts (https://*.protect.clerk.com).
// - connect-src needs the abuse/fraud hosts with trailing :* (served on
//   non-443 ports; a sourceless port matches 443 only).
// - frame-src needs the Turnstile host + abuse/fraud hosts.
// NOTE: `https://clerk.*.com` is NOT a valid CSP source (mid-host wildcards
// are ignored by browsers) — the valid subdomain wildcard
// `https://*.clerk.com` below already covers it.
// NOTE: headers() changes require a `next dev` restart + hard refresh.
const CLERK_CONNECT = "https://*.clerk.accounts.dev https://*.clerk.com https://*.protect.clerk.com:*";
const CLERK_SCRIPT = "https://*.clerk.accounts.dev https://*.clerk.com https://challenges.cloudflare.com https://*.protect.clerk.com";
const CLERK_IMG = "https://img.clerk.com";
const CLERK_FRAME = "https://*.clerk.accounts.dev https://challenges.cloudflare.com https://*.protect.clerk.com";
const connectSrc = isProd
  ? `connect-src 'self' https: ${CLERK_CONNECT}`
  : `connect-src 'self' http://localhost:* http://127.0.0.1:* https: ws: wss: blob: data: ${CLERK_CONNECT}`;
const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
  // Web-appropriate CSP: keeps spec 08 §3.1 "frame-ancestors 'none'" token while
  // allowing Next.js self scripts/styles. API uses default-src 'none' (see middleware.py).
  // blob: in script-src + explicit worker-src allow Clerk's internal blob: workers
  // (both dev and prod); without them Chrome blocks `new Worker(blob:...)`.
  {
    key: "Content-Security-Policy",
    value:
      `default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: ${CLERK_SCRIPT}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: ${CLERK_IMG}; font-src 'self' data:; ${connectSrc}; worker-src 'self' blob:; frame-src 'self' ${CLERK_FRAME}; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`,
  },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "X-DNS-Prefetch-Control", value: "off" },
];

const nextConfig = {
  reactStrictMode: true,
  typedRoutes: false,
  poweredByHeader: false,
  // Standalone output keeps the app portable (Vercel ignores it safely; required only if you add a Docker fallback).
  output: "standalone",
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  // Same-origin proxy: lets the browser call /api/v1/*, /health(z), /ready(z)
  // on :3000 and have Next.js forward to FastAPI. Bypasses CORS/CSP/IPv6
  // (localhost→::1) issues in dev. Prod (Vercel) calls Cloud Run directly via
  // NEXT_PUBLIC_API_URL, so set API_URL per env; never commit secrets here.
  async rewrites() {
    const api = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
    return [
      { source: "/api/v1/:path*", destination: `${api}/api/v1/:path*` },
      { source: "/health", destination: `${api}/health` },
      { source: "/healthz", destination: `${api}/healthz` },
      { source: "/ready", destination: `${api}/ready` },
      { source: "/readyz", destination: `${api}/readyz` },
    ];
  },
};
module.exports = nextConfig;

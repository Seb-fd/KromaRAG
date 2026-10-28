import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

/**
 * Clerk edge gate (Next.js 16 proxy.ts convention).
 *
 * - Pages (/, /login excluded) require a Clerk session via auth.protect().
 * - /api/* stays public at the edge — the FastAPI backend enforces auth
 *   itself (Bearer Clerk JWT, JWKS/RS256); the browser sends the token and
 *   cookies only gate pages.
 * - Static brand assets (logo.webp, file-route icons) stay public too —
 *   otherwise the logo 404s/redirects on the logged-out /login view.
 * - unauthenticatedUrl forces the local /login route so Clerk never falls
 *   back to the hosted accounts.dev/sign-in page.
 */
const isPublic = createRouteMatcher(["/login(.*)", "/api/:path*"]);

// Plain marker cookie stamped by /login/dev (see setDevBypass in
// lib/kroma-token.ts). Dev-only: lets local dev reach `/` without a Clerk
// session. The FastAPI backend still enforces a valid JWT per request, so
// this cookie alone grants no data access. Never honored in production
// (NODE_ENV check here + /login/dev 404s there).
const DEV_COOKIE = "kroma-dev-bypass";

const mw = clerkMiddleware(async (auth, req) => {
  if (process.env.NODE_ENV !== "production" && req.cookies.get(DEV_COOKIE)?.value === "1") return;
  // Next.js redirect() requires an absolute URL — derive it from the
  // incoming request so the edge gate stays on the local /login route
  // (never the hosted accounts.dev/sign-in page).
  if (!isPublic(req)) await auth.protect({ unauthenticatedUrl: new URL("/login", req.url).href });
});

export default mw;
// Next.js 16 proxy convention also accepts a named `proxy` export
// (mirrors the prior `export { auth as proxy }` Auth.js shape).
export { mw as proxy };

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.png|apple-icon.png|logo.webp).*)"],
};

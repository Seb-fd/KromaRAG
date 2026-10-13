import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

/**
 * Clerk edge gate (Next.js 16 proxy.ts convention).
 *
 * - Pages (/, /login excluded) require a Clerk session via auth.protect().
 * - /api/* stays public at the edge — the FastAPI backend enforces auth
 *   itself (Bearer Clerk JWT, JWKS/RS256); the browser sends the token and
 *   cookies only gate pages.
 * - unauthenticatedUrl forces the local /login route so Clerk never falls
 *   back to the hosted accounts.dev/sign-in page.
 */
const isPublic = createRouteMatcher(["/login(.*)", "/api/:path*"]);

const mw = clerkMiddleware(async (auth, req) => {
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
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.png|apple-icon.png).*)"],
};

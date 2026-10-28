"use client";

import { useAuth, useUser } from "@clerk/nextjs";
import { useCallback, useEffect } from "react";
import { isRole, type KromaRole } from "./roles";

/**
 * Single token source for the whole app (Clerk cutover).
 *
 * - Prod/real: Clerk session JWT with the `kromarag` template
 *   (custom claims email={{user.primary_email_address}},
 *   roles={{user.public_metadata.roles}}, aud=kromarag-web, TTL 60s).
 *   Resolved per request — never cached (template TTL is 60s).
 *   Unconditional fallback: if the template request fails/404s, falls back
 *   to the default session token (no template) so local dev doesn't crash.
 * - Dev bypass: strictly NODE_ENV !== "production" + localStorage flag.
 *   Mints HS256 locally via the dev-only /api/dev-token route (server holds
 *   the dev AUTH_SECRET; prod route 404s). Never shipped to prod builds.
 */

export const KROMA_TEMPLATE = "kromarag";
const DEV_FLAG = "kroma-dev-bypass";
const DEV_EMAIL = "kroma-dev-email";
const DEV_ROLE = "kroma-dev-role";
// Plain (non-HttpOnly) marker cookie so the Next edge gate can let local dev
// reach `/` without a Clerk session. Dev-only: the API still enforces a valid
// JWT per request, so this cookie alone grants no data access. Never set in
// production builds (see /login/dev 404 guard + proxy.ts NODE_ENV check).
const DEV_COOKIE = "kroma-dev-bypass";

/** True when the last token attempt fell back (custom template 404/error). */
let _templateMissing = false;

/** Read by UI to explain 401s as backend/template misconfig, not expiry. */
export function clerkTemplateMissing(): boolean {
  return _templateMissing;
}

/** Dev-only hint when the custom template is missing (Clerk 404 /tokens/kromarag). */
function warnTemplateFallback(): void {
  _templateMissing = true;
  if (process.env.NODE_ENV === "production") return;
  try {
    console.error(
      `[kroma-token] Clerk template "${KROMA_TEMPLATE}" unavailable (404/error) — ` +
        `fell back to the default session token. Create it in the Clerk Dashboard → ` +
        `Sessions → Customize session token (name "${KROMA_TEMPLATE}", ` +
        `claims email/roles, aud=kromarag-web). Backend may still 401 until the template exists.`,
    );
  } catch {
    /* ignore */
  }
}

/** Registered Clerk getToken (set by <KromaTokenBridge> inside AppProvider). */
let _bridgeGetToken: ((opts?: { template?: string }) => Promise<string | null>) | null = null;

export function registerKromaTokenGetter(
  fn: ((opts?: { template?: string }) => Promise<string | null>) | null,
): void {
  _bridgeGetToken = fn;
}

export function devBypassActive(): boolean {
  if (process.env.NODE_ENV === "production") return false;
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DEV_FLAG) === "1";
  } catch {
    return false;
  }
}

export function getDevBypassIdentity(): { email: string; role: KromaRole } {
  let email = "admin@kromarag.com";
  let role: KromaRole = "viewer";
  try {
    email = window.localStorage.getItem(DEV_EMAIL) || email;
    const r = window.localStorage.getItem(DEV_ROLE) || "viewer";
    if (isRole(r)) role = r;
  } catch {
    /* defaults */
  }
  return { email, role };
}

export function setDevBypass(email: string, role: KromaRole): void {
  if (process.env.NODE_ENV === "production") return;
  try {
    window.localStorage.setItem(DEV_FLAG, "1");
    window.localStorage.setItem(DEV_EMAIL, email);
    window.localStorage.setItem(DEV_ROLE, role);
    // Edge-gate marker (proxy.ts honors it in non-prod only).
    document.cookie = `${DEV_COOKIE}=1; Path=/; SameSite=Lax`;
  } catch {
    /* ignore */
  }
}

export function clearDevBypass(): void {
  try {
    window.localStorage.removeItem(DEV_FLAG);
    window.localStorage.removeItem(DEV_EMAIL);
    window.localStorage.removeItem(DEV_ROLE);
    document.cookie = `${DEV_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax`;
  } catch {
    /* ignore */
  }
}

async function devBypassToken(): Promise<string | undefined> {
  const { email, role } = getDevBypassIdentity();
  try {
    const r = await fetch("/api/dev-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
      cache: "no-store",
    });
    if (!r.ok) return undefined;
    const j = (await r.json().catch(() => ({}))) as { token?: string };
    return typeof j.token === "string" && j.token ? j.token : undefined;
  } catch {
    return undefined;
  }
}

/** True when a real Clerk session exists in this browser (mirrors useAuth). */
function hasClerkSession(): boolean {
  try {
    const w = window as unknown as { Clerk?: { session?: unknown } };
    return !!w?.Clerk?.session;
  } catch {
    return false;
  }
}

/** Resolve a fresh API JWT per request (Clerk template, 60s TTL — never cache).
 *
 * Precedence: a real Clerk session ALWAYS wins. The dev bypass is strictly a
 * fallback for browsers with no Clerk session — it must never hijack a signed-in
 * user (a stale dev flag once caused "Session expired" on valid sessions).
 *
 * Unconditional fallback: templated token first (`kromarag`), then the bare
 * default session token. The fallback keeps the frontend from crashing with
 * "Auth token unavailable" when the custom template hasn't been created yet
 * (Clerk 404 /tokens/kromarag). Note the backend requires aud=kromarag-web,
 * so fallback tokens may still 401 until the template exists.
 */
export async function getKromaToken(): Promise<string | undefined> {
  if (typeof window === "undefined") return undefined;
  const clerkToken = await resolveClerkToken();
  if (clerkToken) return clerkToken;
  if (process.env.NODE_ENV !== "production" && devBypassActive()) {
    return devBypassToken();
  }
  return undefined;
}

/** Clerk half of getKromaToken: bridge (useAuth) first, then window.Clerk. */
async function resolveClerkToken(): Promise<string | undefined> {
  try {
    if (_bridgeGetToken) {
      try {
        const t = await _bridgeGetToken({ template: KROMA_TEMPLATE });
        if (t) {
          _templateMissing = false;
          return t;
        }
      } catch {
        /* templated request failed (e.g. 404) — fall through to default */
      }
      // Only blame the template when a session actually exists — a signed-out
      // browser (or a dev-bypass-only flow) has no token to template.
      if (hasClerkSession()) warnTemplateFallback();
      try {
        const fallback = await _bridgeGetToken();
        if (fallback) return fallback;
      } catch {
        /* fall through to window.Clerk */
      }
    }
  } catch {
    /* fall through to window.Clerk */
  }
  try {
    const w = window as unknown as {
      Clerk?: { session?: { getToken?: (opts?: object) => Promise<string | null> } };
    };
    const getToken = w.Clerk?.session?.getToken;
    if (!getToken) return undefined;
    try {
      const t = await getToken({ template: KROMA_TEMPLATE });
      if (t) {
        _templateMissing = false;
        return t;
      }
    } catch {
      /* templated request failed (e.g. 404) — fall through to default */
    }
    if (hasClerkSession()) warnTemplateFallback();
    try {
      const fallback = await getToken();
      return fallback ?? undefined;
    } catch {
      return undefined;
    }
  } catch {
    return undefined;
  }
}

/** Normalize Clerk publicMetadata.roles → KromaRole[] (default ["viewer"]). */
export function rolesFromUser(user: { publicMetadata?: unknown } | null | undefined): KromaRole[] {
  const raw =
    (user?.publicMetadata as Record<string, unknown> | undefined)?.["roles"];
  if (!Array.isArray(raw)) return ["viewer"];
  const kept = raw.filter(isRole);
  return kept.length > 0 ? kept : ["viewer"];
}

/** Hook replacing next-auth useSession() at call sites. */
export function useKromaSession() {
  const { isSignedIn, isLoaded } = useAuth();
  const { user } = useUser();
  const roles = rolesFromUser(user);
  const getToken = useCallback(
    async () => getKromaToken(),
    [],
  );
  return {
    isLoaded,
    isSignedIn: isSignedIn ?? false,
    user,
    email: user?.primaryEmailAddress?.emailAddress ?? null,
    roles,
    role: roles[0] ?? ("viewer" as KromaRole),
    getToken,
  };
}

/** Bridge: registers Clerk useAuth().getToken for non-hook call sites (store). */
export function KromaTokenBridge() {
  const { getToken } = useAuth();
  useEffect(() => {
    registerKromaTokenGetter(getToken as typeof _bridgeGetToken);
    return () => registerKromaTokenGetter(null);
  }, [getToken]);
  return null;
}

/**
 * Auth.js v5 — issues API-ready JWTs with {sub, email, roles[]} per specs/02 §5.
 *
 * The Auth.js session cookie is HttpOnly; the browser never sees it. To call
 * FastAPI we mint a short-lived HS256 JWS (AUTH_SECRET) in the session callback
 * and expose it as `session.accessToken`. FastAPI verifies iss/aud/exp/signature
 * in apps/api/app/core/security.py (JWKS or HS256).
 */
import NextAuth, { type DefaultSession } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";
import * as jose from "jose";
import { isRole } from "./lib/roles";

export { KROMA_ROLES, type KromaRole } from "./lib/roles";

declare module "next-auth" {
  interface User {
    roles?: string[];
  }
  interface Session {
    accessToken?: string;
    user: DefaultSession["user"] & { roles?: string[]; provider?: string };
  }
}

/** next-auth beta ships no "next-auth/jwt" type entry — carry roles via a structural cast. */
type TokenWithRoles = { roles?: string[]; provider?: string };

const AUTH_SECRET =
  process.env.AUTH_SECRET ?? "dev-only-insecure-secret-change-me";
const ISSUER = process.env.NEXTAUTH_URL ?? "http://localhost:3000";

if (!process.env.AUTH_SECRET) {
  if (process.env.NODE_ENV === "production") {
    throw new Error(
      "[auth] AUTH_SECRET is missing — set it in apps/web/.env.local to match apps/api/.env",
    );
  }
  console.warn(
    "[auth] AUTH_SECRET missing — using dev fallback. Create apps/web/.env.local matching apps/api/.env or chat/SSE will 401.",
  );
}

async function mintApiToken(args: {
  sub: string;
  email?: string | null;
  name?: string | null;
  roles: string[];
}): Promise<string> {
  return await new jose.SignJWT({
    email: args.email ?? undefined,
    name: args.name ?? undefined,
    roles: args.roles,
  })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(args.sub)
    .setIssuer(ISSUER)
    .setAudience("kromarag-web")
    .setJti(crypto.randomUUID())
    .setIssuedAt()
    .setExpirationTime("8h")
    .sign(new TextEncoder().encode(AUTH_SECRET));
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  secret: AUTH_SECRET,
  trustHost: true,
  session: { strategy: "jwt", maxAge: 8 * 3600 },
  providers: [
    // Demo IdP: one-click sign-in with an explicit role (dev/RBAC simulation).
    // SECURITY (specs/08 SEC-04/SEC-05): strictly disabled in production —
    // otherwise anyone could self-mint any role (incl. admin). Prod uses
    // Google OAuth (above) with server-provisioned roles only.
    ...(process.env.NODE_ENV === "production"
      ? []
      : [
          Credentials({
            id: "credentials",
            name: "Demo access",
            credentials: {
              email: {
                label: "Email",
                type: "email",
                placeholder: "admin@kromarag.com",
              },
              role: { label: "Role", type: "text" },
            },
            authorize: async (creds) => {
              const email = String(creds?.email ?? "")
                .trim()
                .toLowerCase();
              const role = String(creds?.role ?? "viewer");
              if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) || !isRole(role))
                return null;
              return {
                id: `demo|${email}`,
                email,
                name: email.split("@")[0],
                roles: [role],
              };
            },
          }),
        ]),
    ...(process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET
      ? [
          Google({
            clientId: process.env.GOOGLE_CLIENT_ID,
            clientSecret: process.env.GOOGLE_CLIENT_SECRET,
          }),
        ]
      : []),
  ],
  callbacks: {
    async jwt({ token, user, account }) {
      const t = token as typeof token & TokenWithRoles;
      if (user) {
        t.sub = user.id;
        t.roles =
          user.roles && user.roles.length > 0
            ? user.roles.filter(isRole)
            : ["viewer"];
        if (account?.provider) t.provider = account.provider;
      }
      // Roles for demo users are the source of truth at sign-in; OAuth users stay viewer
      // unless roles were provisioned (future: join kroma_user_roles here via DATABASE_URL).
      if (!t.roles) t.roles = ["viewer"];
      return token;
    },
    async session({ session, token }) {
      const t = token as typeof token & TokenWithRoles;
      session.user.roles = (t.roles ?? ["viewer"]).filter(isRole);
      session.user.provider = t.provider ?? "credentials";
      try {
        session.accessToken = await mintApiToken({
          sub: String(token.sub ?? ""),
          email: token.email,
          name: token.name,
          roles: session.user.roles,
        });
      } catch (e) {
        console.error(
          "[auth] mintApiToken failed — Authorization header will be missing:",
          e,
        );
        session.accessToken = undefined;
      }
      return session;
    },
  },
  pages: { signIn: "/login" },
});

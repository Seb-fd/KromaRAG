import { createHmac, randomUUID } from "crypto";
import { NextResponse } from "next/server";
import { isRole } from "@/lib/roles";

/**
 * Dev-only HS256 mint for the Credentials bypass (Clerk cutover).
 *
 * Strictly disabled in production (404) — otherwise anyone could self-mint
 * any role. Dev web mints with the dev AUTH_SECRET; the local FastAPI
 * verifies with JWT_ISSUER=http://localhost:3000, jwt_jwks_enabled=false.
 * Prod physically cannot accept these tokens (backend rejects kid-less HS256).
 */

const ISSUER = "http://localhost:3000";
const AUDIENCE = "kromarag-web";

function b64url(input: Buffer | string): string {
  const b = typeof input === "string" ? Buffer.from(input) : input;
  return b.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function signHS256(payload: Record<string, unknown>, secret: string): string {
  const header = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = b64url(JSON.stringify(payload));
  const sig = createHmac("sha256", secret).update(`${header}.${body}`).digest();
  return `${header}.${body}.${b64url(sig)}`;
}

export async function POST(req: Request) {
  if (process.env.NODE_ENV === "production") {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  const secret =
    process.env.AUTH_SECRET ?? "dev-only-insecure-secret-change-me";
  let email = "";
  let role: string = "viewer";
  try {
    const j = (await req.json().catch(() => ({}))) as { email?: unknown; role?: unknown };
    email = String(j.email ?? "").trim().toLowerCase();
    role = String(j.role ?? "viewer");
  } catch {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) || !isRole(role)) {
    return NextResponse.json({ error: "invalid email or role" }, { status: 400 });
  }
  const now = Math.floor(Date.now() / 1000);
  const token = signHS256(
    {
      email,
      name: email.split("@")[0],
      roles: [role],
      sub: `demo|${email}`,
      iss: ISSUER,
      aud: AUDIENCE,
      exp: now + 8 * 3600,
      iat: now,
      jti: randomUUID(),
    },
    secret,
  );
  return NextResponse.json({ token }, { status: 200 });
}

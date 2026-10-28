"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { KROMA_ROLES, type KromaRole } from "@/lib/roles";
import { setDevBypass } from "@/lib/kroma-token";
import { Logo } from "@/components/Logo";

/**
 * Local dev-only sign-in bypass.
 * Never rendered in production (see ./page.tsx guard + prod early-return
 * in setDevBypass + 404 on /api/dev-token in prod).
 */
export function DevBypassForm() {
  const router = useRouter();
  const [email, setEmail] = useState("admin@kromarag.com");
  const [role, setRole] = useState<KromaRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const em = email.trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(em)) {
        setError("Sign-in failed — check the email format and try again.");
        return;
      }
      // Dev-only: stamp the local bypass flag; the HS256 token itself is
      // minted per request by the dev-only /api/dev-token route (404 in prod).
      setDevBypass(em, role);
      router.push("/");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="glass anim-fade-up w-full max-w-sm rounded-2xl p-7 space-y-5"
      style={{ boxShadow: "var(--shadow)" }}
    >
      <div className="flex items-center gap-3">
        <Logo size={40} className="rounded-xl" />
        <div>
          <h1 className="font-semibold tracking-tight text-lg">KromaRAG</h1>
          <p className="text-xs ink-soft">
            Knowledge &amp; Role-Based Vector Architecture
          </p>
        </div>
      </div>
      <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] ink-soft">
        Local dev bypass only — never available in production builds. For real
        sign-in use <Link href="/login" className="underline">/login</Link> via
        Clerk. API calls with this bypass need the backend at
        JWT_ISSUER=http://localhost:3000 (see apps/api/.env.example); with the
        Clerk issuer configured, use a real Clerk session instead.
      </p>
      <div className="space-y-2">
        <label className="text-xs font-medium ink-soft" htmlFor="email">
          Work email
        </label>
        <input
          id="email"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="surface w-full rounded-lg px-3 py-2 text-sm outline-none focus:border-kroma-500"
          placeholder="admin@kromarag.com"
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="text-xs font-medium ink-soft">
          Access role (stamped into your dev JWT)
        </legend>
        <div className="grid grid-cols-3 gap-2">
          {KROMA_ROLES.map((r) => (
            <button
              type="button"
              key={r}
              onClick={() => setRole(r)}
              className={`rounded-lg border px-2 py-2 text-xs font-semibold capitalize transition-all ${role === r ? "border-kroma-500 bg-kroma-500/15 text-current" : "ink-soft hover:opacity-80"}`}
              style={role === r ? {} : { borderColor: "var(--border)" }}
            >
              {r}
            </button>
          ))}
        </div>
        <p className="text-[11px] ink-faint">
          FastAPI enforces this server-side on every query. Upload needs
          editor+, delete needs admin.
        </p>
      </fieldset>
      {error && <p className="text-xs text-red-500">{error}</p>}
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-lg bg-kroma-600 px-4 py-2.5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
      >
        {busy ? "Signing in…" : `Continue as ${role} (dev)`}
      </button>
    </form>
  );
}

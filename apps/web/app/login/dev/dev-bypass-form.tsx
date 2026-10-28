"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { TriangleAlert } from "lucide-react";
import { KROMA_ROLES, type KromaRole } from "@/lib/roles";
import { setDevBypass } from "@/lib/kroma-token";
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

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
      className="glass anim-fade-up w-full max-w-sm space-y-5 rounded-2xl p-7 text-center"
      style={{ boxShadow: "var(--shadow)" }}
    >
      <div className="flex flex-col items-center gap-2">
        <Logo size={44} className="avatar-ring rounded-full" />
        <h1 className="font-display text-lg font-semibold tracking-tight">KromaRAG <Badge variant="warning">dev bypass</Badge></h1>
        <p className="text-xs ink-soft">
          Knowledge &amp; Role-Based Vector Architecture
        </p>
      </div>
      <p className="flex items-start gap-1.5 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-left text-xs ink-soft">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" aria-hidden />
        <span>
          Local dev bypass only — never available in production builds. For real
          sign-in use <Link href="/login" className="underline">/login</Link> via
          Clerk. API calls with this bypass need the backend at
          JWT_ISSUER=http://localhost:3000 (see apps/api/.env.example); with the
          Clerk issuer configured, use a real Clerk session instead.
        </span>
      </p>
      <div className="space-y-2 text-left">
        <label className="text-xs font-medium ink-soft" htmlFor="email">
          Work email
        </label>
        <Input
          id="email"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="admin@kromarag.com"
        />
      </div>
      <fieldset className="space-y-2 text-left">
        <legend className="text-xs font-medium ink-soft">
          Access role (stamped into your dev JWT)
        </legend>
        <div className="grid grid-cols-3 gap-2">
          {KROMA_ROLES.map((r) => (
            <Button
              type="button"
              key={r}
              variant={role === r ? "default" : "outline"}
              size="sm"
              onClick={() => setRole(r)}
              aria-pressed={role === r}
              className={cn("capitalize", role === r && "kroma-gradient-btn border-transparent")}
            >
              {r}
            </Button>
          ))}
        </div>
        <p className="text-center text-xs ink-faint">
          FastAPI enforces this server-side on every query. Upload needs
          editor+, delete needs admin.
        </p>
      </fieldset>
      {error && <p role="alert" className="text-xs text-red-500">{error}</p>}
      <Button
        type="submit"
        disabled={busy}
        className="kroma-gradient-btn h-11 w-full text-[15px]"
      >
        {busy ? "Signing in…" : `Continue as ${role} (dev)`}
      </Button>
    </form>
  );
}

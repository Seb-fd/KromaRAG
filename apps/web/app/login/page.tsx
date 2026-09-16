"use client";

import { useState } from "react";
import Image from "next/image";
import { signIn } from "next-auth/react";
import { KROMA_ROLES, type KromaRole } from "@/lib/roles";

export default function LoginPage() {
  const [email, setEmail] = useState("admin@kromarag.com");
  const [role, setRole] = useState<KromaRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const res = await signIn("credentials", {
      email,
      role,
      redirect: false,
      callbackUrl: "/",
    });
    setBusy(false);
    if (res?.error)
      setError("Sign-in failed — check the email format and try again.");
    else if (res?.ok) window.location.href = res.url ?? "/";
  }

  return (
    <div
      className="min-h-screen grid place-items-center px-4"
      style={{ background: "var(--bg)" }}
    >
      <form
        onSubmit={submit}
        className="glass anim-fade-up w-full max-w-sm rounded-2xl p-7 space-y-5"
        style={{ boxShadow: "var(--shadow)" }}
      >
        <div className="flex items-center gap-3">
          <Image
            alt="KromaRAG Logo"
            className="rounded-xl"
            height={40}
            priority
            src="/logo.webp"
            width={40}
          />
          <div>
            <h1 className="font-semibold tracking-tight text-lg">KromaRAG</h1>
            <p className="text-xs ink-soft">
              Knowledge &amp; Role-Based Vector Architecture
            </p>
          </div>
        </div>
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
            Access role (stamped into your JWT)
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
          {busy ? "Signing in…" : `Continue as ${role}`}
        </button>
      </form>
    </div>
  );
}

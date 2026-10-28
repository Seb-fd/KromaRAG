import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { ShieldCheck, FileSearch, Lock } from "lucide-react";
import { CustomLoginForm } from "../custom-login-form";
import { Logo } from "@/components/Logo";

/**
 * Custom KromaRAG login route (Clerk Client API — no <SignIn/>).
 *
 * Strictly Clerk-only. The legacy dev bypass lives at /login/dev (dev builds
 * only) so this view never renders two panels at once.
 *
 * This catch-all serves /login itself. The explicit
 * /login/sso-callback/page.tsx takes precedence for the Google OAuth
 * handshake (via <AuthenticateWithRedirectCallback/>), and /login/dev takes
 * precedence for the dev bypass. Any other stale sub-path (e.g. old
 * path-routed Clerk steps like /login/factor-one) redirects to /login since
 * the custom form handles sign-in/sign-up/verification inline.
 *
 * Signed-in users are redirected server-side to `/` so an active session
 * never sees (or flashes) the form — this closes the double-login loop
 * where proxy.ts protect() and a stale client state bounced / ↔ /login.
 */
export default async function LoginPage({
  params,
}: {
  params: Promise<{ rest?: string[] }>;
}) {
  const { rest } = await params;
  if (rest && rest.length > 0) {
    redirect("/login");
  }

  const { userId } = await auth();
  if (userId) {
    redirect("/");
  }

  return (
    <div
      className="grid min-h-screen place-items-center px-4 py-10"
      style={{ background: "var(--bg)" }}
    >
      <div className="anim-fade-up grid w-full max-w-3xl overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--surface)] md:grid-cols-[1.05fr_1fr]" style={{ boxShadow: "var(--shadow)" }}>
        {/* Brand panel */}
        <div className="relative hidden flex-col justify-between overflow-hidden p-8 md:flex" style={{ background: "linear-gradient(150deg, #221d5e 0%, #3b2fa0 45%, #5747e3 100%)" }}>
          <div className="pointer-events-none absolute -right-20 -top-20 size-64 rounded-full" style={{ background: "radial-gradient(circle, rgba(179,166,255,0.45), transparent 70%)" }} aria-hidden />
          <div className="pointer-events-none absolute -bottom-24 -left-16 size-72 rounded-full" style={{ background: "radial-gradient(circle, rgba(109,92,255,0.5), transparent 70%)" }} aria-hidden />
          <div className="relative flex items-center gap-3">
            <Logo size={40} className="rounded-xl" />
            <div>
              <p className="font-display text-lg font-semibold tracking-tight text-white">KromaRAG</p>
              <p className="text-xs text-white/70">Knowledge &amp; Role-Based Vector Architecture</p>
            </div>
          </div>
          <ul className="relative space-y-3 text-sm text-white/85">
            <li className="flex items-center gap-2.5">
              <Lock className="size-4 shrink-0 text-white/70" aria-hidden />
              RBAC enforced inside every query
            </li>
            <li className="flex items-center gap-2.5">
              <FileSearch className="size-4 shrink-0 text-white/70" aria-hidden />
              Answers cite exact source chunks
            </li>
            <li className="flex items-center gap-2.5">
              <ShieldCheck className="size-4 shrink-0 text-white/70" aria-hidden />
              Hybrid vector + full-text retrieval
            </li>
          </ul>
          <p className="relative text-xs text-white/55">Google sign-in · new accounts start as viewer</p>
        </div>
        {/* Sign-in panel — centered */}
        <div className="flex flex-col items-center p-7 text-center sm:p-9">
          <div className="mb-5 md:hidden">
            <Logo size={44} className="kroma-gradient-ring mx-auto rounded-2xl" />
          </div>
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Welcome to <span className="kroma-gradient-text">KromaRAG</span>
          </h1>
          <p className="mt-1 text-[13px] ink-soft">Ask across your accessible docs</p>
          <div className="mt-6 w-full max-w-xs">
            <CustomLoginForm />
          </div>
        </div>
      </div>
    </div>
  );
}

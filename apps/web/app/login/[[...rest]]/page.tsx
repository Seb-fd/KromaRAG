import Image from "next/image";
import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { CustomLoginForm } from "../custom-login-form";

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
      className="min-h-screen grid place-items-center px-4 py-10"
      style={{ background: "var(--bg)" }}
    >
      <div
        className="glass anim-fade-up w-full max-w-sm rounded-2xl p-7 space-y-6"
        style={{ boxShadow: "var(--shadow)" }}
      >
        <div className="flex items-center gap-3">
          <Image
            alt="KromaRAG Logo"
            className="rounded-xl"
            height={40}
            priority
            unoptimized
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
        <CustomLoginForm />
      </div>
    </div>
  );
}

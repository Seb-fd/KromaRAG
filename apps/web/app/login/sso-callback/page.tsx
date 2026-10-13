"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useClerk, useSignIn, useSignUp } from "@clerk/nextjs";

/**
 * Clerk SSO callback handler (Future API).
 *
 * Google OAuth starts in ../custom-login-form.tsx via `signIn.sso({
 * strategy: "oauth_google", redirectUrl: "/",
 * redirectCallbackUrl: "/login/sso-callback" })`. On full success Clerk goes
 * straight to `/`; only transfer/MFA/captcha/existing-session cases land
 * here. This route runs the documented Future `finalize()` state machine
 * (no <AuthenticateWithRedirectCallback/> — that's the legacy protocol and
 * mixing them traps users on this route).
 *
 * Role assignment deliberately does NOT happen here: the stashed
 * `kroma-pending-role` choice is applied lazily, best-effort by AppShell once
 * the session exists on `/` (mirrors the working social app's post-auth
 * `syncUser()` — never races or blocks session establishment).
 */
function describeFailure(err: unknown, fallback: string): string {
  if (!err) return fallback;
  // ClerkAPIResponseError shape: { errors: [{ code, message, longMessage }] }
  if (typeof err === "object" && err !== null && "errors" in err) {
    const errs = (err as { errors?: unknown }).errors;
    if (Array.isArray(errs) && errs.length > 0) {
      const first = errs[0] as {
        code?: unknown;
        longMessage?: unknown;
        message?: unknown;
      };
      const code = typeof first.code === "string" ? ` (${first.code})` : "";
      const msg =
        (typeof first.longMessage === "string" && first.longMessage) ||
        (typeof first.message === "string" && first.message) ||
        fallback;
      return `${msg}${code}`;
    }
  }
  if (typeof err === "object" && err !== null) {
    const e = err as {
      code?: unknown;
      longMessage?: unknown;
      message?: unknown;
    };
    const code = typeof e.code === "string" ? ` (${e.code})` : "";
    const msg =
      (typeof e.longMessage === "string" && e.longMessage) ||
      (typeof e.message === "string" && e.message) ||
      fallback;
    // Avoid rendering "[object Object]".
    if (msg !== "[object Object]") return `${msg}${code}`;
  }
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return fallback;
}

function FinalizeOAuth() {
  const clerk = useClerk();
  const { signIn } = useSignIn();
  const { signUp } = useSignUp();
  const router = useRouter();
  const hasRun = useRef(false);
  const [failureDetail, setFailureDetail] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      if (!clerk.loaded || !signIn || !signUp || hasRun.current) return;
      // Prevent Next.js re-running this effect during session activation.
      hasRun.current = true;

      const fail = (err: unknown, fallback: string) => {
        console.error("[sso-callback] finalize failed", err);
        setFailureDetail(describeFailure(err, fallback));
      };

      const goHome = (decorateUrl: (url: string) => string) => {
        const url = decorateUrl("/");
        if (url.startsWith("http")) {
          window.location.href = url;
        } else {
          router.push(url);
          router.refresh();
        }
      };
      const backToLogin = () => {
        router.push("/login");
        router.refresh();
      };

      try {
        // 0. Session already active (OAuth completed, effect re-run after
        // activation, or pre-existing session) → go home. Without this, a
        // signed-in user falls through to step 8 backToLogin(), and the next
        // "Continue with Google" click fails with "already signed in".
        if (clerk.session) {
          router.push("/");
          router.refresh();
          return;
        }

        const finalizeSignIn = async () => {
          const { error } = await signIn.finalize({
            navigate: async ({ session, decorateUrl }) => {
              if (session?.currentTask) return;
              goHome(decorateUrl);
            },
          });
          if (error) {
            fail(error, "Could not activate your session.");
            return false;
          }
          return true;
        };
        const finalizeSignUp = async () => {
          const { error } = await signUp.finalize({
            navigate: async ({ session, decorateUrl }) => {
              if (session?.currentTask) return;
              goHome(decorateUrl);
            },
          });
          if (error) {
            fail(error, "Could not activate your session.");
            return false;
          }
          return true;
        };

        // 1. Sign-in already complete → activate session, go home.
        if (signIn.status === "complete") {
          await finalizeSignIn();
          return;
        }

        // 2. Sign-up used an existing account → transfer to sign-in.
        if (signUp.isTransferable) {
          const { error } = await signIn.create({ transfer: true });
          if (error) {
            fail(error, "Could not transfer your sign-up to sign-in.");
            return;
          }
          if ((signIn.status as string) === "complete") {
            await finalizeSignIn();
            return;
          }
          backToLogin();
          return;
        }

        // 3. Sign-in needs a configured first factor → back to login form.
        if (
          signIn.status === "needs_first_factor" &&
          !signIn.supportedFirstFactors?.every(
            (f) => f.strategy === "enterprise_sso",
          )
        ) {
          backToLogin();
          return;
        }

        // 4. New Google account (no existing user) → transfer to sign-up.
        // Requires Cloudflare Turnstile captcha (see <div id="clerk-captcha" />
        // below + CSP frame/script-src for challenges.cloudflare.com).
        if (signIn.isTransferable) {
          const { error } = await signUp.create({ transfer: true });
          if (error) {
            fail(error, "New-account creation failed (captcha?).");
            return;
          }
          if (signUp.status === "complete") {
            await finalizeSignUp();
            return;
          }
          // Missing requirements (e.g. username) — custom form can't collect
          // them inline, so return to login instead of stranding the user.
          backToLogin();
          return;
        }

        // 5. Sign-up complete (e.g. captcha resolved) → activate, go home.
        if (signUp.status === "complete") {
          await finalizeSignUp();
          return;
        }

        // 6. MFA / new-password required → custom form can't complete it.
        if (
          signIn.status === "needs_second_factor" ||
          (signIn.status as string) === "needs_new_password"
        ) {
          backToLogin();
          return;
        }

        // 7. External account already tied to an active session → activate it.
        const sessionId =
          signIn.existingSession?.sessionId ??
          signUp.existingSession?.sessionId;
        if (sessionId) {
          await clerk.setActive({
            session: sessionId,
            navigate: async ({ session, decorateUrl }) => {
              if (session?.currentTask) return;
              goHome(decorateUrl);
            },
          });
          return;
        }

        // 8. Anything else → home if a session is active (see step 0),
        // else back to login; don't spin forever.
        if (clerk.session) {
          router.push("/");
          router.refresh();
          return;
        }
        backToLogin();
      } catch (err) {
        fail(err, "Google sign-in didn't finish.");
      }
    })();
  }, [clerk, signIn, signUp, router]);

  if (failureDetail) {
    return (
      <div className="space-y-2 text-center">
        <p className="text-sm font-medium">Google sign-in didn&apos;t finish.</p>
        <p className="text-xs ink-soft">
          Your session wasn&apos;t established — try again.
        </p>
        <p role="alert" className="text-xs text-red-500">
          {failureDetail}
        </p>
        <Link
          href="/login"
          className="inline-block rounded-lg bg-kroma-600 px-4 py-2 text-sm font-semibold text-white hover:opacity-90"
        >
          Back to login
        </Link>
      </div>
    );
  }

  return null;
}

export default function SSOCallbackPage() {
  return (
    <div
      className="min-h-screen grid place-items-center px-4 py-10"
      style={{ background: "var(--bg)" }}
    >
      <FinalizeOAuth />
      {/* Required for transfer-to-signup captcha verification. */}
      <div id="clerk-captcha" />
    </div>
  );
}

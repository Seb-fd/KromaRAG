"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth, useSignIn } from "@clerk/nextjs";

/**
 * Google-only KromaRAG sign-in (Clerk Client API — no <SignIn/>).
 *
 * Everyone lands as viewer by default; existing users keep the role an
 * admin assigned (FastAPI + Clerk publicMetadata enforce it server-side).
 * Admins promote via the Clerk Dashboard — there is no self-serve upgrade.
 *
 * Google OAuth (Future API): `signIn.sso({ strategy: "oauth_google",
 * redirectUrl: "/", redirectCallbackUrl: "/login/sso-callback" })` — Clerk
 * goes straight to `/` on full success; only transfer/MFA/captcha cases land
 * on /login/sso-callback, which runs the Future `finalize()` state machine.
 */
export function CustomLoginForm() {
  const router = useRouter();
  const { signIn } = useSignIn();
  const { isSignedIn, isLoaded: authLoaded } = useAuth();

  const [busyOAuth, setBusyOAuth] = useState(false);
  const [error, setError] = useState("");

  // Navigate home AND refresh server components so Clerk's session cookie
  // (set by finalize()) is visible to proxy.ts protect() on `/`. Without
  // refresh() the first push can bounce straight back to /login, looking
  // like a double login / dropped session.
  function goHome() {
    router.push("/");
    router.refresh();
  }

  async function handleOAuth() {
    setError("");
    // Already holding a session (e.g. bounced back here after OAuth
    // completed) → go home instead of starting SSO, which Clerk rejects
    // with "You're already signed in."
    if (authLoaded && isSignedIn) {
      setBusyOAuth(false);
      goHome();
      return;
    }
    if (!signIn) return;
    setBusyOAuth(true);
    try {
      // Future-API SSO start: navigates to Google. On full success Clerk goes
      // straight to `redirectUrl`; only transfer/MFA/captcha cases land on
      // `redirectCallbackUrl`, which runs the finalize() state machine.
      const { error: ssoError } = await signIn.sso({
        strategy: "oauth_google",
        redirectUrl: "/",
        redirectCallbackUrl: "/login/sso-callback",
      });
      if (ssoError) {
        // Race: session appeared between render and click → go home.
        const code =
          typeof ssoError === "object" && ssoError !== null && "code" in ssoError
            ? String((ssoError as { code?: unknown }).code)
            : "";
        const msg =
          typeof ssoError === "object" &&
          ssoError !== null &&
          "message" in ssoError &&
          typeof (ssoError as { message?: unknown }).message === "string"
            ? ((ssoError as { message: string }).message ?? "")
            : "";
        if (code === "already_signed_in" || /already signed in/i.test(msg)) {
          setBusyOAuth(false);
          goHome();
          return;
        }
        setError(
          msg || "Google sign-in failed. Try again.",
        );
        setBusyOAuth(false);
      }
      // On success the browser navigates to Google — no further action here.
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Google sign-in failed. Try again.",
      );
      setBusyOAuth(false);
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="font-semibold tracking-tight text-base">
          Sign in to KromaRAG
        </h2>
        <p className="text-xs ink-soft mt-0.5">
          Continue with Google — new accounts start as viewer
        </p>
      </div>

      {error && (
        <p role="alert" className="text-xs text-red-500">
          {error}
        </p>
      )}

      <button
        type="button"
        onClick={handleOAuth}
        disabled={busyOAuth || signIn == null}
        className="surface flex w-full items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
      >
        {busyOAuth ? (
          <>
            <span className="spinner" aria-hidden />
            Redirecting to Google…
          </>
        ) : authLoaded && isSignedIn ? (
          "Go to app →"
        ) : (
          "Continue with Google"
        )}
      </button>
      <p className="text-[11px] ink-faint text-center">
        Google accounts start as viewer — an admin can upgrade you later.
      </p>
    </div>
  );
}

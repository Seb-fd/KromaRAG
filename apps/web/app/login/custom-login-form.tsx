"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, CircleAlert, Info, Loader2 } from "lucide-react";
import { useAuth, useSignIn } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";

/** Official "G" mark (multi-color SVG, not an emoji). */
function GoogleMark() {
  return (
    <svg className="size-4" viewBox="0 0 24 24" aria-hidden>
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23z" />
      <path fill="#FBBC05" d="M5.84 14.1a6.6 6.6 0 0 1 0-4.2V7.06H2.18a11 11 0 0 0 0 9.88l3.66-2.84z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15A11 11 0 0 0 2.18 7.06l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z" />
    </svg>
  );
}

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
    <div className="w-full space-y-4">
      {error && (
        <p role="alert" className="mx-auto flex max-w-xs items-start justify-center gap-1.5 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-500 dark:text-red-400">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}

      <Button
        type="button"
        onClick={handleOAuth}
        disabled={busyOAuth || signIn == null}
        className="kroma-gradient-btn h-11 w-full text-[15px]"
      >
        {busyOAuth ? (
          <>
            <Loader2 className="animate-spin" aria-hidden />
            Redirecting to Google…
          </>
        ) : authLoaded && isSignedIn ? (
          <>
            Go to app <ArrowRight aria-hidden />
          </>
        ) : (
          <>
            <span className="flex size-6 items-center justify-center rounded-full bg-white">
              <GoogleMark />
            </span>
            Continue with Google
          </>
        )}
      </Button>
      <p className="flex items-start justify-center gap-1 text-xs ink-faint">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        New accounts start as viewer — an admin can upgrade you later.
      </p>
    </div>
  );
}

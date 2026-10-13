"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth, useSignIn, useSignUp } from "@clerk/nextjs";
import { KROMA_ROLES, type KromaRole } from "@/lib/roles";
import { setInitialRole } from "./actions";

type Step = "form" | "verify";

export const PENDING_ROLE_KEY = "kroma-pending-role";
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

type ClerkishError = {
  code?: string;
  longMessage?: string;
  message?: string;
} | null
  | undefined;

/** Human-readable message from a Clerk Future-API `{ error }` result. */
function clerkMessage(err: ClerkishError, fallback: string): string {
  if (!err) return fallback;
  if (typeof err === "object" && "longMessage" in err && err.longMessage)
    return err.longMessage;
  if (typeof err === "object" && "message" in err && err.message)
    return err.message;
  return fallback;
}

function clerkCode(err: ClerkishError): string | undefined {
  return err && typeof err === "object" ? err.code : undefined;
}

/**
 * Custom KromaRAG sign-in form (Clerk Client API — no <SignIn/>).
 *
 * Built against the installed @clerk/nextjs v7 signal-based "Future" API:
 * `useSignIn() → { signIn }`, `useSignUp() → { signUp }`. Every method
 * resolves `{ error }` and mutates the resource — status is read back from
 * `signIn.status` / `signUp.status`, and sessions activate via `finalize()`.
 *
 * Flows:
 * - Existing user: `signIn.create({ identifier, password })` → finalize → `/`.
 * - New user (auto-sign-up): `signUp.create({ emailAddress, password })` →
 *   `verifications.sendEmailCode()` → code step →
 *   `verifications.verifyEmailCode({ code })` → finalize →
 *   `setInitialRole(viewer|editor)` (server action, admin blocked) → `/`.
 * - Google OAuth (Future API): `signIn.sso({ strategy: "oauth_google",
 *   redirectUrl: "/", redirectCallbackUrl: "/login/sso-callback" })` — Clerk
 *   goes straight to `/` on full success; only transfer/MFA/captcha cases land
 *   on /login/sso-callback, which runs the Future `finalize()` state machine.
 *   Role buttons apply to email sign-ups; OAuth role choice is stashed as
 *   `kroma-pending-role` and applied lazily, best-effort by AppShell after
 *   Clerk lands on `/` (default viewer when absent — never blocks session
 *   establishment).
 */
export function CustomLoginForm() {
  const router = useRouter();
  const { signIn } = useSignIn();
  const { signUp } = useSignUp();
  const { isSignedIn, isLoaded: authLoaded } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [role, setRole] = useState<KromaRole>("viewer");
  const [step, setStep] = useState<Step>("form");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyOAuth, setBusyOAuth] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  const ready = signIn != null && signUp != null;

  // Navigate home AND refresh server components so Clerk's session cookie
  // (set by finalize()) is visible to proxy.ts protect() on `/`. Without
  // refresh() the first push can bounce straight back to /login, looking
  // like a double login / dropped session.
  function goHome() {
    router.push("/");
    router.refresh();
  }

  function validateForm(): string | null {
    const em = email.trim().toLowerCase();
    if (!EMAIL_RE.test(em)) return "Enter a valid work email.";
    if (password.length < 8) return "Password must be at least 8 characters.";
    if (role === "admin")
      return "Admin access can't be self-assigned. Choose viewer or editor.";
    return null;
  }

  async function handleContinue(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setInfo("");
    const validation = validateForm();
    if (validation) {
      setError(validation);
      return;
    }
    if (!signIn || !signUp) return;
    setBusy(true);
    try {
      const identifier = email.trim().toLowerCase();

      // 1. Try sign-in for existing users.
      const { error: signInError } = await signIn.create({
        identifier,
        password,
      });
      if (!signInError) {
        if (signIn.status === "complete") {
          const { error: finError } = await signIn.finalize();
          if (finError) {
            setError(clerkMessage(finError, "Could not start your session."));
            return;
          }
          goHome();
          return;
        }
        if (
          signIn.status === "needs_first_factor" ||
          signIn.status === "needs_second_factor"
        ) {
          setError(
            "Additional verification is required for this account — use the emailed link to finish signing in, then return.",
          );
          return;
        }
        setError("Sign-in needs further steps this form can't complete yet.");
        return;
      }
      const signInCode = clerkCode(signInError);
      if (signInCode === "form_password_incorrect") {
        setError("Incorrect password for this email. Try again.");
        return;
      }
      if (
        signInCode !== "form_identifier_not_found" &&
        signInCode !== "form_identifier_invalid"
      ) {
        setError(clerkMessage(signInError, "Sign-in failed. Try again."));
        return;
      }
      // Identifier unknown → fall through to auto-sign-up.

      // 2. Auto-sign-up for new users (role is assigned after verification,
      //    once the session is active — see handleVerify).
      const { error: signUpError } = await signUp.create({
        emailAddress: identifier,
        password,
      });
      if (signUpError) {
        if (clerkCode(signUpError) === "form_identifier_exists") {
          setError(
            "An account with this email already exists — check your password and try again.",
          );
          return;
        }
        setError(clerkMessage(signUpError, "Sign-up failed. Try again."));
        return;
      }
      const { error: codeError } =
        await signUp.verifications.sendEmailCode();
      if (codeError) {
        setError(
          clerkMessage(
            codeError,
            "Account created, but the verification email failed. Try signing in.",
          ),
        );
        return;
      }
      setStep("verify");
      setInfo(`We sent a 6-digit code to ${identifier}.`);
    } finally {
      setBusy(false);
    }
  }

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const clean = code.trim();
    if (clean.length < 6) {
      setError("Enter the 6-digit code from your email.");
      return;
    }
    if (!signUp) return;
    setBusy(true);
    try {
      const { error: verifyError } =
        await signUp.verifications.verifyEmailCode({ code: clean });
      if (verifyError) {
        const errCode = clerkCode(verifyError);
        if (
          errCode === "form_code_incorrect" ||
          errCode === "form_verification_failed"
        ) {
          setError("Incorrect code. Check your email and try again.");
          return;
        }
        if (errCode === "form_code_expired") {
          setError("That code expired — go back and start over.");
          return;
        }
        setError(clerkMessage(verifyError, "Verification failed. Try again."));
        return;
      }
      if (signUp.status !== "complete") {
        setError("Verification needs further steps — check your email.");
        return;
      }
      const { error: finError } = await signUp.finalize();
      if (finError) {
        setError(clerkMessage(finError, "Could not start your session."));
        return;
      }
      // Session is now active → assign the selected role (viewer|editor).
      // Non-fatal: AppShell defaults to viewer if this fails.
      try {
        await setInitialRole(role);
      } catch (roleErr) {
        setInfo(
          roleErr instanceof Error
            ? roleErr.message
            : "Signed in — role assignment needs an admin.",
        );
      }
      goHome();
    } finally {
      setBusy(false);
    }
  }

  async function handleOAuth() {
    setError("");
    if (role === "admin") {
      setError("Admin access can't be self-assigned. Choose viewer or editor.");
      return;
    }
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
      try {
        window.localStorage.setItem(PENDING_ROLE_KEY, role);
      } catch {
        /* stash is best-effort — backend defaults to viewer */
      }
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
        const code = clerkCode(ssoError);
        if (
          code === "already_signed_in" ||
          /already signed in/i.test(clerkMessage(ssoError, ""))
        ) {
          setBusyOAuth(false);
          goHome();
          return;
        }
        setError(clerkMessage(ssoError, "Google sign-in failed. Try again."));
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

  function backToForm() {
    setStep("form");
    setCode("");
    setError("");
    setInfo("");
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="font-semibold tracking-tight text-base">
          Sign in to KromaRAG
        </h2>
        <p className="text-xs ink-soft mt-0.5">
          {step === "verify"
            ? "Check your inbox to finish creating your account"
            : "Use your work email — new addresses create an account automatically"}
        </p>
      </div>

      {step === "form" ? (
        <form onSubmit={handleContinue} className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs font-medium ink-soft" htmlFor="email">
              Work email
            </label>
            <input
              id="email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="surface w-full rounded-lg px-3 py-2 text-sm outline-none focus:border-kroma-500"
              placeholder="you@company.com"
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-medium ink-soft" htmlFor="password">
              Password
            </label>
            <div className="relative">
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                required
                minLength={8}
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="surface w-full rounded-lg px-3 py-2 pr-16 text-sm outline-none focus:border-kroma-500"
                placeholder="••••••••"
              />
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-[11px] font-medium ink-faint hover:opacity-80"
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
          </div>

          <fieldset className="space-y-2">
            <legend className="text-xs font-medium ink-soft">
              Access role for new accounts
            </legend>
            <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="Access role">
              {KROMA_ROLES.map((r) => {
                const selected = role === r;
                const blocked = r === "admin";
                return (
                  <button
                    type="button"
                    key={r}
                    role="radio"
                    aria-checked={selected}
                    title={
                      blocked
                        ? "Admin can't be self-assigned — ask an existing admin"
                        : `Continue as ${r}`
                    }
                    onClick={() => setRole(r)}
                    className={`rounded-lg border px-2 py-2 text-xs font-semibold capitalize transition-all ${selected ? "border-kroma-500 bg-kroma-500/15 text-current" : "ink-soft hover:opacity-80"} ${blocked && !selected ? "opacity-60" : ""}`}
                    style={selected ? {} : { borderColor: "var(--border)" }}
                  >
                    {r}
                  </button>
                );
              })}
            </div>
            <p className="text-[11px] ink-faint">
              New email sign-ups get this role (viewer or editor). Existing
              accounts keep their assigned role. Admin is granted by an
              existing admin — FastAPI enforces this server-side on every
              query. Upload needs editor+, delete needs admin.
            </p>
          </fieldset>

          {error && (
            <p role="alert" className="text-xs text-red-500">
              {error}
            </p>
          )}
          {info && <p className="text-xs ink-soft">{info}</p>}

          <button
            type="submit"
            disabled={busy || !ready}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-kroma-600 px-4 py-2.5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy && <span className="spinner" aria-hidden />}
            {busy ? "Signing in…" : `Continue as ${role}`}
          </button>
        </form>
      ) : (
        <form onSubmit={handleVerify} className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs font-medium ink-soft" htmlFor="code">
              Verification code
            </label>
            <input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="surface w-full rounded-lg px-3 py-2 text-sm tracking-[0.2em] outline-none focus:border-kroma-500"
              placeholder="••••••"
            />
            <p className="text-[11px] ink-faint">
              Creating <span className="font-medium">{email.trim()}</span> as{" "}
              <span className="font-medium capitalize">{role}</span>. Wrong
              address?{" "}
              <button
                type="button"
                onClick={backToForm}
                className="underline hover:opacity-80"
              >
                Go back
              </button>
            </p>
          </div>

          {error && (
            <p role="alert" className="text-xs text-red-500">
              {error}
            </p>
          )}
          {info && <p className="text-xs ink-soft">{info}</p>}

          <button
            type="submit"
            disabled={busy || signUp == null}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-kroma-600 px-4 py-2.5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy && <span className="spinner" aria-hidden />}
            {busy ? "Verifying…" : "Verify & continue"}
          </button>
        </form>
      )}

      {step === "form" && (
        <>
          <div className="flex items-center gap-3" aria-hidden>
            <span
              className="h-px flex-1"
              style={{ background: "var(--border)" }}
            />
            <span className="text-[11px] ink-faint">or</span>
            <span
              className="h-px flex-1"
              style={{ background: "var(--border)" }}
            />
          </div>

          <button
            type="button"
            onClick={handleOAuth}
            disabled={busyOAuth || busy || signIn == null}
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
        </>
      )}
    </div>
  );
}

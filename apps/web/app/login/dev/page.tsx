import { notFound } from "next/navigation";
import { DevBypassForm } from "./dev-bypass-form";

/**
 * Dedicated dev-bypass route (dev builds only).
 *
 * Keeps /login strictly Clerk-only so the two panels never stack. Explicit
 * /login/dev takes precedence over /login/[[...rest]] in the App Router.
 * Covered by the public matcher in proxy.ts (/login(.*)).
 */
export default function DevLoginPage() {
  if (process.env.NODE_ENV === "production") {
    notFound();
  }

  return (
    <div
      className="min-h-screen grid place-items-center px-4 py-10"
      style={{ background: "var(--bg)" }}
    >
      <DevBypassForm />
    </div>
  );
}

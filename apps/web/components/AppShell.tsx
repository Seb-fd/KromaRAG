"use client";

import { useEffect } from "react";
import { useUser } from "@clerk/nextjs";
import { useStore, type Role } from "@/lib/store";
import { rolesFromUser } from "@/lib/kroma-token";
import { isRole } from "@/lib/roles";
import { setInitialRole } from "@/app/login/actions";
import { Sidebar } from "./Sidebar";
import { Chat } from "./Chat";
import { Inspector } from "./Inspector";
import { Toasts } from "./Toasts";

// Must match the stash key in app/login/custom-login-form.tsx (kept as a
// local const to avoid pulling the whole form into the home bundle).
const PENDING_ROLE_KEY = "kroma-pending-role";

/** Enterprise shell: sidebar (history/docs/RBAC) + central chat + source inspector. */
export function AppShell() {
  const { isLoaded, user } = useUser();
  const { sidebarOpen, setSidebarOpen, role, setRole } = useStore();

  // Keep the UI role indicator in sync with the Clerk JWT that authorizes
  // requests (roles live in publicMetadata.roles, default viewer).
  const clerkRoles = rolesFromUser(user);
  const jwtRole = clerkRoles[0] as Role | undefined;
  useEffect(() => {
    if (jwtRole && jwtRole !== role && ["viewer", "editor", "admin"].includes(jwtRole)) {
      setRole(jwtRole);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jwtRole]);

  // Lazy OAuth role sync (proven social-app pattern: post-auth, non-blocking).
  // The login form stashes the viewer|editor choice as `kroma-pending-role`
  // before leaving for Google; Clerk lands back on `/` via
  // <AuthenticateWithRedirectCallback/>. Apply it here best-effort — never
  // blocks render or navigation, never overwrites (server action is a no-op
  // when roles already exist). Absent/invalid stash → viewer default.
  useEffect(() => {
    if (!isLoaded || !user) return;
    let pending: string | null = null;
    try {
      pending = window.localStorage.getItem(PENDING_ROLE_KEY);
    } catch {
      pending = null;
    }
    if (!pending) return;
    try {
      window.localStorage.removeItem(PENDING_ROLE_KEY);
    } catch {
      /* stash is best-effort */
    }
    if (!isRole(pending) || pending === "admin") return;
    (async () => {
      try {
        await setInitialRole(pending);
      } catch {
        /* role stays as assigned — non-fatal, UI defaults to viewer */
      }
    })();
  }, [isLoaded, user]);

  // Mobile: sidebar starts closed
  useEffect(() => {
    if (window.innerWidth < 768) setSidebarOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      {sidebarOpen && (
        <div className="fixed inset-0 z-20 bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <div className={`z-30 shrink-0 ${sidebarOpen ? "fixed inset-y-0 left-0 md:static" : "hidden"}`}>
        <Sidebar />
      </div>
      <main className="min-w-0 flex-1 p-2 sm:p-3">
        <div className="mx-auto flex h-full max-w-4xl flex-col">
          <Chat />
        </div>
      </main>
      <Inspector />
      <Toasts />
    </div>
  );
}

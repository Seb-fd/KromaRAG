"use client";

import { Suspense, lazy, useEffect } from "react";
import { FileText } from "lucide-react";
import { useUser } from "@clerk/nextjs";
import { useStore, type Role } from "@/lib/store";
import { clearDevBypass, rolesFromUser } from "@/lib/kroma-token";
import { Logo } from "./Logo";
import { Sidebar } from "./Sidebar";
import { Chat } from "./Chat";
import { Documents } from "./Documents";
import { Toasts } from "./Toasts";

// Inspector is a rarely-opened drawer: split it out of the initial bundle.
const Inspector = lazy(() => import("./Inspector").then((m) => ({ default: m.Inspector })));

/** Enterprise shell: sidebar (history/RBAC) + central chat/docs + source inspector. */
export function AppShell() {
  const { isLoaded, user } = useUser();
  const { sidebarOpen, setSidebarOpen, role, setRole, mainView } = useStore();

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

  // A real Clerk session takes over: drop any stale dev-bypass flag/cookie
  // so dev tokens can never shadow (or 401 against) the real session.
  useEffect(() => {
    if (user) clearDevBypass();
  }, [user]);

  // Mobile: sidebar starts closed; Esc dismisses the overlay drawer.
  useEffect(() => {
    if (window.innerWidth < 768) setSidebarOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!sidebarOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && window.innerWidth < 768) setSidebarOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sidebarOpen, setSidebarOpen]);

  // Session still hydrating (fresh login lands here via proxy.ts) — show a
  // skeleton instead of flashing an empty/signed-out shell.
  if (!isLoaded) {
    return (
      <div className="flex h-screen items-center justify-center overflow-hidden">
        <div className="anim-fade-up flex flex-col items-center gap-3">
          <Logo size={48} />
          <p className="text-xs ink-soft">Loading your workspace…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <a
        href="#kroma-composer"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-[var(--surface-solid)] focus:px-3 focus:py-1.5 focus:text-sm focus:ring-2 focus:ring-[var(--ring)]"
      >
        Skip to message composer
      </a>
      {sidebarOpen && (
        <div className="fixed inset-0 z-20 bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <div className={`z-30 shrink-0 ${sidebarOpen ? "fixed inset-y-0 left-0 md:static" : "hidden"}`}>
        <Sidebar />
      </div>
      <main className="min-w-0 flex-1 p-2 sm:p-3" aria-label="Main panel">
        <div className="mx-auto flex h-full max-w-4xl flex-col" hidden={mainView !== "chat"}>
          <Chat />
        </div>
        {mainView === "docs" && (
          <div className="slim-scroll mx-auto h-full max-w-2xl overflow-y-auto pb-8">
            <h2 className="mb-3 flex items-center gap-1.5 text-sm font-semibold tracking-tight">
              <FileText className="size-4 text-kroma-500" aria-hidden />
              Documents
            </h2>
            <Documents />
          </div>
        )}
      </main>
      <Suspense fallback={null}>
        <Inspector />
      </Suspense>
      <Toasts />
    </div>
  );
}

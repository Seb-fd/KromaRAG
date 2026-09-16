"use client";

import { useEffect } from "react";
import { useSession } from "next-auth/react";
import { useStore, type Role } from "@/lib/store";
import { Sidebar } from "./Sidebar";
import { Chat } from "./Chat";
import { Inspector } from "./Inspector";
import { Toasts } from "./Toasts";

/** Enterprise shell: sidebar (history/docs/RBAC) + central chat + source inspector. */
export function AppShell() {
  const { data: session } = useSession();
  const { sidebarOpen, setSidebarOpen, role, setRole } = useStore();

  // Keep the UI role indicator in sync with the JWT that actually authorizes requests
  useEffect(() => {
    const jwtRole = session?.user?.roles?.[0] as Role | undefined;
    if (jwtRole && jwtRole !== role && ["viewer", "editor", "admin"].includes(jwtRole)) {
      setRole(jwtRole);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.user?.roles]);

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

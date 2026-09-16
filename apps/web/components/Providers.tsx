"use client";

import { SessionProvider } from "next-auth/react";
import { AppProvider } from "@/lib/store";

/** Client providers: Auth.js session + KromaRAG store (theme, role, convs, toasts). */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider refetchOnWindowFocus={false}>
      <AppProvider>{children}</AppProvider>
    </SessionProvider>
  );
}

"use client";

import { ClerkProvider } from "@clerk/nextjs";
import { AppProvider } from "@/lib/store";

/** Client providers: Clerk session + KromaRAG store (theme, role, convs, toasts). */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ClerkProvider
      signInUrl="/login"
      signUpUrl="/login"
      signInFallbackRedirectUrl="/"
      signUpFallbackRedirectUrl="/"
      afterSignOutUrl="/login"
    >
      <AppProvider>{children}</AppProvider>
    </ClerkProvider>
  );
}

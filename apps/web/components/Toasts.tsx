"use client";

import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

/** Toast notifications: API disconnects, rate limits, stream terminations, RBAC denials. */
export function Toasts() {
  const { toasts, dismissToast } = useStore();
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2" role="region" aria-label="Notifications">
      {toasts.map((t) => (
        <div key={t.id} role={t.kind === "error" ? "alert" : "status"} className={cn(
          "anim-toast glass pointer-events-auto flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[13px]",
          t.kind === "info" && "border-kroma-500/50",
          t.kind === "success" && "border-emerald-500/50",
          t.kind === "error" && "border-red-500/60",
        )} style={{ boxShadow: "var(--shadow)" }}>
          {t.kind === "error" ? (
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-red-500" aria-hidden />
          ) : t.kind === "success" ? (
            <CircleCheck className="mt-0.5 size-4 shrink-0 text-emerald-500" aria-hidden />
          ) : (
            <Info className="mt-0.5 size-4 shrink-0 text-kroma-500" aria-hidden />
          )}
          <p className="flex-1">{t.text}</p>
          <button onClick={() => dismissToast(t.id)} aria-label="Dismiss notification" title="Dismiss" className="rounded p-0.5 ink-faint hover:opacity-70 outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]">
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}

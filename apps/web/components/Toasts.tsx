"use client";

import { useStore } from "@/lib/store";

/** Toast notifications: API disconnects, rate limits, stream terminations, RBAC denials. */
export function Toasts() {
  const { toasts, dismissToast } = useStore();
  const colors = {
    info: "border-kroma-500/50",
    success: "border-emerald-500/50",
    error: "border-red-500/60",
  } as const;
  const dots = { info: "bg-kroma-500", success: "bg-emerald-500", error: "bg-red-500" } as const;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div key={t.id} className={`anim-toast glass pointer-events-auto flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[13px] ${colors[t.kind]}`} style={{ boxShadow: "var(--shadow)" }}>
          <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dots[t.kind]}`} />
          <p className="flex-1">{t.text}</p>
          <button onClick={() => dismissToast(t.id)} className="ink-faint hover:opacity-70" title="Dismiss">✕</button>
        </div>
      ))}
    </div>
  );
}

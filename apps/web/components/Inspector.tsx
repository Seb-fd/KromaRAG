"use client";

import { useEffect, useRef, useState } from "react";
import { useStore } from "@/lib/store";
import { normalizeLegacy, sanitizeTitle } from "@/lib/citations";

/** Right-side Source Drawer: exact chunk metadata, relevance score, cited context. */
export function Inspector() {
  const { inspector, closeInspector, active } = useStore();
  const [copied, setCopied] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<Element | null>(null);

  // Focus the drawer on open, restore focus on close; Esc dismisses.
  useEffect(() => {
    if (!inspector.open) return;
    returnFocusRef.current = document.activeElement;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeInspector();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const el = returnFocusRef.current as HTMLElement | null;
      if (el?.isConnected) el.focus?.();
    };
  }, [inspector.open, closeInspector]);
  if (!inspector.open || !inspector.chunk) return null;
  const c = inspector.chunk;

  // Sentences in the current conversation that cite this chunk.
  // Trusted source: chunk identity comes from SSE `source` metadata (m.chunks);
  // message text is normalized first so legacy `⌗ Title:00.50` still matches.
  // Displayed as readable "Title [ord]" — never raw `[doc:…]` markup.
  const citedIn: string[] = [];
  const want = `[doc:${sanitizeTitle(c.title)}:${c.ord}]`;
  for (const m of active?.messages ?? []) {
    if (m.role !== "assistant") continue;
    const normalized = normalizeLegacy(m.content, m.chunks);
    for (const s of normalized.split(/(?<=[.!?])\s+/)) {
      if (s.includes(want)) citedIn.push(s.replace(/\[doc:([^\[\]:]+):(\d+)\]/g, "$1 [$2]"));
    }
  }

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(`doc_id=${c.doc_id} chunk_id=${c.chunk_id}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* noop */ }
  };

  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true" aria-label="Source inspector">
      <div className="absolute inset-0 bg-black/40" onClick={closeInspector} />
      <aside className="anim-drawer glass absolute right-0 top-0 flex h-full w-full max-w-md flex-col" style={{ background: "var(--surface-solid)" }}>
        <div className="flex items-start gap-2 border-b p-4" style={{ borderColor: "var(--border)" }}>
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wider ink-faint">Source citation</p>
            <h3 className="truncate text-base font-semibold">📄 Source: {c.title} [{c.ord}]</h3>
          </div>
          <button ref={closeRef} onClick={closeInspector} className="ml-auto rounded-lg px-2 py-1 text-lg ink-soft hover:opacity-70" title="Close (Esc)">✕</button>
        </div>
        <div className="slim-scroll flex-1 space-y-4 overflow-y-auto p-4">
          <div>
            <div className="mb-1 flex items-center justify-between text-xs ink-soft">
              <span>Relevance</span><span className="font-mono">{c.score.toFixed(3)}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full" style={{ background: "var(--surface-2)" }}>
              <div className="h-full rounded-full bg-gradient-to-r from-kroma-500 to-kroma-300" style={{ width: `${Math.min(100, Math.round(c.score * 100))}%` }} />
            </div>
          </div>
          <dl className="space-y-1.5 font-mono text-[11.5px]">
            {[["doc_id", c.doc_id], ["chunk_id", c.chunk_id], ["ord", String(c.ord)]].map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="w-20 shrink-0 ink-faint">{k}</dt>
                <dd className="break-all">{v}</dd>
              </div>
            ))}
          </dl>
          <button onClick={copyId} className="rounded-lg border px-2.5 py-1.5 text-xs font-medium ink-soft hover:opacity-80" style={{ borderColor: "var(--border)" }}>
            {copied ? "Copied ✓" : "Copy IDs"}
          </button>
          {c.content && (
            <div>
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider ink-faint">Retrieved chunk</h4>
              <p className="whitespace-pre-wrap rounded-lg p-3 text-[13px] leading-relaxed" style={{ background: "var(--surface-2)" }}>{c.content}</p>
            </div>
          )}
          {citedIn.length > 0 && (
            <div>
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider ink-faint">Cited in this answer</h4>
              <div className="space-y-2">
                {citedIn.map((s, i) => (
                  <p key={i} className="rounded-lg border-l-2 border-kroma-500 p-2 text-[13px] ink-soft" style={{ background: "var(--surface-2)" }}>{s}</p>
                ))}
              </div>
            </div>
          )}
          <p className="text-[11px] ink-faint">RBAC: this chunk was served because your JWT roles satisfy its document policy (specs/01 §4).</p>
        </div>
      </aside>
    </div>
  );
}

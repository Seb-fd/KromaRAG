"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { UserButton, useClerk } from "@clerk/nextjs";
import { clearDevBypass } from "@/lib/kroma-token";
import { Logo } from "./Logo";
import { useStore } from "@/lib/store";
import { getKromaToken, useKromaSession } from "@/lib/kroma-token";

function ago(ts: number): string {
  const m = Math.floor((Date.now() - ts) / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export function Sidebar() {
  const { email, roles } = useKromaSession();
  const { signOut } = useClerk();
  const router = useRouter();
  const {
    theme,
    toggleTheme,
    role,
    conversations,
    activeId,
    newConversation,
    selectConversation,
    deleteThread,
    renameThread,
    syncThreads,
    threadsLoading,
    threadsError,
    apiOnline,
    apiVersion,
    retryApiPing,
    setSidebarOpen,
    mainView,
    setMainView,
  } = useStore();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [chatQuery, setChatQuery] = useState("");

  const visibleConversations = chatQuery.trim()
    ? conversations.filter((c) => c.title.toLowerCase().includes(chatQuery.trim().toLowerCase()))
    : conversations;

  /** Tabs switch the main panel; the sidebar always lists conversations. */
  function switchView(v: "chat" | "docs") {
    setMainView(v);
    if (typeof window !== "undefined" && window.innerWidth < 768) setSidebarOpen(false);
  }

  async function authedJwt(): Promise<string | undefined> {
    return getKromaToken();
  }

  function beginRename(id: string, current: string) {
    setEditingId(id);
    setDraftTitle(current);
  }

  async function commitRename() {
    if (!editingId) return;
    const title = draftTitle.trim();
    if (!title) {
      return;
    }
    const id = editingId;
    setEditingId(null);
    await renameThread(id, title, await authedJwt());
  }

  return (
    <div
      className="glass flex h-full min-h-0 w-80 max-w-[85vw] flex-col md:rounded-none"
      style={{ borderRight: "1px solid var(--border)" }}
    >
      {/* Brand */}
      <div
        className="flex items-center gap-2.5 border-b px-4 py-3"
        style={{ borderColor: "var(--border)" }}
      >
        <Logo size={32} />
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold tracking-tight">
            KromaRAG
          </h1>
          <p className="truncate text-[10.5px] ink-faint">
            Role-Based Vector Architecture
          </p>
        </div>
        <button
          onClick={toggleTheme}
          className="ml-auto rounded-lg px-2 py-1 ink-soft hover:opacity-70"
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
        <button
          onClick={() => setSidebarOpen(false)}
          className="rounded-lg px-2 py-1 ink-soft hover:opacity-70 md:hidden"
          title="Close"
        >
          ✕
        </button>
      </div>

      {/* Tabs: switch the main panel (sidebar always lists conversations) */}
      <div
        className="flex gap-1 border-b px-3 py-2"
        style={{ borderColor: "var(--border)" }}
      >
        {(["chat", "docs"] as const).map((t) => (
          <button
            key={t}
            onClick={() => switchView(t)}
            className={`flex-1 rounded-lg px-2 py-1.5 text-xs font-semibold capitalize transition-all ${mainView === t ? "bg-kroma-500/15 text-current" : "ink-soft hover:opacity-80"}`}
          >
            {t === "chat" ? "💬 Chats" : "📄 Documents"}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="slim-scroll min-h-0 flex-1 overflow-y-auto p-3">
          <div className="space-y-1.5">
            <div className="flex gap-1.5">
              <button
                onClick={() => { newConversation(); setMainView("chat"); }}
                className="flex-1 rounded-xl bg-kroma-600 px-3 py-2 text-[13px] font-semibold text-white transition-opacity hover:opacity-90"
              >
                + New chat
              </button>
              <button
                onClick={() => {
                  void (async () => syncThreads(await authedJwt()))();
                }}
                disabled={threadsLoading}
                className="rounded-xl border px-2.5 text-sm ink-soft transition-opacity hover:opacity-70 disabled:opacity-40"
                style={{ borderColor: "var(--border)" }}
                title="Refresh threads from server"
              >
                {threadsLoading ? "…" : "↻"}
              </button>
            </div>
            {threadsError && (
              <p className="px-1 pt-1 text-[11px] ink-faint">{threadsError}</p>
            )}
            <div className="flex gap-1.5">
              <input
                value={chatQuery}
                onChange={(e) => setChatQuery(e.target.value)}
                placeholder="Search chats…"
                aria-label="Search conversations"
                className="surface min-w-0 flex-1 rounded-lg px-2.5 py-1.5 text-xs outline-none placeholder:text-[var(--ink-faint)] focus:border-kroma-500"
              />
              {chatQuery && (
                <button
                  onClick={() => setChatQuery("")}
                  className="rounded-lg border px-2 text-xs ink-soft transition-opacity hover:opacity-70"
                  style={{ borderColor: "var(--border)" }}
                  title="Clear search"
                >
                  ✕
                </button>
              )}
            </div>
            {threadsLoading && conversations.length === 0 && (
              <div className="space-y-1.5" aria-label="Loading conversations">
                {[0, 1, 2].map((i) => (
                  <div key={i} className="animate-pulse rounded-xl border px-3 py-2" style={{ borderColor: "var(--border)" }}>
                    <div className="h-3 w-3/4 rounded" style={{ background: "var(--surface-2)" }} />
                    <div className="mt-1.5 h-2 w-1/3 rounded" style={{ background: "var(--surface-2)" }} />
                  </div>
                ))}
              </div>
            )}
            {conversations.length === 0 && !threadsLoading && (
              <p className="px-1 pt-2 text-xs ink-faint">
                No conversations yet — start one with + New chat.
              </p>
            )}
            {chatQuery.trim() && visibleConversations.length === 0 && conversations.length > 0 && (
              <p className="px-1 pt-2 text-xs ink-faint">
                No chats match “{chatQuery.trim()}”.
              </p>
            )}
            {visibleConversations.map((c) => (
              <div
                key={c.id}
                onClick={() => {
                  setMainView("chat");
                  void (async () => selectConversation(c.id, await authedJwt()))();
                }}
                className={`group cursor-pointer rounded-xl border px-3 py-2 transition-all ${c.id === activeId ? "border-kroma-500/60 bg-kroma-500/10" : "hover:bg-kroma-500/5"}`}
                style={c.id === activeId ? {} : { borderColor: "transparent" }}
              >
                {editingId === c.id ? (
                  <input
                    autoFocus
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void commitRename();
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    onBlur={() => {
                      void commitRename();
                    }}
                    maxLength={120}
                    className="w-full rounded-md border bg-transparent px-1.5 py-0.5 text-[13px] outline-none"
                    style={{ borderColor: "var(--border)" }}
                  />
                ) : (
                  <p className="truncate text-[13px] font-medium" title={c.title}>
                    {c.title}
                    {c.pending && (
                      <span className="ml-1.5 text-[10px] ink-faint">
                        · local
                      </span>
                    )}
                  </p>
                )}
                <p className="mt-0.5 flex items-center justify-between text-[10.5px] ink-faint"
                  title={new Date(c.createdAt).toLocaleString()}>
                  <span>
                    {c.messageCount ?? c.messages.length} msgs ·{" "}
                    {ago(c.createdAt)}
                    {c.messagesLoading ? " · loading…" : ""}
                    {c.hasMore ? " · more ↑" : ""}
                  </span>
                  <span className="flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        beginRename(c.id, c.title);
                      }}
                      className="rounded px-1 hover:opacity-70"
                      title="Rename"
                    >
                      ✏️
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void (async () => deleteThread(c.id, await authedJwt()))();
                      }}
                      className="rounded px-1 hover:text-red-500"
                      title="Delete"
                    >
                      🗑
                    </button>
                  </span>
                </p>
              </div>
            ))}
          </div>
      </div>

      {/* Access card */}
      <div
        className="space-y-2 border-t p-3"
        style={{ borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-2 text-[11px] ink-soft">
          <span
            className={`h-2 w-2 rounded-full ${apiOnline == null ? "bg-gray-400" : apiOnline ? "bg-emerald-500" : "bg-red-500"}`}
          />
          {apiOnline == null ? (
            "Checking API…"
          ) : apiOnline ? (
            `API online${apiVersion ? ` · v${apiVersion}` : ""}`
          ) : (
            <span className="flex min-w-0 items-center gap-1.5">
              <span
                className="truncate"
                title="The API couldn't be reached from this browser. Common causes: no connection, VPN/firewall, or an adblocker blocking the API host. Whitelist the API host or disable the blocker for this site, then retry."
              >
                API unreachable — check connection or adblocker
              </span>
              <button
                onClick={retryApiPing}
                className="shrink-0 rounded px-1.5 py-0.5 text-xs ink-soft transition-opacity hover:opacity-70"
                style={{ border: "1px solid var(--border)" }}
                title="Retry API health check"
              >
                ↻
              </button>
            </span>
          )}
        </div>
        <div className="surface rounded-xl p-2.5">
          <div className="flex items-center gap-2">
            <UserButton />
            <p
              className="min-w-0 flex-1 truncate text-[11px] font-medium"
              title={email ?? ""}
            >
              {email ?? "Signed in"}
            </p>
          </div>
          <p className="mt-1.5 text-[11px] ink-soft">
            Role: <span className="font-semibold capitalize">{role}</span>
            {roles.length > 1 && (
              <span className="ink-faint"> ({roles.join(", ")})</span>
            )}
          </p>
          <p className="mt-1 text-[10px] ink-faint">
            Roles are managed in the Clerk dashboard — contact an admin to
            change yours. FastAPI enforces them server-side.
          </p>
        </div>
        <button
          onClick={() => {
            // Drop the dev marker too — otherwise the edge gate would let the
            // next visitor straight back in without a Clerk session.
            clearDevBypass();
            // signOut may not navigate when there is no Clerk session (e.g.
            // dev-bypass-only), so force our way back to /login regardless.
            void (async () => {
              try {
                await signOut({ redirectUrl: "/login" });
              } finally {
                router.push("/login");
                router.refresh();
              }
            })();
          }}
          className="w-full rounded-lg border px-2 py-1.5 text-xs ink-soft transition-opacity hover:opacity-70"
          style={{ borderColor: "var(--border)" }}
        >
          Sign out
        </button>
      </div>
    </div>
  );
}

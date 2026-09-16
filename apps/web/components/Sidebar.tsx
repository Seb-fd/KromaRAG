"use client";

import { useState } from "react";
import Image from "next/image";
import { signIn, signOut, useSession } from "next-auth/react";
import { ROLES, useStore, type Role } from "@/lib/store";
import { Documents } from "./Documents";

function ago(ts: number): string {
  const m = Math.floor((Date.now() - ts) / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export function Sidebar() {
  const { data: session } = useSession();
  const {
    theme,
    toggleTheme,
    role,
    setRole,
    conversations,
    activeId,
    newConversation,
    selectConversation,
    deleteThread,
    renameThread,
    syncThreads,
    threadsLoading,
    threadsError,
    toast,
    apiOnline,
    apiVersion,
    setSidebarOpen,
  } = useStore();
  const [tab, setTab] = useState<"chats" | "docs">("chats");
  const [switching, setSwitching] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const jwt = session?.accessToken;

  async function switchRole(r: Role) {
    if (r === role) return;
    if (session?.user?.provider && session.user.provider !== "credentials") {
      toast("SSO-managed role — contact your directory admin", "error");
      return;
    }
    setSwitching(true);
    try {
      const res = await signIn("credentials", {
        email: session?.user?.email ?? "admin@kromarag.com",
        role: r,
        redirect: false,
      });
      if (res?.ok) {
        setRole(r);
        toast(
          `Role switched to ${r} — JWT re-issued, enforcement is server-side`,
          "success",
        );
      } else {
        toast("Role switch failed", "error");
      }
    } finally {
      setSwitching(false);
    }
  }

  function beginRename(id: string, current: string) {
    setEditingId(id);
    setDraftTitle(current);
  }

  async function commitRename() {
    if (!editingId) return;
    const title = draftTitle.trim();
    if (!title) {
      toast("Title must not be blank", "error");
      return;
    }
    const id = editingId;
    setEditingId(null);
    await renameThread(id, title, jwt);
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
        <Image
          alt="KromaRAG Logo"
          className="rounded-lg"
          height={32}
          priority
          src="/logo.webp"
          width={32}
        />
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

      {/* Tabs */}
      <div
        className="flex gap-1 border-b px-3 py-2"
        style={{ borderColor: "var(--border)" }}
      >
        {(["chats", "docs"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 rounded-lg px-2 py-1.5 text-xs font-semibold capitalize transition-all ${tab === t ? "bg-kroma-500/15 text-current" : "ink-soft hover:opacity-80"}`}
          >
            {t === "chats" ? "💬 Chats" : "📄 Documents"}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="slim-scroll min-h-0 flex-1 overflow-y-auto p-3">
        {tab === "chats" ? (
          <div className="space-y-1.5">
            <div className="flex gap-1.5">
              <button
                onClick={newConversation}
                className="flex-1 rounded-xl bg-kroma-600 px-3 py-2 text-[13px] font-semibold text-white transition-opacity hover:opacity-90"
              >
                + New chat
              </button>
              <button
                onClick={() => {
                  void syncThreads(jwt);
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
            {conversations.length === 0 && !threadsLoading && (
              <p className="px-1 pt-2 text-xs ink-faint">
                No conversations yet.
              </p>
            )}
            {conversations.map((c) => (
              <div
                key={c.id}
                onClick={() => {
                  void selectConversation(c.id, jwt);
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
                  <p className="truncate text-[13px] font-medium">
                    {c.title}
                    {c.pending && (
                      <span className="ml-1.5 text-[10px] ink-faint">
                        · local
                      </span>
                    )}
                  </p>
                )}
                <p className="mt-0.5 flex items-center justify-between text-[10.5px] ink-faint">
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
                        void deleteThread(c.id, jwt);
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
        ) : (
          <Documents />
        )}
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
          {apiOnline == null
            ? "Checking API…"
            : apiOnline
              ? `API online${apiVersion ? ` · v${apiVersion}` : ""}`
              : "API offline — start apps/api"}
        </div>
        <div className="surface rounded-xl p-2.5">
          <p
            className="truncate text-[11px] font-medium"
            title={session?.user?.email ?? ""}
          >
            {session?.user?.email}
          </p>
          <div className="mt-1.5 grid grid-cols-3 gap-1">
            {ROLES.map((r) => (
              <button
                key={r}
                disabled={switching}
                onClick={() => switchRole(r)}
                className={`rounded-md border px-1 py-1 text-[11px] font-semibold capitalize transition-all ${role === r ? "border-kroma-500/70 bg-kroma-500/15" : "opacity-60 hover:opacity-100"}`}
                style={{
                  borderColor: role === r ? undefined : "var(--border)",
                }}
                title={
                  role === r ? "Current role" : `Switch to ${r} (re-issues JWT)`
                }
              >
                {r}
              </button>
            ))}
          </div>
          <p className="mt-1 text-[10px] ink-faint">
            Switching re-issues your JWT — FastAPI enforces it.
          </p>
        </div>
        <button
          onClick={() => signOut({ callbackUrl: "/login" })}
          className="w-full rounded-lg border px-2 py-1.5 text-xs ink-soft transition-opacity hover:opacity-70"
          style={{ borderColor: "var(--border)" }}
        >
          Sign out
        </button>
      </div>
    </div>
  );
}

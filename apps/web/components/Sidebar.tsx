"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { UserButton, useClerk } from "@clerk/nextjs";
import {
  FileText,
  Loader2,
  Lock,
  LogOut,
  MessageSquare,
  Moon,
  Pencil,
  Plus,
  RotateCw,
  Search,
  Sun,
  Trash2,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { clearDevBypass } from "@/lib/kroma-token";
import { Logo } from "./Logo";
import { useStore } from "@/lib/store";
import { getKromaToken, useKromaSession } from "@/lib/kroma-token";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Badge } from "./ui/badge";
import { Skeleton } from "./ui/skeleton";
import { cn } from "@/lib/utils";

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
      role="complementary"
      aria-label="Workspace sidebar"
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
          <p className="truncate text-[11px] ink-faint">
            Role-Based Vector Architecture
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={toggleTheme}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          className="ml-auto"
        >
          {theme === "dark" ? <Sun /> : <Moon />}
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setSidebarOpen(false)}
          aria-label="Close sidebar"
          title="Close"
          className="md:hidden"
        >
          <X />
        </Button>
      </div>

      {/* Tabs: switch the main panel (sidebar always lists conversations) */}
      <div
        className="flex gap-1 border-b px-3 py-2"
        style={{ borderColor: "var(--border)" }}
        role="tablist"
        aria-label="Main view"
      >
        {(["chat", "docs"] as const).map((t) => (
          <Button
            key={t}
            variant="ghost"
            size="sm"
            role="tab"
            aria-selected={mainView === t}
            onClick={() => switchView(t)}
            className={cn(
              "flex-1 capitalize",
              mainView === t && "bg-kroma-500/15 text-current hover:bg-kroma-500/20 hover:text-current",
            )}
          >
            {t === "chat" ? <MessageSquare /> : <FileText />}
            {t === "chat" ? "Chats" : "Documents"}
          </Button>
        ))}
      </div>

      {/* Body */}
      <div className="slim-scroll min-h-0 flex-1 overflow-y-auto p-3">
          <div className="space-y-1.5">
            <div className="flex gap-1.5">
              <Button
                onClick={() => { newConversation(); setMainView("chat"); }}
                className="flex-1"
                size="sm"
              >
                <Plus />
                New chat
              </Button>
              <Button
                variant="outline"
                size="icon"
                onClick={() => {
                  void (async () => syncThreads(await authedJwt()))();
                }}
                disabled={threadsLoading}
                aria-label="Refresh threads from server"
                title="Refresh threads from server"
              >
                {threadsLoading ? <Loader2 className="animate-spin" /> : <RotateCw />}
              </Button>
            </div>
            {threadsError && (
              <p className="px-1 pt-1 text-xs ink-faint">{threadsError}</p>
            )}
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--ink-faint)]" aria-hidden />
              <Input
                value={chatQuery}
                onChange={(e) => setChatQuery(e.target.value)}
                placeholder="Search chats…"
                aria-label="Search conversations"
                className="pl-8 pr-8"
              />
              {chatQuery && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setChatQuery("")}
                  aria-label="Clear search"
                  title="Clear search"
                  className="absolute right-1 top-1/2 -translate-y-1/2"
                >
                  <X />
                </Button>
              )}
            </div>
            {threadsLoading && conversations.length === 0 && (
              <div className="space-y-1.5" aria-label="Loading conversations">
                {[0, 1, 2].map((i) => (
                  <div key={i} className="rounded-xl border border-[var(--border)] px-3 py-2">
                    <Skeleton className="h-3 w-3/4" />
                    <Skeleton className="mt-1.5 h-2 w-1/3" />
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
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setMainView("chat");
                    void (async () => selectConversation(c.id, await authedJwt()))();
                  }
                }}
                role="button"
                tabIndex={0}
                aria-current={c.id === activeId}
                className={cn(
                  "group cursor-pointer rounded-xl border px-3 py-2 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
                  c.id === activeId
                    ? "border-kroma-500/60 bg-kroma-500/10"
                    : "border-transparent hover:bg-kroma-500/5",
                )}
              >
                {editingId === c.id ? (
                  <Input
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
                    aria-label="Rename conversation"
                    className="h-7 px-1.5 text-[13px]"
                  />
                ) : (
                  <p className="truncate text-[13px] font-medium" title={c.title}>
                    {c.title}
                    {c.pending && (
                      <Badge variant="muted" className="ml-1.5">
                        local
                      </Badge>
                    )}
                  </p>
                )}
                <p className="tnum mt-0.5 flex items-center justify-between text-xs ink-faint"
                  title={new Date(c.createdAt).toLocaleString()}>
                  <span>
                    {c.messageCount ?? c.messages.length} msgs ·{" "}
                    {ago(c.createdAt)}
                    {c.messagesLoading ? " · loading…" : ""}
                    {c.hasMore ? " · more" : ""}
                  </span>
                  <span className="flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100">
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        beginRename(c.id, c.title);
                      }}
                      aria-label={`Rename "${c.title}"`}
                      title="Rename"
                    >
                      <Pencil />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        void (async () => deleteThread(c.id, await authedJwt()))();
                      }}
                      aria-label={`Delete "${c.title}"`}
                      title="Delete"
                      className="hover:bg-red-500/10 hover:text-red-500"
                    >
                      <Trash2 />
                    </Button>
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
        <div className="flex items-center gap-2 text-xs ink-soft">
          {apiOnline == null ? (
            <>
              <Loader2 className="size-3.5 animate-spin text-[var(--ink-faint)]" aria-hidden />
              Checking API…
            </>
          ) : apiOnline ? (
            <>
              <Wifi className="size-3.5 text-emerald-500" aria-hidden />
              <span className="tnum">
                API online{apiVersion ? ` · v${apiVersion}` : ""}
              </span>
            </>
          ) : (
            <span className="flex min-w-0 items-center gap-1.5">
              <WifiOff className="size-3.5 shrink-0 text-red-500" aria-hidden />
              <span
                className="truncate"
                title="The API couldn't be reached from this browser. Common causes: no connection, VPN/firewall, or an adblocker blocking the API host. Whitelist the API host or disable the blocker for this site, then retry."
              >
                API unreachable — check connection or adblocker
              </span>
              <Button
                variant="outline"
                size="icon-sm"
                onClick={retryApiPing}
                aria-label="Retry API health check"
                title="Retry API health check"
                className="shrink-0"
              >
                <RotateCw />
              </Button>
            </span>
          )}
        </div>
        <div className="surface rounded-xl p-2.5">
          <div className="flex items-center gap-2">
            <UserButton />
            <p
              className="min-w-0 flex-1 truncate text-xs font-medium"
              title={email ?? ""}
            >
              {email ?? "Signed in"}
            </p>
          </div>
          <p className="mt-1.5 flex items-center gap-1.5 text-xs ink-soft">
            Role:{" "}
            <Badge variant="primary" className="capitalize">
              {role}
            </Badge>
            {roles.length > 1 && (
              <span className="ink-faint"> ({roles.join(", ")})</span>
            )}
          </p>
          <p className="mt-1 flex gap-1 text-[11px] leading-snug ink-faint">
            <Lock className="mt-0.5 size-3 shrink-0" aria-hidden />
            Roles are managed in the Clerk dashboard — contact an admin to
            change yours. FastAPI enforces them server-side.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
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
          className="w-full"
        >
          <LogOut />
          Sign out
        </Button>
      </div>
    </div>
  );
}

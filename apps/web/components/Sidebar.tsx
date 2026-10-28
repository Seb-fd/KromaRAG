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
  User,
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
  const { email, roles, user } = useKromaSession();
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
                className={cn(
                  "group cursor-pointer rounded-xl border px-3 py-2 transition-colors",
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
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMainView("chat");
                      void (async () => selectConversation(c.id, await authedJwt()))();
                    }}
                    aria-current={c.id === activeId}
                    title={c.title}
                    className="block w-full truncate text-left text-[13px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] rounded"
                  >
                    {c.title}
                    {c.pending && (
                      <Badge variant="muted" className="ml-1.5">
                        local
                      </Badge>
                    )}
                  </button>
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

      {/* Profile block — centered identity, quiet status */}
      <div
        className="space-y-2.5 border-t border-[var(--border)] p-3"
      >
        <div className="flex items-center justify-center gap-1.5 text-xs ink-faint" role="status">
          {apiOnline == null ? (
            <>
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
              Checking API…
            </>
          ) : apiOnline ? (
            <>
              <span className="size-1.5 rounded-full bg-emerald-500" aria-hidden />
              <span className="tnum">
                API online{apiVersion ? ` · v${apiVersion}` : ""}
              </span>
            </>
          ) : (
            <span className="flex min-w-0 items-center justify-center gap-1.5">
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
        <div className="surface flex flex-col items-center rounded-xl px-3 py-3.5 text-center">
          {user ? (
            <UserButton
              appearance={{
                elements: {
                  avatarBox: {
                    width: "2.25rem",
                    height: "2.25rem",
                    borderRadius: "50%",
                    boxShadow: "0 0 0 3px rgba(109, 92, 255, 0.35)",
                  },
                },
              }}
            />
          ) : (
            <span
              className="avatar-ring flex size-9 items-center justify-center rounded-full bg-kroma-500/15 text-kroma-500 dark:text-kroma-300"
              aria-hidden
            >
              <User className="size-4" />
            </span>
          )}
          <p
            className="mt-2 w-full truncate text-[13px] font-medium"
            title={email ?? ""}
          >
            {email ?? "Signed in"}
          </p>
          <span className="mt-1.5" title={roles.length > 1 ? `All roles: ${roles.join(", ")}` : "Roles live in Clerk publicMetadata and are enforced server-side by FastAPI. Contact an admin to change yours."}>
            <Badge variant="primary" className="capitalize">
              <Lock aria-hidden />
              {role}
            </Badge>
          </span>
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

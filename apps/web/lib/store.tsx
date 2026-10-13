"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { KromaTokenBridge, clerkTemplateMissing, getKromaToken, useKromaSession } from "./kroma-token";
import {
  listConversations as apiListConversations,
  getMessages as apiGetMessages,
  getConversation as apiGetConversation,
  renameConversation as apiRenameConversation,
  deleteConversationThread as apiDeleteThread,
  createConversation as apiCreateConversation,
  ApiError,
  type ThreadMessage,
} from "./api-client";

/* ── Types ── */
export type Role = "viewer" | "editor" | "admin";
export const ROLES: Role[] = ["viewer", "editor", "admin"];

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  chunks?: SourceChunk[];
  usage?: { prompt_tokens: number; completion_tokens: number; model: string };
  model?: string;
  fallbackUsed?: boolean;
};

export type Conversation = {
  id: string;
  title: string;
  createdAt: number;
  messages: ChatMessage[];
  /** Server-reported total (may exceed loaded messages when paginated). */
  messageCount?: number;
  /** Keyset cursor for `GET .../messages` pagination (oldest loaded id). */
  nextBeforeId?: string | null;
  hasMore?: boolean;
  messagesLoading?: boolean;
  /** True for local-only drafts not yet persisted server-side. */
  pending?: boolean;
  updatedAt?: string | null;
};
export type SourceChunk = { chunk_id: string; doc_id: string; title: string; ord: number; score: number; content?: string };
export type Toast = { id: string; kind: "info" | "success" | "error"; text: string };

type Ctx = {
  theme: "dark" | "light";
  toggleTheme: () => void;
  role: Role;
  setRole: (r: Role) => void;
  canUpload: boolean;
  canDelete: boolean;
  conversations: Conversation[];
  activeId: string | null;
  active: Conversation | null;
  threadsLoading: boolean;
  threadsError: string | null;
  /** Sync thread list from server (server wins on conflict). Offline-safe (keeps cache). */
  syncThreads: (jwtOverride?: string) => Promise<void>;
  /** Local-only draft (instant, no network). Becomes a server thread on first query. */
  newConversation: () => void;
  /** Sync alias of newConversation — returns the local draft id for optimistic echo. */
  createConversation: () => string;
  /** Explicit server-side create with offline fallback to a local draft. */
  createThread: (opts?: { title?: string; jwt?: string }) => Promise<string>;
  /** Switch threads + load history from server (read-through cache on failure). */
  selectConversation: (id: string, jwtOverride?: string) => Promise<void>;
  /** Prepend the next older page via keyset cursor. No-op when !hasMore. */
  loadMoreMessages: (convId: string, jwtOverride?: string) => Promise<void>;
  renameThread: (id: string, title: string, jwtOverride?: string) => Promise<void>;
  deleteThread: (id: string, jwtOverride?: string) => Promise<void>;
  /** Back-compat alias for deleteThread (Sidebar). Fire-and-forget safe. */
  deleteConversation: (id: string, jwtOverride?: string) => void;
  /** Adopt the server-minted conv_id after an implicit `POST /query` (done.conv_id). */
  adoptServerThread: (localId: string, serverId: string, title?: string) => void;
  /** Background revalidation of one thread's meta (auto-title updates). Never throws. */
  refreshThreadMeta: (id: string, jwtOverride?: string) => Promise<void>;
  appendMessage: (convId: string, m: ChatMessage) => void;
  updateMessage: (convId: string, id: string, patch: Partial<ChatMessage>) => void;
  toasts: Toast[];
  toast: (text: string, kind?: Toast["kind"]) => void;
  dismissToast: (id: string) => void;
  inspector: { open: boolean; chunk: SourceChunk | null };
  openInspector: (c: SourceChunk) => void;
  closeInspector: () => void;
  apiOnline: boolean | null;
  apiVersion: string;
  sidebarOpen: boolean;
  setSidebarOpen: (b: boolean) => void;
};

const StoreCtx = createContext<Ctx | null>(null);
const uid = () => Math.random().toString(36).slice(2, 10);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** Server thread ids are UUIDv4; anything else is a local-only draft. */
export function isServerThreadId(id: string): boolean {
  return UUID_RE.test(id);
}

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function toChatMessages(items: ThreadMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of items) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    out.push({ id: m.id, role: m.role, content: m.content });
  }
  return out;
}

function metaCreatedAt(s: string | null, fallback: number): number {
  if (!s) return fallback;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : fallback;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const { isSignedIn } = useKromaSession();
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [role, setRoleState] = useState<Role>("viewer");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [threadsLoading, setThreadsLoading] = useState(false);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [inspector, setInspector] = useState<{ open: boolean; chunk: SourceChunk | null }>({ open: false, chunk: null });
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [apiVersion, setApiVersion] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const booted = useRef(false);

  // Clerk template TTL is 60s — never cache the JWT. Every authed call
  // resolves a fresh token per request via getKromaToken() (callers may
  // also pass an explicit jwtOverride).

  // Read mirror for async callbacks (avoid stale closures).
  const convsRef = useRef<Conversation[]>([]);
  useEffect(() => {
    convsRef.current = conversations;
  }, [conversations]);

  // Mount-only hydration of client-persisted keys (localStorage is SSR-unsafe, so this
  // cannot run during render). Acts as the offline/read-through cache: instant paint,
  // server wins on conflict once syncThreads runs.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- hydration, not derived state
    setTheme(load("kromarag.theme", "dark"));
    setRoleState(load("kromarag.role", "viewer"));
    const convs = load<Conversation[]>("kromarag.convs.v1", []);
    const normalized = (Array.isArray(convs) ? convs : []).map((c) => ({
      ...c,
      messages: Array.isArray(c.messages) ? c.messages : [],
      pending: c.pending ?? !isServerThreadId(String(c.id ?? "")),
      hasMore: c.hasMore ?? false,
      nextBeforeId: c.nextBeforeId ?? null,
      messagesLoading: false,
    }));
    setConversations(normalized);
    setActiveId(load("kromarag.active", normalized[0]?.id ?? null));
    booted.current = true;
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    if (booted.current) localStorage.setItem("kromarag.theme", JSON.stringify(theme));
  }, [theme]);

  useEffect(() => {
    if (booted.current) {
      localStorage.setItem("kromarag.convs.v1", JSON.stringify(conversations.slice(0, 50)));
      localStorage.setItem("kromarag.active", JSON.stringify(activeId));
    }
  }, [conversations, activeId]);

  // API health ping (client-side; resilient to offline backend)
  // Proxy-aware: relative /health rides next.config.js rewrites() when
  // NEXT_PUBLIC_USE_PROXY=1 (dev), else direct NEXT_PUBLIC_API_URL fetch.
  useEffect(() => {
    let stop = false;
    const ping = async () => {
      try {
        const base = process.env.NEXT_PUBLIC_USE_PROXY === "1"
          ? ""
          : (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000");
        const r = await fetch(`${base}/health`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
        if (!stop) {
          setApiOnline(r.ok);
          if (r.ok) setApiVersion((await r.json().catch(() => ({})))?.version ?? "");
        }
      } catch {
        if (!stop) setApiOnline(false);
      }
    };
    ping();
    const t = setInterval(ping, 30000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  const toast = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = uid();
    setToasts((ts) => [...ts.slice(-3), { id, kind, text }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 5200);
  }, []);

  /* ── Server-authoritative sync (specs/06 §3.3, FR-02/FR-10) ── */

  const syncThreads = useCallback(async (jwtOverride?: string) => {
    const jwt = jwtOverride ?? (await getKromaToken());
    if (!jwt) return;
    setThreadsLoading(true);
    try {
      const data = await apiListConversations(jwt, 50, 0);
      setThreadsError(null);
      setConversations((prev) => {
        const byId = new Map(prev.map((c) => [c.id, c]));
        const merged: Conversation[] = data.items.map((meta) => {
          const local = byId.get(meta.id);
          if (local) {
            return {
              ...local,
              title: meta.title || local.title,
              createdAt: meta.created_at ? metaCreatedAt(meta.created_at, local.createdAt) : local.createdAt,
              updatedAt: meta.updated_at,
              messageCount: meta.message_count,
              hasMore: local.messages.length === 0 ? meta.message_count > 0 : local.hasMore,
              pending: false,
              messagesLoading: false,
            };
          }
          return {
            id: meta.id,
            title: meta.title || "New conversation",
            createdAt: meta.created_at ? metaCreatedAt(meta.created_at, Date.now()) : Date.now(),
            messages: [],
            messageCount: meta.message_count,
            nextBeforeId: null,
            hasMore: meta.message_count > 0,
            messagesLoading: false,
            pending: false,
            updatedAt: meta.updated_at,
          };
        });
        // Preserve local-only drafts (pending or non-UUID legacy ids). Server wins
        // otherwise: UUID leftovers absent from the server list were deleted elsewhere.
        const pendings = prev.filter((c) => {
          if (merged.some((m) => m.id === c.id)) return false;
          return c.pending || !isServerThreadId(c.id);
        });
        return [...pendings, ...merged].slice(0, 50);
      });
      setActiveId((prevActive) => {
        if (prevActive && data.items.some((m) => m.id === prevActive)) return prevActive;
        if (prevActive) {
          const known = convsRef.current.find((c) => c.id === prevActive);
          if (known && (known.pending || !isServerThreadId(known.id))) return prevActive;
        }
        if (prevActive) return prevActive; // keep stale selection; select will 404-clean
        return data.items[0]?.id ?? prevActive;
      });
    } catch (e) {
      // Offline fallback: keep the local cache, surface a soft error only.
      if (e instanceof ApiError && e.code === "UNAUTHENTICATED") {
        setThreadsError(
          clerkTemplateMissing()
            ? 'Backend rejected token — create Clerk template "kromarag" (aud=kromarag-web) and check API JWT_ISSUER'
            : "Session expired — please sign in again",
        );
      } else if (e instanceof ApiError && e.code === "API_UNREACHABLE") {
        setThreadsError("API offline — showing cached threads");
      } else {
        setThreadsError(e instanceof Error ? e.message.slice(0, 160) : "Thread sync failed");
      }
    } finally {
      setThreadsLoading(false);
    }
  }, []);

  // Auto-sync on sign-in. Server wins on conflict. Token itself is resolved
  // per request inside syncThreads (60s template TTL — never cached).
  useEffect(() => {
    if (!booted.current || !isSignedIn) return;
    void syncThreads();
  }, [isSignedIn, syncThreads]);

  const selectConversation = useCallback(async (id: string, jwtOverride?: string) => {
    setActiveId(id);
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, messagesLoading: true } : c)));
    const jwt = jwtOverride ?? (await getKromaToken());
    const known = convsRef.current.find((c) => c.id === id);
    if (!jwt || (known && (known.pending || !isServerThreadId(id)))) {
      // Local draft or offline: switch instantly, no fetch.
      setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, messagesLoading: false, hasMore: false } : c)));
      return;
    }
    try {
      const data = await apiGetMessages(jwt, id, 50);
      const chats = toChatMessages(data.items);
      setConversations((prev) => prev.map((c) => {
        if (c.id !== id) return c;
        return {
          ...c,
          messages: chats,
          nextBeforeId: data.next_before_id,
          hasMore: data.next_before_id != null,
          messageCount: Math.max(c.messageCount ?? 0, chats.length),
          messagesLoading: false,
          pending: false,
        };
      }));
    } catch (e) {
      setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, messagesLoading: false } : c)));
      if (e instanceof ApiError && e.code === "NOT_FOUND") {
        // Deleted on another device — drop the stale shell (leak-safe 404, FR-08).
        setConversations((prev) => prev.filter((c) => c.id !== id));
        setActiveId((prevActive) => {
          if (prevActive !== id) return prevActive;
          const remaining = convsRef.current.filter((c) => c.id !== id);
          return remaining[0]?.id ?? null;
        });
        toast("Conversation not found — it may have been deleted", "error");
      }
      // Other failures (offline): keep the read-through cache silently.
    }
  }, [toast]);

  const loadMoreMessages = useCallback(async (convId: string, jwtOverride?: string) => {
    const known = convsRef.current.find((c) => c.id === convId);
    if (!known || known.messagesLoading || !known.hasMore || !known.nextBeforeId) return;
    if (known.pending || !isServerThreadId(convId)) return;
    const jwt = jwtOverride ?? (await getKromaToken());
    if (!jwt) return;
    setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, messagesLoading: true } : c)));
    try {
      const cursor = convsRef.current.find((c) => c.id === convId)?.nextBeforeId ?? known.nextBeforeId;
      if (!cursor) {
        setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, messagesLoading: false, hasMore: false } : c)));
        return;
      }
      const data = await apiGetMessages(jwt, convId, 50, cursor);
      const older = toChatMessages(data.items);
      setConversations((prev) => prev.map((c) => {
        if (c.id !== convId) return c;
        const seen = new Set(c.messages.map((m) => m.id));
        const fresh = older.filter((m) => !seen.has(m.id));
        return {
          ...c,
          messages: [...fresh, ...c.messages],
          nextBeforeId: data.next_before_id,
          hasMore: data.next_before_id != null,
          messagesLoading: false,
        };
      }));
    } catch {
      // Offline: keep what we have.
      setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, messagesLoading: false } : c)));
    }
  }, []);

  const renameThread = useCallback(async (id: string, title: string, jwtOverride?: string) => {
    const clean = title.trim().slice(0, 120);
    if (!clean) {
      toast("Title must not be blank", "error");
      return;
    }
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title: clean } : c)));
    const jwt = jwtOverride ?? (await getKromaToken());
    const known = convsRef.current.find((c) => c.id === id);
    if (!jwt || (known && (known.pending || !isServerThreadId(id)))) return; // local-only
    try {
      const meta = await apiRenameConversation(jwt, id, clean);
      setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title: meta.title } : c)));
    } catch (e) {
      if (e instanceof ApiError && e.code === "NOT_FOUND") {
        toast("Conversation not found — it may have been deleted", "error");
      } else if (!(e instanceof ApiError && e.code === "API_UNREACHABLE")) {
        toast(e instanceof Error ? e.message.slice(0, 160) : "Rename failed", "error");
      }
      // Offline: keep the optimistic local title (reconciles on next sync; server wins).
    }
  }, [toast]);

  const deleteThread = useCallback(async (id: string, jwtOverride?: string) => {
    const jwt = jwtOverride ?? (await getKromaToken());
    const known = convsRef.current.find((c) => c.id === id);
    const isLocalOnly = !known || known.pending || !isServerThreadId(id);
    // Optimistic removal for instant UX.
    setConversations((prev) => prev.filter((c) => c.id !== id));
    setActiveId((prevActive) => {
      if (prevActive !== id) return prevActive;
      const remaining = convsRef.current.filter((c) => c.id !== id);
      return remaining[0]?.id ?? null;
    });
    if (!jwt || isLocalOnly) return;
    try {
      await apiDeleteThread(jwt, id);
    } catch (e) {
      if (e instanceof ApiError && e.code === "NOT_FOUND") return; // idempotent (FR-05)
      if (e instanceof ApiError && e.code === "API_UNREACHABLE") {
        toast("Deleted locally (offline) — will reconcile when online", "info");
        return;
      }
      toast(e instanceof Error ? e.message.slice(0, 160) : "Delete failed", "error");
      // Next syncThreads (server wins) restores the thread if the delete never landed.
      void syncThreads(jwt);
    }
  }, [syncThreads, toast]);

  const createThread = useCallback(async (opts?: { title?: string; jwt?: string }) => {
    const jwt = opts?.jwt ?? (await getKromaToken());
    const fallbackDraft = () => {
      const c: Conversation = {
        id: uid(), title: opts?.title?.trim() || "New conversation",
        createdAt: Date.now(), messages: [], pending: true, hasMore: false, nextBeforeId: null,
      };
      setConversations((prev) => [c, ...prev].slice(0, 50));
      setActiveId(c.id);
      return c.id;
    };
    if (!jwt) return fallbackDraft();
    try {
      const meta = await apiCreateConversation({ title: opts?.title, jwt });
      const c: Conversation = {
        id: meta.id, title: meta.title || "New conversation",
        createdAt: meta.created_at ? metaCreatedAt(meta.created_at, Date.now()) : Date.now(),
        messages: [], messageCount: 0, nextBeforeId: null, hasMore: false,
        messagesLoading: false, pending: false, updatedAt: meta.updated_at,
      };
      setConversations((prev) => [c, ...prev.filter((x) => x.id !== c.id)].slice(0, 50));
      setActiveId(c.id);
      return c.id;
    } catch {
      return fallbackDraft();
    }
  }, []);

  const adoptServerThread = useCallback((localId: string, serverId: string, title?: string) => {
    if (!localId || !serverId || localId === serverId) return;
    setConversations((prev) => {
      const local = prev.find((c) => c.id === localId);
      if (!local) return prev;
      const existing = prev.find((c) => c.id === serverId);
      if (existing) {
        const seen = new Set(existing.messages.map((m) => m.id));
        const extra = local.messages.filter((m) => !seen.has(m.id));
        return prev
          .filter((c) => c.id !== localId)
          .map((c) => (c.id === serverId
            ? { ...c, messages: [...c.messages, ...extra], pending: false, title: title ?? c.title }
            : c));
      }
      return prev.map((c) => (c.id === localId
        ? { ...c, id: serverId, pending: false, title: title ?? c.title }
        : c));
    });
    setActiveId((prev) => (prev === localId ? serverId : prev));
  }, []);

  const refreshThreadMeta = useCallback(async (id: string, jwtOverride?: string) => {
    const jwt = jwtOverride ?? (await getKromaToken());
    if (!jwt || !isServerThreadId(id)) return;
    try {
      const meta = await apiGetConversation(jwt, id);
      setConversations((prev) => prev.map((c) => (c.id === id
        ? { ...c, title: meta.title || c.title, messageCount: meta.message_count, updatedAt: meta.updated_at, pending: false }
        : c)));
    } catch {
      // Background revalidation — silent on failure (auto-title arrives on next sync).
    }
  }, []);

  const makeLocalDraft = useCallback((): string => {
    const c: Conversation = { id: uid(), title: "New conversation", createdAt: Date.now(), messages: [], pending: true, hasMore: false, nextBeforeId: null };
    setConversations((prev) => [c, ...prev].slice(0, 50));
    setActiveId(c.id);
    return c.id;
  }, []);

  const deleteConversationAlias = useCallback((id: string, jwtOverride?: string) => {
    void deleteThread(id, jwtOverride);
  }, [deleteThread]);

  const value = useMemo<Ctx>(() => ({
    theme,
    toggleTheme: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    role,
    setRole: (r) => { setRoleState(r); localStorage.setItem("kromarag.role", JSON.stringify(r)); },
    canUpload: role === "editor" || role === "admin",
    canDelete: role === "admin",
    conversations,
    activeId,
    active: conversations.find((c) => c.id === activeId) ?? null,
    threadsLoading,
    threadsError,
    syncThreads,
    newConversation: makeLocalDraft,
    createConversation: makeLocalDraft,
    createThread,
    selectConversation,
    loadMoreMessages,
    renameThread,
    deleteThread,
    deleteConversation: deleteConversationAlias,
    adoptServerThread,
    refreshThreadMeta,
    appendMessage: (convId, m) => setConversations((prev) => prev.map((c) => {
      if (c.id !== convId) return c;
      const title = c.messages.length === 0 && m.role === "user"
        ? m.content.slice(0, 42) + (m.content.length > 42 ? "…" : "") : c.title;
      return { ...c, title, messages: [...c.messages, m] };
    })),
    updateMessage: (convId, id, patch) => setConversations((prev) => prev.map((c) =>
      c.id !== convId ? c : { ...c, messages: c.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) })),
    toasts,
    toast,
    dismissToast: (id) => setToasts((ts) => ts.filter((t) => t.id !== id)),
    inspector,
    openInspector: (chunk) => setInspector({ open: true, chunk }),
    closeInspector: () => setInspector((s) => ({ ...s, open: false })),
    apiOnline,
    apiVersion,
    sidebarOpen,
    setSidebarOpen,
  }), [theme, role, conversations, activeId, threadsLoading, threadsError,
    syncThreads, makeLocalDraft, createThread, selectConversation, loadMoreMessages,
    renameThread, deleteThread, deleteConversationAlias, adoptServerThread,
    refreshThreadMeta, toasts, inspector, apiOnline, apiVersion, sidebarOpen, toast]);

  return (
    <StoreCtx.Provider value={value}>
      <KromaTokenBridge />
      {children}
    </StoreCtx.Provider>
  );
}

export function useStore(): Ctx {
  const ctx = useContext(StoreCtx);
  if (!ctx) throw new Error("useStore outside AppProvider");
  return ctx;
}

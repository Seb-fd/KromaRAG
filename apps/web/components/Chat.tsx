"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { clerkTemplateMissing, getKromaToken, useKromaSession } from "@/lib/kroma-token";
import { streamQuery } from "@/lib/api-client";
import {
  isServerThreadId,
  useStore,
  type ChatMessage,
  type SourceChunk,
} from "@/lib/store";
import { MessageBody } from "./Message";
import { Logo } from "./Logo";

type Phase = "idle" | "searching" | "reranking" | "synthesizing" | "error";
const PHASE_LABEL: Record<Exclude<Phase, "idle" | "error">, string> = {
  searching: "Searching vectors…",
  reranking: "Reranking sources…",
  synthesizing: "Synthesizing answer…",
};
const SUGGESTIONS = [
  "What is KromaRAG and how does hybrid retrieval work?",
  "How is RBAC enforced on every query?",
  "Which models power answers and what happens on rate limits?",
];

const uid = () => Math.random().toString(36).slice(2, 10);

const HINTS: Record<string, string> = {
  API_UNREACHABLE: "API unreachable — check your connection, adblocker, or VPN",
  UNAUTHENTICATED: "Session expired — please sign in again",
  FORBIDDEN: "Forbidden for your role — switch role or ask an admin",
  RATE_LIMITED: "Rate limited — wait a few seconds and retry",
  STREAM_INTERRUPTED: "Stream interrupted — retrying is safe",
  NOT_FOUND: "Conversation not found — it may have been deleted",
};

export function Chat() {
  const { isSignedIn, isLoaded } = useKromaSession();
  const {
    active,
    activeId,
    createConversation,
    appendMessage,
    updateMessage,
    adoptServerThread,
    refreshThreadMeta,
    syncThreads,
    loadMoreMessages,
    toast,
    openInspector,
    setSidebarOpen,
    sidebarOpen,
    apiOnline,
  } = useStore();
  const [input, setInput] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [failedQuery, setFailedQuery] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const streaming =
    phase === "searching" || phase === "reranking" || phase === "synthesizing";
  const messages = useMemo(() => active?.messages ?? [], [active]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, phase]);

  async function send(raw?: string) {
    const text = (raw ?? input).trim();
    if (!text || streaming) return;
    if (apiOnline === false) {
      toast("API offline — check connection or adblocker, then retry", "error");
      return;
    }
    const jwt = await getKromaToken();
    if (!jwt) {
      toast(
        clerkTemplateMissing()
          ? 'Clerk token template "kromarag" missing — create it in Dashboard → Sessions → Customize session token (aud=kromarag-web)'
          : "Auth token unavailable — please sign in again",
        "error",
      );
      return;
    }
    // specs/06 §3.2 + §4.1: transmit conv_id only for server-backed threads.
    // Local drafts (pending / non-UUID) omit conv_id so the server implicitly
    // mints the thread; done.conv_id is then adopted below (FR-01).
    let echoId: string;
    let convToSend: string | undefined;
    let implicitLocalId: string | null = null;
    if (!activeId || !active) {
      echoId = createConversation();
      implicitLocalId = echoId;
    } else if (active.pending || !isServerThreadId(active.id)) {
      echoId = activeId;
      implicitLocalId = activeId;
    } else {
      echoId = activeId;
      convToSend = activeId;
    }
    const userMsg: ChatMessage = { id: uid(), role: "user", content: text };
    const assistantId = uid();
    appendMessage(echoId, userMsg);
    appendMessage(echoId, { id: assistantId, role: "assistant", content: "" });
    setInput("");
    setFailedQuery(null);
    setPhase("searching");
    if (taRef.current) taRef.current.style.height = "auto";

    let acc = "";
    // One honest retry on 401: a fresh login's session can still be
    // propagating when the first request fires. Reuses the same optimistic
    // bubbles so a transient failure leaves no duplicate error trail.
    let authRetried = false;
    let activeJwt = jwt;
    const runStream = async (token: string) => {
      const ctl = new AbortController();
      abortRef.current = ctl;
      return streamQuery({
      query: text,
      jwt: token,
      top_k: 6,
      conv_id: convToSend,
      signal: ctl.signal,
      onSource: (s) => {
        setPhase("reranking");
        updateMessage(echoId, assistantId, {
          chunks: s.chunks as SourceChunk[],
        });
      },
      onToken: (delta) => {
        setPhase("synthesizing");
        acc += delta;
        updateMessage(echoId, assistantId, { content: acc });
      },
      onDone: (meta) => {
        // Update the optimistic bubble first (echoId is always known), then
        // adopt the stable server thread id for implicit creates (FR-01, FR-09).
        updateMessage(echoId, assistantId, {
          usage: meta.usage,
          model: meta.model ?? meta.usage?.model,
          fallbackUsed: meta.fallback_used,
        });
        const serverId = meta.conv_id;
        if (implicitLocalId && serverId && serverId !== implicitLocalId) {
          adoptServerThread(implicitLocalId, serverId);
        } else if (
          serverId &&
          serverId !== echoId &&
          isServerThreadId(serverId)
        ) {
          adoptServerThread(echoId, serverId);
        }
        if (meta.fallback_used)
          toast("Answered via Groq fallback (Gemini was limited)", "info");
        setPhase("idle");
        abortRef.current = null;
        // Background revalidation: server auto-title (§4.3) + list ordering (FR-04).
        if (serverId) {
          void refreshThreadMeta(serverId, activeJwt).catch(() => {});
          void syncThreads(activeJwt).catch(() => {});
        }
      },
      onError: async (err) => {
        if (err.code === "LLM_FALLBACK") {
          // Informational only — the stream continues and onDone toasts the
          // final "Answered via Groq fallback" notice. Toasting here too
          // produces a duplicate notice for a single fallback event.
          return;
        }
        if (err.code === "NOT_FOUND") {
          // Thread deleted elsewhere (leak-safe 404, FR-08): refresh the list so
          // the stale shell disappears; user can start a new chat.
          void syncThreads(activeJwt).catch(() => {});
        }
        if (err.code === "UNAUTHENTICATED" && !authRetried && !clerkTemplateMissing()) {
          // A fresh login's session can still be propagating when the first
          // request fires — re-mint once and resend on the same bubbles.
          authRetried = true;
          toast("Couldn't verify your session — retrying…", "info");
          updateMessage(echoId, assistantId, { content: "" });
          acc = "";
          setPhase("searching");
          const fresh = await getKromaToken();
          if (fresh) {
            activeJwt = fresh;
            await runStream(fresh);
            return;
          }
          // No fresh token — fall through to the final error below.
        }
        setPhase("error");
        setFailedQuery(text);
        const hint =
          err.code === "UNAUTHENTICATED" && clerkTemplateMissing()
            ? 'Backend rejected the fallback token — create Clerk template "kromarag" (aud=kromarag-web) and check API JWT_ISSUER'
            : (HINTS[err.code] ?? err.message.slice(0, 200));
        toast(hint, "error");
        updateMessage(echoId, assistantId, {
          content: acc ? `${acc}\n\n⚠️ ${hint}` : `⚠️ ${hint}`,
        });
        abortRef.current = null;
      },
    });
    };
    await runStream(jwt);
  }

  const lastModel =
    [...messages].reverse().find((m) => m.model)?.model ?? "gemini-3.6-flash";
  const lastFallback = [...messages].reverse().find((m) => m.model)?.fallbackUsed ?? false;
  const offline = apiOnline === false;

  async function handleLoadMore() {
    if (!activeId) return;
    const jwt = await getKromaToken();
    if (!jwt) return;
    await loadMoreMessages(activeId, jwt);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="glass z-10 flex items-center gap-2 rounded-xl px-3 py-2">
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="rounded-lg px-2 py-1 text-lg ink-soft hover:opacity-70"
            title="Open sidebar"
          >
            ☰
          </button>
        )}
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">
            {active?.title ?? "KromaRAG"}
          </h2>
          <p className="text-[11px] ink-faint">
            RBAC-filtered hybrid retrieval · SSE streaming
            {active?.pending ? " · local draft" : ""}
          </p>
        </div>
        <span
          className="ml-auto hidden rounded-full border px-2 py-0.5 font-mono text-[11px] ink-soft sm:inline"
          style={{ borderColor: "var(--border)" }}
          title={lastFallback ? "Primary model was limited; this answer came from the Groq fallback" : `Answering model: ${lastModel}`}
        >
          {lastModel}
          {lastFallback ? " · groq-fallback" : ""}
        </span>
      </div>

      {/* Messages */}
      <div
        ref={scrollRef}
        className="slim-scroll min-h-0 flex-1 space-y-4 overflow-y-auto px-1 py-4"
      >
        {!isLoaded && (
          <p className="text-sm ink-faint">Loading session…</p>
        )}
        {active?.messagesLoading && messages.length > 0 && (
          <p className="text-center text-xs ink-faint">Loading history…</p>
        )}
        {active?.messagesLoading && messages.length === 0 && isSignedIn && (
          <div className="anim-fade-up mx-auto max-w-[92%] space-y-4 py-4 sm:max-w-[85%]" aria-label="Loading messages">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex animate-pulse" style={{ justifyContent: i % 2 ? "flex-end" : "flex-start" }}>
                <div className="w-3/4 space-y-2 rounded-2xl px-4 py-3" style={{ background: "var(--surface-2)" }}>
                  <div className="h-3 w-11/12 rounded" style={{ background: "var(--border)" }} />
                  <div className="h-3 w-2/3 rounded" style={{ background: "var(--border)" }} />
                </div>
              </div>
            ))}
          </div>
        )}
        {active?.hasMore && (
          <div className="text-center">
            <button
              onClick={handleLoadMore}
              disabled={active.messagesLoading}
              className="rounded-lg border px-3 py-1.5 text-xs font-semibold ink-soft transition-opacity hover:opacity-70 disabled:opacity-40"
              style={{ borderColor: "var(--border)" }}
            >
              {active.messagesLoading ? "Loading…" : "↑ Load older messages"}
            </button>
          </div>
        )}
        {messages.length === 0 &&
          isSignedIn &&
          !active?.messagesLoading && (
            <div className="anim-fade-up mx-auto max-w-lg pt-10 text-center">
              <Logo size={48} className="mx-auto mb-4 rounded-xl" />
              <h3 className="text-lg font-semibold tracking-tight">
                Ask across your accessible docs
              </h3>
              <p className="mt-1 text-sm ink-soft">
                Answers cite exact chunks. Click a citation to inspect the
                source.
              </p>
              <div className="mt-4 space-y-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="surface block w-full rounded-xl px-3 py-2 text-left text-[13px] transition-all hover:border-kroma-500/60 hover:bg-kroma-500/5"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
        {messages.map((m) => (
          <div
            key={m.id}
            className="anim-fade-up flex"
            style={{
              justifyContent: m.role === "user" ? "flex-end" : "flex-start",
            }}
          >
            <div
              className={`max-w-[92%] rounded-2xl px-4 py-3 sm:max-w-[85%] ${m.role === "user" ? "text-white" : "surface"}`}
              style={
                m.role === "user"
                  ? {
                      background: "var(--user-bubble)",
                      boxShadow: "var(--shadow)",
                    }
                  : { boxShadow: "var(--shadow)" }
              }
            >
              {m.role === "assistant" ? (
                <>
                  {m.content ? (
                    <MessageBody
                      content={m.content}
                      chunks={m.chunks}
                      onOpenSource={openInspector}
                    />
                  ) : (
                    <span className="cursor-blink text-kroma-500">▍</span>
                  )}
                  {/* Trusted source chips: render ONLY from SSE `source` metadata (m.chunks), never by parsing LLM text. */}
                  {m.chunks && m.chunks.length > 0 && (
                    <div
                      className="mt-2 flex flex-wrap gap-1.5 border-t pt-2"
                      style={{ borderColor: "var(--border)" }}
                    >
                      {[...new Map(m.chunks.map((c) => [c.chunk_id, c])).values()].slice(0, 6).map((c) => (
                        <button
                          key={c.chunk_id}
                          onClick={() => openInspector(c)}
                          className="rounded-md px-1.5 py-0.5 text-[11px] ink-soft transition-colors hover:text-current"
                          style={{ background: "var(--surface-2)" }}
                          title={`${c.title} · score ${c.score.toFixed(2)}`}
                        >
                          <span aria-hidden>📄</span> Source: {c.title} [{c.ord}
                          ]
                        </button>
                      ))}
                    </div>
                  )}
                  {(m.usage || m.model) && (
                    <p className="mt-1.5 font-mono text-[10.5px] ink-faint">
                      {m.model}
                      {m.usage
                        ? ` · ↑${m.usage.prompt_tokens} ↓${m.usage.completion_tokens}`
                        : ""}
                      {m.fallbackUsed ? " · groq-fallback" : ""}
                    </p>
                  )}
                </>
              ) : (
                <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">
                  {m.content}
                </p>
              )}
            </div>
          </div>
        ))}
        {streaming && (
          <div className="flex items-center gap-2 text-xs ink-soft">
            <span className="spinner" />
            <span>{PHASE_LABEL[phase as keyof typeof PHASE_LABEL]}</span>
          </div>
        )}
        {phase === "error" && failedQuery && (
          <button
            onClick={() => send(failedQuery)}
            className="rounded-lg border border-kroma-500/50 px-3 py-1.5 text-xs font-semibold text-kroma-600 dark:text-kroma-300"
          >
            ↻ Retry last query
          </button>
        )}
      </div>

      {/* Composer */}
      <div
        className="glass rounded-2xl p-2.5"
        style={{ boxShadow: "var(--shadow)" }}
      >
        {offline && (
          <p className="mb-1.5 rounded-lg border border-red-500/40 bg-red-500/10 px-2.5 py-1.5 text-[11px] text-red-400" role="alert">
            API offline — answers unavailable. Check connection/adblocker, then retry.
          </p>
        )}
        <div className="flex items-end gap-2">
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            disabled={streaming || offline}
            aria-label="Ask KromaRAG"
            onChange={(e) => {
              setInput(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height =
                Math.min(e.target.scrollHeight, 160) + "px";
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Ask KromaRAG… (Enter to send, Shift+Enter for newline)"
            className="max-h-40 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none placeholder:text-[var(--ink-faint)]"
          />
          {streaming ? (
            <button
              onClick={() => abortRef.current?.abort()}
              title="Stop generating"
              className="rounded-xl bg-red-500/90 px-4 py-2 text-sm font-semibold text-white"
            >
              ■ Stop
            </button>
          ) : (
            <button
              onClick={() => send()}
              disabled={!input.trim() || offline}
              title={offline ? "API offline" : "Send (Enter)"}
              className="rounded-xl bg-kroma-600 px-4 py-2 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              Send ↑
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

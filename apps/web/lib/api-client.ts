/** KromaRAG API client — implements specs/02 §1+§3. fetch + ReadableStream (POST body, not EventSource). */
export type SourceChunk = { chunk_id: string; doc_id: string; title: string; ord: number; score: number; content?: string };
export type DoneMeta = {
  conv_id: string; trace_id: string;
  usage: { prompt_tokens: number; completion_tokens: number; model: string };
  model?: string; fallback_used?: boolean; eval?: Record<string, unknown>;
};
export type StreamError = { code: string; message: string };

/* ── Conversations & History (specs/06 §2.4 + §3.3) ── */
export type ConversationMeta = {
  id: string; title: string;
  created_at: string | null; updated_at: string | null;
  message_count: number;
};
export type ConversationList = { items: ConversationMeta[]; total: number };
export type ThreadMessage = {
  id: string; role: "user" | "assistant" | "system";
  content: string; trace_id: string | null; created_at: string | null;
};
export type MessageList = { items: ThreadMessage[]; next_before_id: string | null };

export type SSEHandlers = {
  onSource?: (chunks: { chunks: SourceChunk[] }) => void;
  onToken?: (delta: string) => void;
  onDone?: (meta: DoneMeta) => void;
  /** NOTE: code LLM_FALLBACK is informational (stream continues); all other codes are terminal. */
  onError?: (err: StreamError & { status?: number }) => void;
};

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function useProxy(): boolean {
  return process.env.NEXT_PUBLIC_USE_PROXY === "1";
}

import { API_BASE_FALLBACK, normalizeApiBaseUrl } from "@/lib/api-base";

export function apiBase(): string {
  // Same-origin rewrite proxy (next.config.js rewrites()): relative URLs hit
  // :3000 and are forwarded to FastAPI, bypassing CORS/CSP/IPv6 issues.
  // Prod (Vercel) keeps the absolute NEXT_PUBLIC_API_URL, normalized so a
  // scheme-less or single-slash value can never misparse as a relative URL
  // (which fetch resolves against window.location.origin -> 404).
  if (process.env.NEXT_PUBLIC_USE_PROXY === "1") return "";
  const raw = process.env.NEXT_PUBLIC_API_URL ?? "";
  if (!raw.trim()) {
    // NEXT_PUBLIC_* inlines at BUILD time: an unset var means the serving
    // bundle predates the Vercel env (or wrong scope) — warn loudly instead
    // of silently hitting localhost and surfacing API_UNREACHABLE.
    if (typeof console !== "undefined") {
      console.warn(
        "[kroma] NEXT_PUBLIC_API_URL unset at build — API calls fall back to localhost " +
          "(rebuild web after setting it in the matching Vercel environment)"
      );
    }
    return API_BASE_FALLBACK;
  }
  return normalizeApiBaseUrl(raw) || API_BASE_FALLBACK;
}

function mapStatus(status: number, fallback: string): string {
  if (status === 401) return "UNAUTHENTICATED";
  if (status === 403) return "FORBIDDEN";
  if (status === 404) return "NOT_FOUND";
  if (status === 422) return "VALIDATION";
  if (status === 429) return "RATE_LIMITED";
  if (status >= 500) return "SERVICE_UNAVAILABLE";
  return fallback;
}

async function authed<T>(path: string, jwt: string | undefined, init: RequestInit, method = "GET"): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${apiBase()}${path}`, {
      ...init,
      method,
      headers: { "Content-Type": "application/json", ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}), ...(init.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "API_UNREACHABLE", "API unreachable — is apps/api running?");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let code = mapStatus(res.status, "API_ERROR");
    try {
      const j = JSON.parse(text);
      if (j?.detail?.code) code = j.detail.code;
      else if (j?.code) code = j.code;
    } catch { /* keep mapped code */ }
    throw new ApiError(res.status, code, code === "FORBIDDEN" ? "Forbidden for your role" : text.slice(0, 300) || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function streamQuery(opts: {
  query: string; jwt?: string; top_k?: number; conv_id?: string | null; signal?: AbortSignal;
} & SSEHandlers): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${apiBase()}/api/v1/query`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(opts.jwt ? { Authorization: `Bearer ${opts.jwt}` } : {}),
      },
      // specs/06 §3.2: transmit conv_id when an active thread exists; omit
      // otherwise so the server implicitly mints a thread (FR-01).
      body: JSON.stringify({
        query: opts.query,
        top_k: opts.top_k ?? 6,
        stream: true,
        ...(opts.conv_id ? { conv_id: opts.conv_id } : {}),
      }),
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return;
    opts.onError?.({ code: "API_UNREACHABLE", message: "API unreachable — is apps/api running?" });
    return;
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    opts.onError?.({ code: mapStatus(res.status, "API_ERROR"), message: text.slice(0, 300), status: res.status });
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  const dispatch = (raw: string) => {
    const m = raw.match(/event:\s*(\w+)\ndata:\s*([\s\S]*)/);
    if (!m) return;
    const [, ev, data] = m;
    try {
      const json = JSON.parse(data.trim());
      if (ev === "source") opts.onSource?.(json);
      else if (ev === "token") opts.onToken?.(json.delta ?? "");
      else if (ev === "done") opts.onDone?.(json);
      else if (ev === "error") opts.onError?.(json);
    } catch (e) {
      opts.onError?.({ code: "STREAM_PARSE", message: String(e) });
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.replace(/\r\n/g, "\n").split("\n\n");
      buf = frames.pop() ?? "";
      for (const f of frames) {
        if (f.trim() && !f.trim().startsWith(":")) dispatch(f);
      }
    }
    if (buf.trim() && !buf.trim().startsWith(":")) dispatch(buf);
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return;
    opts.onError?.({ code: "STREAM_INTERRUPTED", message: "Stream interrupted — retrying is safe" });
  }
}

/* ── Documents (specs/02 §1 + specs/07 §2.4/§3.3) ── */
export type DocStatus = "pending" | "indexing" | "indexed" | "failed" | "archived";

/** Additive extension of specs/02 DocMeta (specs/07 §2.4). Old clients ignore `version`. */
export type DocumentMeta = {
  id: string; title: string; mime: string; status: string; chunks: number;
  created_at: string | null; version: number;
};

/** Full lifecycle view — mirrors backend `DocumentDetail` Pydantic shape. */
export type DocumentDetail = {
  id: string; title: string; mime: string; status: DocStatus;
  chunks: number; version: number;
  owner_sub: string; uri: string | null;
  allowed_roles: string[];
  chunk_config: { chunk_tokens: number; chunk_overlap: number };
  embedding_model: string; embedding_dim: number;
  error: string | null;
  created_at: string | null; updated_at: string | null; archived_at: string | null;
};

export type DocumentList = { items: DocumentDetail[]; total: number };
export type ReindexResponse = { doc_id: string; job_id: string; status: "indexing"; version: number };
export type DocumentStatus = {
  doc_id: string; status: DocStatus; chunks: number; version: number;
  error: string | null; updated_at: string | null;
};
export type UploadJob = {
  id: string; label: string;
  state: "reading" | "uploading" | "indexing" | "done" | "error";
  detail: string; doc_id: string | null;
};
export type UploadResult = { doc_id: string; status: DocStatus; chunks: number; job_id: string };

/** Back-compat alias — pre-07 code used `DocItem`; it is now the full `DocumentDetail`. */
export type DocItem = DocumentDetail;

export type DocumentFilters = { status?: DocStatus | "all"; mime?: string; q?: string };
export type DocumentPatch = { title?: string; allowed_roles?: string[]; owner_sub?: string; uri?: string };
export type ReindexBody = { chunk_tokens?: number; chunk_overlap?: number; embedding_model?: string };

/* ── Conversations (specs/06 §3.3) ── */

export function createConversation(args: { title?: string; jwt?: string }): Promise<ConversationMeta> {
  const body = args.title?.trim() ? { title: args.title.trim().slice(0, 120) } : {};
  return authed<ConversationMeta>("/api/v1/conversations", args.jwt, { body: JSON.stringify(body) }, "POST");
}

export function listConversations(
  jwt?: string, limit = 20, offset = 0,
): Promise<ConversationList> {
  return authed<ConversationList>(
    `/api/v1/conversations?limit=${encodeURIComponent(String(limit))}&offset=${encodeURIComponent(String(offset))}`,
    jwt, {},
  );
}

export function getConversation(jwt: string | undefined, id: string): Promise<ConversationMeta> {
  return authed<ConversationMeta>(`/api/v1/conversations/${encodeURIComponent(id)}`, jwt, {});
}

export function renameConversation(
  jwt: string | undefined, id: string, title: string,
): Promise<ConversationMeta> {
  return authed<ConversationMeta>(
    `/api/v1/conversations/${encodeURIComponent(id)}`,
    jwt, { body: JSON.stringify({ title }) }, "PATCH",
  );
}

export function deleteConversationThread(jwt: string | undefined, id: string): Promise<void> {
  return authed<void>(`/api/v1/conversations/${encodeURIComponent(id)}`, jwt, {}, "DELETE");
}

/** Alias for the §3.3 `deleteConversationThread` (task wording uses `deleteConversation`). */
export const deleteConversation = deleteConversationThread;

export function getMessages(
  jwt: string | undefined, convId: string, limit = 50, before_id?: string,
): Promise<MessageList> {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (before_id) qs.set("before_id", before_id);
  return authed<MessageList>(
    `/api/v1/conversations/${encodeURIComponent(convId)}/messages?${qs.toString()}`, jwt, {},
  );
}

export function ingestDocument(args: {
  title: string; content?: string; url?: string; mime?: string; allowed_roles?: string[]; jwt?: string;
}): Promise<{ doc_id: string; status: string; chunks: number }> {
  return authed("/api/v1/ingest", args.jwt, {
    body: JSON.stringify({
      title: args.title, content: args.content, url: args.url,
      mime: args.mime ?? "text/plain", allowed_roles: args.allowed_roles ?? ["viewer"],
    }),
  }, "POST");
}

/* ── Document lifecycle (specs/07 §3.3) ── */

function parseApiError(status: number, text: string): ApiError {
  let code = mapStatus(status, "API_ERROR");
  try {
    const j = JSON.parse(text);
    if (j?.detail?.code) code = j.detail.code;
    else if (j?.code) code = j.code;
  } catch { /* keep mapped code */ }
  return new ApiError(status, code, text.slice(0, 300) || `Request failed (${status})`);
}

/**
 * Multipart binary upload (specs/07 FR-01). Uses XHR when `onProgress` is
 * supplied so the UI gets real upload percentages; fetch otherwise.
 * Never sets Content-Type manually — the browser supplies the boundary.
 */
export function uploadDocument(args: {
  file: File; title?: string; allowed_roles?: string[];
  chunk_tokens?: number; chunk_overlap?: number; jwt?: string;
  onProgress?: (pct: number) => void;
}): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", args.file, args.file.name);
  if (args.title?.trim()) form.append("title", args.title.trim().slice(0, 300));
  form.append("allowed_roles", JSON.stringify(args.allowed_roles ?? ["viewer"]));
  if (args.chunk_tokens != null) form.append("chunk_tokens", String(args.chunk_tokens));
  if (args.chunk_overlap != null) form.append("chunk_overlap", String(args.chunk_overlap));

  const url = `${apiBase()}/api/v1/documents/upload`;
  const headers: Record<string, string> = args.jwt ? { Authorization: `Bearer ${args.jwt}` } : {};

  if (args.onProgress && typeof XMLHttpRequest !== "undefined") {
    return new Promise<UploadResult>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) {
          args.onProgress?.(Math.min(99, Math.round((e.loaded / e.total) * 100)));
        }
      };
      xhr.onload = () => {
        const text = xhr.responseText ?? "";
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(text) as UploadResult);
          } catch {
            reject(new ApiError(xhr.status, "STREAM_PARSE", "Upload succeeded with unreadable response"));
          }
        } else {
          reject(parseApiError(xhr.status, text));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "API_UNREACHABLE", "API unreachable — is apps/api running?"));
      xhr.onabort = () => reject(new ApiError(0, "API_UNREACHABLE", "Upload aborted"));
      xhr.send(form);
    });
  }

  return (async () => {
    let res: Response;
    try {
      res = await fetch(url, { method: "POST", headers, body: form });
    } catch {
      throw new ApiError(0, "API_UNREACHABLE", "API unreachable — is apps/api running?");
    }
    if (!res.ok) throw parseApiError(res.status, await res.text().catch(() => ""));
    return (await res.json()) as UploadResult;
  })();
}

export function listDocuments(
  jwt?: string, limit = 20, offset = 0,
  filters?: DocumentFilters,
): Promise<DocumentList> {
  const qs = new URLSearchParams({
    limit: String(Math.max(1, Math.min(limit, 100))),
    offset: String(Math.max(0, offset)),
  });
  if (filters?.status) qs.set("status", filters.status);
  if (filters?.mime) qs.set("mime", filters.mime);
  if (filters?.q?.trim()) qs.set("q", filters.q.trim());
  return authed<DocumentList>(`/api/v1/documents?${qs.toString()}`, jwt, {});
}

export function getDocument(jwt: string | undefined, id: string): Promise<DocumentDetail> {
  return authed<DocumentDetail>(`/api/v1/documents/${encodeURIComponent(id)}`, jwt, {});
}

export function getDocumentStatus(jwt: string | undefined, id: string): Promise<DocumentStatus> {
  return authed<DocumentStatus>(`/api/v1/documents/${encodeURIComponent(id)}/status`, jwt, {});
}

export function patchDocument(
  jwt: string | undefined, id: string, body: DocumentPatch,
): Promise<DocumentDetail> {
  return authed<DocumentDetail>(
    `/api/v1/documents/${encodeURIComponent(id)}`, jwt,
    { body: JSON.stringify(body) }, "PATCH",
  );
}

/** Alias for the task wording (`updateDocumentMetadata`); canonical name is `patchDocument`. */
export const updateDocumentMetadata = patchDocument;

/** Soft-archive (specs/07 §4.5): DELETE → status='archived', chunks retained. */
export function archiveDocument(jwt: string | undefined, id: string): Promise<void> {
  return authed<void>(`/api/v1/documents/${encodeURIComponent(id)}`, jwt, {}, "DELETE");
}

/** Alias for the task wording (`softDeleteDocument`); canonical name is `archiveDocument`. */
export const softDeleteDocument = archiveDocument;

/**
 * `deleteDocument` is kept as an alias of soft-archive. Hard purge MUST call
 * `purgeDocument` explicitly (specs/07 §3.3 — no `DELETE?hard=true` variant).
 */
export const deleteDocument = archiveDocument;

/** Hard-purge (irreversible CASCADE). 409 while pending/indexing, 404 when gone. */
export function purgeDocument(jwt: string | undefined, id: string): Promise<void> {
  return authed<void>(`/api/v1/documents/${encodeURIComponent(id)}/purge`, jwt, {}, "POST");
}

/** Async re-index: reserves version+1 synchronously, embeds in background (202). */
export function reindexDocument(
  jwt: string | undefined, id: string, body?: ReindexBody,
): Promise<ReindexResponse> {
  return authed<ReindexResponse>(
    `/api/v1/documents/${encodeURIComponent(id)}/reindex`, jwt,
    { body: JSON.stringify(body ?? {}) }, "POST",
  );
}

export const SUPPORTED_MIMES = ["text/plain", "text/markdown", "application/pdf"];
/**
 * Client-side upload guardrail (specs/07 §4.1 rule 6). The server cap
 * `DOC_UPLOAD_MAX_BYTES` (20 MB) is authoritative; the web pre-guard matches it
 * so PDF/MD/TXT binaries fail fast with a friendly message instead of a 413.
 */
export const MAX_UPLOAD_BYTES = 20_971_520;

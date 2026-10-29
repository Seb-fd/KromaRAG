"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Archive,
  CheckCircle2,
  FileCode2,
  FileText,
  FileType2,
  Loader2,
  Lock,
  RefreshCw,
  Trash2,
  UploadCloud,
  XCircle,
} from "lucide-react";
import { getKromaToken, useKromaSession } from "@/lib/kroma-token";
import {
  ApiError,
  MAX_UPLOAD_BYTES, SUPPORTED_MIMES, archiveDocument, getDocumentStatus, ingestDocument,
  listDocuments, purgeDocument, reindexDocument, uploadDocument,
  type DocStatus, type DocumentDetail, type UploadJob,
} from "@/lib/api-client";
import { useStore } from "@/lib/store";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Badge } from "./ui/badge";
import { Skeleton } from "./ui/skeleton";

const TERMINAL_STATUSES: DocStatus[] = ["indexed", "failed", "archived"];
const STATUS_FILTERS: (DocStatus | "all")[] = ["all", "indexed", "indexing", "pending", "failed", "archived"];
const POLL_INTERVAL_MS = 1500;
const MAX_POLLS = 40;

function mimeForFile(f: File): string {
  const name = f.name.toLowerCase();
  if (f.type === "application/pdf" || name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".md") || name.endsWith(".markdown") || f.type === "text/markdown") return "text/markdown";
  return "text/plain";
}

function extAllowed(name: string): boolean {
  const n = name.toLowerCase();
  return n.endsWith(".txt") || n.endsWith(".md") || n.endsWith(".markdown") || n.endsWith(".pdf");
}

/** UI gating only (server is authoritative): Clerk user id == backend `sub`. */

function friendlyDocError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case "PAYLOAD_TOO_LARGE":
        return `File exceeds ${(MAX_UPLOAD_BYTES / 1_048_576).toFixed(0)} MB cap`;
      case "REINDEX_IN_PROGRESS":
        return "Reindex already in progress for this doc";
      case "ARCHIVE_CONFLICT":
        return "Busy indexing — retry after terminal state";
      case "NOT_FOUND":
        return "Not found — deleted or never shared with you";
      case "FORBIDDEN":
        return "Forbidden for your role";
      case "EMBEDDING_MISMATCH":
      case "VALIDATION":
        return e.message.slice(0, 140) || "Validation failed";
      case "API_UNREACHABLE":
        return "API unreachable — is apps/api running?";
      default:
        return e.message.slice(0, 140) || `Request failed (${e.status})`;
    }
  }
  return (e as Error).message?.slice(0, 140) ?? "Upload failed";
}

/** Local UI-only job id (module scope: keeps render pure per react-hooks/purity). */
const newLocalId = () => Math.random().toString(36).slice(2, 9);

/** Mime glyph for a library row: pdf → doc, markdown → code, else generic. */
function DocMimeIcon({ mime }: { mime: string }) {
  const m = mime.toLowerCase();
  if (m.includes("pdf")) return <FileType2 className="size-4" aria-hidden />;
  if (m.includes("markdown") || m.includes("text")) return <FileCode2 className="size-4" aria-hidden />;
  return <FileText className="size-4" aria-hidden />;
}

function StatusBadge({ status }: { status: DocStatus }) {  const busy = status === "pending" || status === "indexing";
  const variant = status === "indexed" ? "success" : status === "failed" ? "danger" : status === "archived" ? "muted" : "warning";
  return (
    <Badge variant={variant} className="shrink-0 font-mono uppercase">
      {busy && <Loader2 className="animate-spin" aria-hidden />}
      {status}
    </Badge>
  );
}

/** Document lifecycle (specs/07 §3.3): binary upload + status badges + gated actions. */
export function Documents() {
  const { user } = useKromaSession();
  const { toast, canUpload, role } = useStore();
  const [jobs, setJobs] = useState<UploadJob[]>([]);
  const [docs, setDocs] = useState<DocumentDetail[]>([]);
  const [total, setTotal] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [url, setUrl] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  const [shareRoles, setShareRoles] = useState<string[]>(["viewer"]);
  const [statusFilter, setStatusFilter] = useState<DocStatus | "all">("all");
  const [qDraft, setQDraft] = useState("");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [confirmPurgeId, setConfirmPurgeId] = useState<string | null>(null);
  const [listLoaded, setListLoaded] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const pollingRef = useRef<Set<string>>(new Set());

  const mySub = user?.id ?? null;
  const isAdmin = role === "admin";

  useEffect(() => {
    mountedRef.current = true;
    const polling = pollingRef.current;
    return () => { mountedRef.current = false; polling.clear(); };
  }, []);

  const patchJob = (id: string, p: Partial<UploadJob>) =>
    setJobs((js) => js.map((j) => (j.id === id ? { ...j, ...p } : j)));

  const markBusy = (id: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });

  const refresh = useCallback(async () => {
    const jwt = await getKromaToken();
    if (!jwt) {
      if (mountedRef.current) setListLoaded(true);
      return;
    }
    try {
      const r = await listDocuments(jwt, 30, 0, {
        ...(statusFilter !== "all" ? { status: statusFilter } : { status: "all" as const }),
        ...(q.trim() ? { q: q.trim() } : {}),
      });
      if (!mountedRef.current) return;
      // Leak-safe: server already RBAC-filters; a 404 here just means nothing visible.
      setDocs(r.items);
      setTotal(r.total);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.code === "NOT_FOUND")) {
        if (mountedRef.current) { setDocs([]); setTotal(0); }
      }
      /* offline backend — list stays empty, upload errors surface per-job */
    }
    if (mountedRef.current) setListLoaded(true);
  }, [statusFilter, q]);

  // Fetch-on-mount + refetch when filters change. Server list is authoritative.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh() only setStates after the async fetch resolves
  useEffect(() => { refresh(); }, [refresh]);

  // While any visible doc is in-flight, revalidate the list so badges flip without manual refresh.
  const hasInflight = docs.some((d) => d.status === "pending" || d.status === "indexing");
  useEffect(() => {
    if (!hasInflight) return;
    const t = setInterval(() => { refresh(); }, 3000);
    return () => clearInterval(t);
  }, [hasInflight, refresh]);

  const pollUntilTerminal = useCallback(async (
    docId: string, jobId: string | null, jwtAtCall: string | undefined,
  ) => {
    if (!jwtAtCall || pollingRef.current.has(docId)) return;
    pollingRef.current.add(docId);
    try {
      for (let i = 0; i < MAX_POLLS; i++) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        if (!mountedRef.current) return;
        let s;
        try {
          s = await getDocumentStatus(jwtAtCall, docId);
        } catch (e) {
          if (e instanceof ApiError && (e.status === 404 || e.code === "NOT_FOUND")) {
            if (jobId) patchJob(jobId, { state: "error", detail: "Not found — deleted or unshared" });
            if (mountedRef.current) setDocs((prev) => prev.filter((d) => d.id !== docId));
            return;
          }
          continue; // transient — keep polling
        }
        if (jobId) {
          patchJob(jobId, {
            state: "indexing",
            detail: `${s.status} · v${s.version} · ${s.chunks} chunks`,
            doc_id: docId,
          });
        }
        if ((TERMINAL_STATUSES as string[]).includes(s.status)) {
          if (jobId) {
            if (s.status === "indexed") {
              patchJob(jobId, { state: "done", detail: `${s.chunks} chunks · v${s.version} · indexed` });
            } else if (s.status === "archived") {
              patchJob(jobId, { state: "done", detail: `archived · v${s.version}` });
            } else {
              patchJob(jobId, { state: "error", detail: (s.error ?? "indexing failed").slice(0, 140) });
            }
          }
          await refresh();
          return;
        }
      }
      if (jobId) patchJob(jobId, { state: "indexing", detail: "Still indexing — list refreshes automatically" });
      await refresh();
    } finally {
      pollingRef.current.delete(docId);
    }
  }, [refresh]);

  async function uploadOne(title: string, content: string | undefined, docUrl: string | undefined, mime: string) {
    const jwtAtCall = await getKromaToken();
    if (!jwtAtCall) {
      toast("Sign in to ingest documents", "error");
      return;
    }
    const id = newLocalId();
    setJobs((js) => [{ id, label: title, state: "uploading" as const, detail: "Embedding & indexing…", doc_id: null }, ...js].slice(0, 5));
    try {
      const r = await ingestDocument({ title, content, url: docUrl, mime, allowed_roles: shareRoles, jwt: jwtAtCall });
      if (r.status === "pending" || r.status === "indexing") {
        patchJob(id, { state: "indexing", detail: `${r.status}…`, doc_id: r.doc_id });
        toast(`Ingesting “${title}” — polling status…`, "info");
        await pollUntilTerminal(r.doc_id, id, jwtAtCall);
      } else {
        patchJob(id, { state: "done", detail: `${r.chunks} chunks · ${r.status}`, doc_id: r.doc_id });
        toast(`Ingested “${title}” (${r.chunks} chunks)`, "success");
      }
      refresh();
    } catch (e) {
      const msg = friendlyDocError(e);
      patchJob(id, { state: "error", detail: msg });
      toast(`Upload failed: ${msg}`, "error");
    }
  }

  async function handleFiles(files: FileList | File[]) {
    if (!canUpload) {
      toast("Ingestion needs the editor role — ask an admin to grant it in Clerk", "error");
      return;
    }
    const jwtAtCall = await getKromaToken();
    if (!jwtAtCall) {
      toast("Sign in to ingest documents", "error");
      return;
    }
    for (const f of Array.from(files)) {
      if (!extAllowed(f.name)) {
        toast(`Unsupported type: ${f.name} (.txt / .md / .pdf only)`, "error");
        continue;
      }
      const mime = mimeForFile(f);
      if (!SUPPORTED_MIMES.includes(mime)) {
        toast(`Unsupported type: ${f.name}`, "error");
        continue;
      }
      if (f.size > MAX_UPLOAD_BYTES) {
        const id = newLocalId();
        setJobs((js) => [{ id, label: f.name, state: "error" as const, detail: `File exceeds ${(MAX_UPLOAD_BYTES / 1_048_576).toFixed(0)} MB cap`, doc_id: null }, ...js].slice(0, 5));
        toast(`Upload failed: file exceeds ${(MAX_UPLOAD_BYTES / 1_048_576).toFixed(0)} MB cap`, "error");
        continue;
      }
      if (f.size === 0) {
        toast(`Empty file: ${f.name}`, "error");
        continue;
      }
      const id = newLocalId();
      setJobs((js) => [{ id, label: f.name, state: "uploading" as const, detail: "0%", doc_id: null }, ...js].slice(0, 5));
      try {
        const r = await uploadDocument({
          file: f,
          title: f.name.replace(/\.[^.]+$/, ""),
          allowed_roles: shareRoles,
          jwt: jwtAtCall,
          onProgress: (pct) => patchJob(id, { detail: `${pct}%` }),
        });
        if (r.status === "indexed") {
          patchJob(id, { state: "done", detail: `${r.chunks} chunks · v1 · indexed`, doc_id: r.doc_id });
          toast(`Ingested “${f.name}” (${r.chunks} chunks)`, "success");
          refresh();
        } else {
          patchJob(id, { state: "indexing", detail: `${r.status}…`, doc_id: r.doc_id });
          toast(`Upload accepted — indexing “${f.name}”…`, "info");
          await pollUntilTerminal(r.doc_id, id, jwtAtCall);
        }
      } catch (e) {
        const msg = friendlyDocError(e);
        patchJob(id, { state: "error", detail: msg });
        toast(`Upload failed: ${msg}`, "error");
      }
    }
  }

  async function submitUrl(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    if (!canUpload) {
      toast("Ingestion needs the editor role — ask an admin to grant it in Clerk", "error");
      return;
    }
    await uploadOne(urlTitle.trim() || url.trim(), undefined, url.trim(), "text/plain");
    setUrl("");
    setUrlTitle("");
  }

  /** (owner_sub == me && editor+) || admin. Empty owner falls back to role gates. */
  function canManage(d: DocumentDetail): boolean {
    if (!mySub) return false;
    if (isAdmin) return true;
    if (!d.owner_sub) return canUpload;
    return d.owner_sub === mySub && canUpload;
  }

  /** Purge needs ownership only (any role) or admin. */
  function canPurge(d: DocumentDetail): boolean {
    if (!mySub) return false;
    if (isAdmin) return true;
    if (!d.owner_sub) return canUpload;
    return d.owner_sub === mySub;
  }

  async function handleReindex(d: DocumentDetail) {
    const jwtAtCall = await getKromaToken();
    if (!jwtAtCall) { toast("Sign in first", "error"); return; }
    const jobId = newLocalId();
    markBusy(d.id, true);
    try {
      const r = await reindexDocument(jwtAtCall, d.id, {});
      setJobs((js) => [{ id: jobId, label: `Reindex ${d.title}`, state: "indexing" as const, detail: `v${r.version} indexing…`, doc_id: d.id }, ...js].slice(0, 5));
      toast(`Reindex started (v${r.version}) — polling status…`, "info");
      await pollUntilTerminal(d.id, jobId, jwtAtCall);
    } catch (e) {
      toast(`Reindex failed: ${friendlyDocError(e)}`, "error");
    } finally {
      markBusy(d.id, false);
    }
  }

  async function handleArchive(d: DocumentDetail) {
    const jwt = await getKromaToken();
    if (!jwt) { toast("Sign in first", "error"); return; }
    markBusy(d.id, true);
    try {
      await archiveDocument(jwt, d.id);
      toast(`Archived “${d.title}”`, "success");
      refresh();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.code === "NOT_FOUND")) {
        setDocs((prev) => prev.filter((x) => x.id !== d.id));
        toast("Not found — deleted or never shared with you", "error");
      } else {
        toast(`Archive failed: ${friendlyDocError(e)}`, "error");
      }
    } finally {
      markBusy(d.id, false);
    }
  }

  async function handlePurge(d: DocumentDetail) {
    const jwt = await getKromaToken();
    if (!jwt) { toast("Sign in first", "error"); return; }
    setConfirmPurgeId(d.id);
  }

  async function confirmPurge(d: DocumentDetail) {
    const jwt = await getKromaToken();
    if (!jwt) { toast("Sign in first", "error"); return; }
    setConfirmPurgeId(null);
    markBusy(d.id, true);
    try {
      await purgeDocument(jwt, d.id);
      toast(`Purged “${d.title}”`, "success");
      setDocs((prev) => prev.filter((x) => x.id !== d.id));
      setTotal((t) => Math.max(0, t - 1));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.code === "NOT_FOUND")) {
        setDocs((prev) => prev.filter((x) => x.id !== d.id));
        toast("Not found — already purged or never shared with you", "error");
      } else {
        toast(`Purge failed: ${friendlyDocError(e)}`, "error");
      }
    } finally {
      markBusy(d.id, false);
    }
  }

  const toggleShare = (r: string) =>
    setShareRoles((rs) => (rs.includes(r) ? rs.filter((x) => x !== r) : [...rs, r]));

  return (
    <div className="space-y-3">
      {/* ── Add documents: everything here configures the NEXT upload ── */}
      <section aria-labelledby="docs-add-heading" className="surface rounded-xl p-3" style={{ boxShadow: "var(--shadow)" }}>
        <h3 id="docs-add-heading" className="text-[13px] font-semibold tracking-tight">
          Add documents
        </h3>
        <p className="mt-0.5 text-[11px] ink-faint">
          {canUpload
            ? ".txt · .md · .pdf · URL · ≤20 MB — answers cite what you add"
            : `Ingestion needs the editor role (you: ${role}) — the library below is read-only for you`}
        </p>
        <div className="mt-2.5 space-y-2.5">
        {/* Dropzone */}
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files); }}
          onClick={() => canUpload && fileRef.current?.click()}
          className={`rounded-xl border border-dashed p-4 text-center text-xs transition-all ${dragOver ? "border-kroma-500 bg-kroma-500/10" : ""} ${canUpload ? "cursor-pointer hover:border-kroma-500/60" : "opacity-50"}`}
          style={{ borderColor: dragOver ? undefined : "var(--border)" }}
          title={canUpload ? "Drop .txt / .md / .pdf files or click to browse" : "Editors+ only"}>
          <input ref={fileRef} type="file" multiple accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"
            className="hidden" onChange={(e) => { if (e.target.files) handleFiles(e.target.files); e.target.value = ""; }} />
          <p className="flex justify-center text-[var(--ink-faint)]"><UploadCloud className="size-5" aria-hidden /></p>
          <p className="flex items-center justify-center gap-1 font-medium">
            {canUpload ? "Drop files or browse" : (
              <>
                <Lock className="size-3.5" aria-hidden />
                Editors only (you: {role})
              </>
            )}
          </p>
          <p className="ink-faint mt-0.5">.txt · .md · .pdf · URL below · ≤{(MAX_UPLOAD_BYTES / 1_048_576).toFixed(0)} MB</p>
        </div>

        {/* URL ingestion */}
        <form onSubmit={submitUrl} className="space-y-1.5">
          <Input value={urlTitle} onChange={(e) => setUrlTitle(e.target.value)} placeholder="Title (optional)" aria-label="Document title (optional)" />
          <div className="flex gap-1.5">
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" aria-label="Document URL" className="min-w-0 flex-1" />
            <Button type="submit" size="sm" disabled={!canUpload || !url.trim()}>
              Add
            </Button>
          </div>
        </form>

        {/* Share roles: who may SEE the next upload (not a library filter) */}
        <div role="group" aria-labelledby="docs-share-label" className="rounded-lg border border-[var(--border)] p-2.5">
          <p id="docs-share-label" className="text-[11px] font-semibold">
            Who can access new uploads
          </p>
          <p className="mt-0.5 text-[11px] ink-faint">
            Applies to files you add above — not to the library below.
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {["viewer", "editor", "admin"].map((r) => (
              <button key={r} type="button" onClick={() => toggleShare(r)}
                aria-pressed={shareRoles.includes(r)}
                className={`rounded-md border px-1.5 py-0.5 capitalize max-md:min-h-[36px] max-md:px-3 max-md:text-xs ${shareRoles.includes(r) ? "border-kroma-500/60 bg-kroma-500/10 font-semibold" : "opacity-60"}`}
                style={{ borderColor: shareRoles.includes(r) ? undefined : "var(--border)" }}>{r}</button>
            ))}
          </div>
        </div>

        {/* Jobs: transient upload state lives next to its cause */}
        {jobs.length > 0 && (
          <ul className="space-y-1.5" aria-label="Upload progress">
            {jobs.map((j) => (
              <li key={j.id} className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs" style={{ background: "var(--surface-2)" }}>
                {(j.state === "reading" || j.state === "uploading" || j.state === "indexing") && <Loader2 className="size-3.5 animate-spin text-kroma-500" aria-hidden />}
                {j.state === "done" && <CheckCircle2 className="size-3.5 text-emerald-500" aria-hidden />}
                {j.state === "error" && <XCircle className="size-3.5 text-red-500" aria-hidden />}
                <span className="min-w-0 flex-1 truncate font-medium">{j.label}</span>
                <span className="tnum shrink-0 ink-faint">{j.detail}</span>
              </li>
            ))}
          </ul>
        )}
        </div>
      </section>

      {/* ── Library: what your roles can already see ── */}
      <section aria-labelledby="docs-library-heading" className="surface rounded-xl p-3" style={{ boxShadow: "var(--shadow)" }}>
        <div className="flex items-baseline justify-between gap-2">
          <h3 id="docs-library-heading" className="text-[13px] font-semibold tracking-tight">
            Library
          </h3>
          <span className="tnum text-[11px] ink-faint" role="status">
            {listLoaded ? `${total} doc${total === 1 ? "" : "s"}` : "loading…"}
          </span>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); setQ(qDraft); }}
          className="mt-2 flex gap-1.5">
          <Input value={qDraft} onChange={(e) => setQDraft(e.target.value)} placeholder="Filter by title…" aria-label="Filter documents by title" className="min-w-0 flex-1" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as DocStatus | "all")}
            aria-label="Filter by indexing status"
            className="h-9 min-h-[36px] shrink-0 cursor-pointer rounded-lg border border-[var(--border)] bg-[var(--surface)] px-2 font-mono text-xs uppercase text-[var(--ink)] outline-none transition-colors hover:border-[var(--border-strong)] focus:border-kroma-500"
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <Button type="submit" variant="outline" size="sm">Search</Button>
          {q && (
            <Button type="button" variant="ghost" size="sm" onClick={() => { setQ(""); setQDraft(""); }}>
              Clear
            </Button>
          )}
        </form>

        <div className="mt-2.5">
        {!listLoaded ? (
          <ul className="space-y-1.5" aria-label="Loading documents">
            {[0, 1, 2].map((i) => (
              <li key={i} className="rounded-xl border border-[var(--border)] px-3 py-2">
                <Skeleton className="h-3 w-2/3" />
                <Skeleton className="mt-1.5 h-2 w-1/3" />
              </li>
            ))}
          </ul>
        ) : docs.length === 0 ? (
          <p className="rounded-lg p-3 text-center text-[11px] ink-faint" style={{ background: "var(--surface-2)" }}>
            Nothing visible to your roles yet — add a document above or adjust the filters.
          </p>
        ) : (
        <ul className="slim-scroll space-y-1.5 overflow-y-auto max-md:overflow-visible md:max-h-56">
          {docs.map((d) => {
            const manageable = canManage(d);
            const purgable = canPurge(d);
            const isBusy = busy.has(d.id);
            const inFlight = d.status === "pending" || d.status === "indexing";
            return (
              <li key={d.id} className="surface rounded-xl px-3 py-2 transition-shadow hover:shadow-[0_4px_20px_rgba(109,92,255,0.12)]" style={{ boxShadow: "var(--shadow)" }}>
                <div className="flex items-center gap-2.5">
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-kroma-500/10 text-kroma-500 dark:text-kroma-300" aria-hidden>
                    <DocMimeIcon mime={d.mime} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium">
                      {d.title}
                      {mySub && d.owner_sub === mySub && (
                        <span className="ink-faint ml-1.5 font-normal">· yours</span>
                      )}
                    </p>
                    <p className="tnum font-mono text-[10px] ink-faint">
                      v{d.version} · {d.chunks} chunks · {d.mime.split("/")[1] ?? d.mime}
                    </p>
                  </div>
                  <StatusBadge status={d.status} />
                </div>
                {d.status === "failed" && d.error && (
                  <p className="mt-1 truncate text-[10px] text-red-400" title={d.error}>
                    {d.error.slice(0, 120)}
                  </p>
                )}
                <div className="mt-1.5 flex flex-wrap items-center gap-1">
                  {manageable ? (
                    <>
                      <Button variant="ghost" size="xs" onClick={() => handleReindex(d)} disabled={isBusy || inFlight}
                        title={inFlight ? "Already indexing — wait for terminal state" : "Re-index (async job, bumps version)"}>
                        {isBusy ? <Loader2 className="animate-spin" aria-hidden /> : <RefreshCw aria-hidden />}
                        Reindex
                      </Button>
                      <Button variant="ghost" size="xs" onClick={() => handleArchive(d)} disabled={isBusy}
                        title="Soft-delete (Archive — recoverable via reindex)">
                        {isBusy ? <Loader2 className="animate-spin" aria-hidden /> : <Archive aria-hidden />}
                        Archive
                      </Button>
                    </>
                  ) : purgable ? (
                    <Badge variant="muted" title="Reindex and archive need the editor role — purge is available because you own this document">
                      <Lock aria-hidden /> restricted
                    </Badge>
                  ) : (
                    <Badge variant="muted" title="Only the document owner or an admin can manage this document">
                      <Lock aria-hidden /> restricted
                    </Badge>
                  )}
                  {purgable ? (
                    confirmPurgeId === d.id ? (
                      <span className="flex flex-wrap items-center gap-x-1 gap-y-1.5 rounded-lg border border-red-500/50 bg-red-500/10 px-2 py-1 text-xs" role="alertdialog" aria-label={`Confirm purge of ${d.title}`}>
                        <span className="ink-soft w-full">Purge “{d.title.slice(0, 24)}” forever?</span>
                        <Button variant="ghost" size="sm" onClick={() => confirmPurge(d)} disabled={isBusy}
                          className="min-h-[40px] font-semibold hover:bg-red-500/20 hover:text-red-500">
                          Yes, purge
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setConfirmPurgeId(null)} className="min-h-[40px]">
                          Keep
                        </Button>
                      </span>
                    ) : (
                      <Button variant="ghost" size="xs" onClick={() => handlePurge(d)} disabled={isBusy || inFlight}
                        title={inFlight ? "Purge blocked while indexing (409)" : "Hard-purge (irreversible CASCADE)"}
                        className="hover:bg-red-500/10 hover:text-red-500">
                        <Trash2 aria-hidden />
                        Purge
                      </Button>
                    )
                  ) : manageable ? (
                    <Badge variant="muted" title="Purge needs the document owner or an admin">
                      <Lock aria-hidden /> owner only
                    </Badge>
                  ) : null}
                  {inFlight && <span className="ml-auto flex items-center gap-1 text-xs ink-faint"><Loader2 className="size-3 animate-spin" aria-hidden />polling…</span>}
                </div>
              </li>
            );
          })}
        </ul>
        )}
        </div>
      </section>
    </div>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Archive,
  CheckCircle2,
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

function StatusBadge({ status }: { status: DocStatus }) {
  const busy = status === "pending" || status === "indexing";
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
    if (!jwt) return;
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
        return;
      }
      /* offline backend — list stays empty, upload errors surface per-job */
    }
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
    if (!window.confirm(`Purge “${d.title}” irreversibly? Chunks + shares are deleted (CASCADE).`)) return;
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

      {/* Share roles */}
      <div className="flex items-center gap-1.5 text-[11px] ink-soft">
        <span>Visible to:</span>
        {["viewer", "editor", "admin"].map((r) => (
          <button key={r} type="button" onClick={() => toggleShare(r)}
            className={`rounded-md border px-1.5 py-0.5 capitalize ${shareRoles.includes(r) ? "border-kroma-500/60 bg-kroma-500/10 font-semibold" : "opacity-60"}`}
            style={{ borderColor: shareRoles.includes(r) ? undefined : "var(--border)" }}>{r}</button>
        ))}
      </div>

      {/* Jobs */}
      {jobs.length > 0 && (
        <ul className="space-y-1.5">
          {jobs.map((j) => (
            <li key={j.id} className="surface flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs">
              {(j.state === "reading" || j.state === "uploading" || j.state === "indexing") && <Loader2 className="size-3.5 animate-spin text-kroma-500" aria-hidden />}
              {j.state === "done" && <CheckCircle2 className="size-3.5 text-emerald-500" aria-hidden />}
              {j.state === "error" && <XCircle className="size-3.5 text-red-500" aria-hidden />}
              <span className="min-w-0 flex-1 truncate font-medium">{j.label}</span>
              <span className="tnum shrink-0 ink-faint">{j.detail}</span>
            </li>
          ))}
        </ul>
      )}

      {/* Filters */}
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-1">
          {STATUS_FILTERS.map((s) => (
            <button key={s} type="button" onClick={() => setStatusFilter(s)}
              className={`rounded-md border px-1.5 py-0.5 font-mono text-[10px] uppercase ${statusFilter === s ? "border-kroma-500/60 bg-kroma-500/10 font-semibold" : "opacity-60"}`}
              style={{ borderColor: statusFilter === s ? undefined : "var(--border)" }}>{s}</button>
          ))}
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); setQ(qDraft); }}
          className="flex gap-1.5">
          <Input value={qDraft} onChange={(e) => setQDraft(e.target.value)} placeholder="Filter by title…" aria-label="Filter documents by title" className="min-w-0 flex-1" />
          <Button type="submit" variant="outline" size="sm">Search</Button>
          {q && (
            <Button type="button" variant="ghost" size="sm" onClick={() => { setQ(""); setQDraft(""); }}>
              Clear
            </Button>
          )}
        </form>
      </div>

      {/* Document list */}
      <div>
        <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider ink-faint">Your accessible docs ({total})</p>
        {docs.length === 0 && <p className="text-[11px] ink-faint">Nothing visible to your roles yet.</p>}
        <ul className="slim-scroll max-h-56 space-y-1.5 overflow-y-auto">
          {docs.map((d) => {
            const manageable = canManage(d);
            const purgable = canPurge(d);
            const isBusy = busy.has(d.id);
            const inFlight = d.status === "pending" || d.status === "indexing";
            return (
              <li key={d.id} className="surface rounded-lg px-2.5 py-1.5">
                <div className="flex items-center gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium">
                      {d.title}
                      {mySub && d.owner_sub === mySub && (
                        <span className="ink-faint ml-1.5 font-normal">· yours</span>
                      )}
                    </p>
                    <p className="font-mono text-[10px] ink-faint">
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
                <div className="mt-1.5 flex items-center gap-1">
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
                  ) : (
                    <Badge variant="muted" title="Reindex/archive needs owner+editor or admin">
                      <Lock aria-hidden /> restricted
                    </Badge>
                  )}
                  {purgable ? (
                    <Button variant="ghost" size="xs" onClick={() => handlePurge(d)} disabled={isBusy || inFlight}
                      title={inFlight ? "Purge blocked while indexing (409)" : "Hard-purge (irreversible CASCADE)"}
                      className="hover:bg-red-500/10 hover:text-red-500">
                      <Trash2 aria-hidden />
                      Purge
                    </Button>
                  ) : (
                    manageable === false && (
                      <Badge variant="muted" title="Purge needs owner or admin">
                        <Lock aria-hidden /> owner only
                      </Badge>
                    )
                  )}
                  {inFlight && <span className="ml-auto flex items-center gap-1 text-xs ink-faint"><Loader2 className="size-3 animate-spin" aria-hidden />polling…</span>}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

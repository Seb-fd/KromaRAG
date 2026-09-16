# 07 — Document Lifecycle Management

> Full lifecycle for ingested knowledge-base documents: multi-format upload & parsing, listing, metadata/RBAC ownership updates, async re-chunking / re-embedding, soft-delete (archive) vs hard-purge, and vector-index synchronization. Closes the lifecycle gap in `01`–`06`: `01` defines `pending|ready|failed` with no archive/versioning, `02` freezes `GET /documents` + `DELETE` (admin-only, no ownership check), `03` defines ingest/chunk/embed with no re-index contract. **Spec-first, no implementation code** — schemas, contracts, predicates, and rules only.

- **Status:** Draft (Build mode, approved 2026-09-16)
- **Refs:** `01 §2–§4` (ingestion flow, `kroma_*` schema, RBAC), `02 §1–§2+§4–§5` (endpoints, Pydantic, errors, JWT), `03 §1–§2+§6` (pipeline, chunking 600/90, knobs), `04 §1–§2` (tracing, usage), `06 §3.1` (leak-safe `404` ownership pattern)
- **Code truth:** `migrations/001_kroma_schema.sql:6-46`, `app/api/v1/documents.py:9-61`, `app/api/v1/ingest.py:9-30`, `app/services/ingest.py:26-171`, `app/services/retriever.py:12-20,82-92`, `app/rag/chunker.py:171-210`, `app/schemas/__init__.py:30-45`, `app/core/config.py:34-42`, `web/components/Documents.tsx:1-196`, `web/lib/api-client.ts:196-216`
- **Decisions (Ambiguity Gate, approved):** (1) `POST /documents/upload` (multipart, server-side `pypdf`) **complements** `POST /ingest` (JSON) — no replacement; (2) **soft-delete + purge** (`ARCHIVED` hidden, separate hard purge); (3) re-index via **`POST /{id}/reindex` async job** (`202` + status polling); (4) **owner+editor or admin** for update/reindex/archive, **owner or admin** for purge, **leak-safe uniform `404`** on all non-visible docs (Spec 06 behavior).

---

## 1. Requirements & User Stories

### 1.1 Feature Objective

Give editors and admins a complete, RBAC-safe document lifecycle without breaking retrieval correctness: upload `.txt` / `.md` / `.pdf` binaries or JSON text/URL (existing path preserved), parse server-side, track explicit states (`PENDING → INDEXING → INDEXED → FAILED`, plus `ARCHIVED` branch), allow metadata and share-role updates including admin-only ownership transfer, re-chunk/re-embed asynchronously with versioning, soft-archive (recoverable, retrieval-excluded) versus hard-purge (irreversible CASCADE), and keep `pgvector` + `tsvector` indexes synchronized with zero orphan chunks and zero cross-tenant leakage.

### 1.2 Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-01 | `editor+` can **ingest via JSON** (`POST /api/v1/ingest {title, content|url, mime, allowed_roles}` — legacy, unchanged) **or upload a binary file** (`POST /api/v1/documents/upload` multipart `file + title? + allowed_roles?` — new). Server sets `owner_sub = JWT sub`, validates mime/size, parses server-side. |
| FR-02 | `viewer+` can **list** RBAC-visible documents paginated (`GET /api/v1/documents?limit&offset&status&mime&q`), newest-first, with `chunks`, `version`, `status`. Only `INDEXED` docs are retrieval-eligible; list may show all states visible to caller. |
| FR-03 | Visible caller can **get one** document (`GET /api/v1/documents/{id}` → `DocumentDetail`). Non-visible / nonexistent → uniform `404 NOT_FOUND` (never `403`, never distinct message). |
| FR-04 | Owner-with-`editor+` or `admin` can **update metadata** (`PATCH /api/v1/documents/{id} {title?, allowed_roles?, owner_sub?}`). `owner_sub` transfer is **admin-only**. Metadata-only updates MUST NOT trigger re-embedding; re-embedding requires an explicit `POST /{id}/reindex` (see FR-07). |
| FR-05 | Owner-with-`editor+` or `admin` can **soft-delete** (`DELETE /api/v1/documents/{id}` → `status='archived'`, `archived_at=now()`). Archived docs are **excluded from retrieval and default list** (`status` filter can opt-in), chunks **retained** for restore/re-audit. Idempotent (second call → `404` if already purged, `200/204` no-op if already archived — §4.5). |
| FR-06 | Owner (any role held at purge time — ownership suffices) or `admin` can **hard-purge** (`POST /api/v1/documents/{id}/purge` → irreversible `DELETE`, CASCADE chunks + roles). Rejects with `409` while `status IN ('pending','indexing')`. Post-purge, all reads → `404`. |
| FR-07 | Owner-with-`editor+` or `admin` can **trigger re-index** (`POST /api/v1/documents/{id}/reindex {chunk_tokens?, chunk_overlap?, embedding_model?}` → `202 {job_id, version}`). Server flips `pending→indexing`, re-chunks + re-embeds in `BackgroundTasks`, bumps `version`, then `indexing→indexed` (or `→failed` with `error`). Concurrent reindex on same doc → `409 REINDEX_IN_PROGRESS`. |
| FR-08 | Visible caller can **poll status** (`GET /api/v1/documents/{id}/status` → `{status, chunks, version, error, updated_at}`) for upload/reindex progress. Non-visible → `404`. |
| FR-09 | **State machine enforced in DB + code** (§2.2, §4.2): `pending → indexing → indexed ⇄ archived`, `pending/indexing → failed → indexing` (retry via reindex), `archived → indexing` (reindex restores). No transition skips `indexing` for content changes. `retriever` only reads `indexed` (§4.4). |
| FR-10 | Frontend migrates `Documents.tsx` / `api-client.ts` to new contracts: multipart upload with progress, status badges (`pending/indexing/indexed/failed/archived`), filter by status, reindex/archive/purge actions gated by `role` + `owner_sub == session.sub`, read-through list cache (server authoritative). |

### 1.3 User Stories

- **US-1 Upload PDF:** As an editor, I drop `handbook.pdf` (8 MB) so the server extracts text, chunks 600/90, embeds, and the doc appears as `indexing` then `indexed` with chunk count — without pasting text manually.
- **US-2 Fix sharing:** As a doc owner, I `PATCH allowed_roles: ["editor"]` to restrict a draft so viewers stop retrieving it, without re-uploading or changing `version`/chunks.
- **US-3 Re-chunk after tuning:** As an admin, I `POST /reindex {chunk_tokens: 900}` on a stale doc so embeddings refresh under a new `version`, old chunks atomically replaced, no orphans, retrieval uninterrupted except for a documented `indexing` window.
- **US-4 Retire safely:** As an owner, I `DELETE` a deprecated policy so it vanishes from answers immediately but can still be audited/restored via reindex — and I `purge` it later for GDPR with all vectors gone.
- **US-5 No enumeration:** As a viewer, I cannot list, open, patch, reindex, archive, or purge another tenant's unshared doc — every attempt looks identical to a random UUID (`404`).

### 1.4 Non-Functional Requirements

| Category | Target |
|----------|--------|
| Performance | `GET /documents` p95 `< 250ms` @ 100K docs (composite indexes §2.2); `GET /{id}/status` p95 `< 100ms`; upload accept p95 `< 500ms` before background embed (sync parse only); reindex throughput ≥ 32 chunks/batch (`embed_batch`), `ivfflat probes=10`, `ANALYZE` after bulk (§4.4). Retrieval budgets from `01`/`03` unchanged. |
| Scaling | Stateless API; keyset-friendly pagination (`limit` clamp `1–100`, `OFFSET` allowed Phase 1, keyset deferred); multipart streamed to temp (never full-file in RAM); `BackgroundTasks` Phase 1, pgqueue/Redis deferred to prod-hardening spec. |
| Security | `owner_sub` = JWT `sub` only (never client-supplied except admin transfer); every lifecycle SQL carries visibility predicate (§3.1); uniform `404` on non-visible (no `403` for existence); `multipart` filename never trusted (sanitize, cap 255 chars); raw file bytes never logged; excerpts `≤ 500` chars per `04 §1`. |
| Reliability | Upload→`pending` row committed before embed starts (crash-safe → retryable via reindex); reindex is idempotent on `(doc_id, version)`; purge is irreversible and requires explicit `POST /purge` (no accidental `DELETE?hard=true`); orphan-chunk invariant `= 0` verified post-purge (§4.3). |
| Observability | LangSmith/tracing spans `ingest.parse`, `ingest.embed{chunks,model}`, `doc.reindex{from_version,to_version}`, `doc.purge{chunks_deleted}`; `done.usage`-style `status.error` surfaced truncated (`≤ 500` chars); `kroma_usage` unchanged (no new table Phase 1). |
| Configurability | `DOC_UPLOAD_MAX_BYTES=20_971_520` (20 MB server), `DOC_WEB_MAX_BYTES=2_000_000` (web guard, existing), `URL_MAX_BYTES=50_000` (unchanged), `DOC_REINDEX_ASYNC=true`, reuse `CHUNK_TOKENS/OVERLAP/MAX_PER_DOC`, `EMBED_BATCH=32`, `PGVECTOR_DIM=768` (all env-overridable, §2.5). |

---

## 2. Data Models / Schemas

### 2.1 Current Baseline (frozen, `001`)

```sql
-- 001 (exists, DO NOT rewrite):
CREATE TABLE kroma_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_sub TEXT NOT NULL,
  title TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT 'text/plain',
  uri TEXT,
  status TEXT NOT NULL DEFAULT 'ready',  -- 'pending'|'ready'|'failed' in practice, unconstrained (gap)
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE kroma_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id UUID NOT NULL REFERENCES kroma_documents(id) ON DELETE CASCADE,
  ord INT NOT NULL,
  content TEXT NOT NULL,
  content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding VECTOR(768) NOT NULL,
  token_count INT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE kroma_document_roles (
  doc_id UUID NOT NULL REFERENCES kroma_documents(id) ON DELETE CASCADE,
  role TEXT NOT NULL REFERENCES kroma_roles(name),
  PRIMARY KEY (doc_id, role)
);
```

Gaps closed by this spec: unconstrained `status` (legacy `'ready'` vs new vocabulary), no `updated_at`/`archived_at`, no `version`, no chunk/embedding provenance, no lifecycle indexes beyond `kroma_chunks_doc_idx`.

### 2.2 Migration `003_kroma_documents_lifecycle.sql` (idempotent DDL contract)

```sql
-- 003 — document lifecycle hardening. Idempotent; runs via psql or app.services.migrate.
-- Assumes 001. Additive only; never drops columns.

-- ── normalize legacy 'ready' → 'indexed' (safe re-run) ──
UPDATE kroma_documents SET status = 'indexed' WHERE status = 'ready';

-- ── lifecycle columns ──
ALTER TABLE kroma_documents
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS chunk_config JSONB NOT NULL DEFAULT '{"chunk_tokens":600,"chunk_overlap":90}',
  ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
  ADD COLUMN IF NOT EXISTS embedding_dim INT NOT NULL DEFAULT 768;
-- error, uri, mime, owner_sub, title, created_at already exist (001).

-- Backfill updated_at from created_at (safe re-run).
UPDATE kroma_documents SET updated_at = created_at WHERE updated_at IS NULL;

-- ── status CHECK (idempotent via DO block; ADD CONSTRAINT has no IF NOT EXISTS) ──
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_status_chk') THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_status_chk
      CHECK (status IN ('pending','indexing','indexed','failed','archived'));
  END IF;
END
$$;

-- ── version / dim sanity ──
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_version_chk') THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_version_chk CHECK (version >= 1);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_dim_chk') THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_dim_chk CHECK (embedding_dim IN (768));
  END IF;
END
$$;
-- NOTE: embedding_dim pinned to 768 Phase 1 (PGVECTOR_DIM). A future dim change
-- requires a new migration + full reindex spec; reindex with mismatched dim → 422.

-- ── access + lifecycle indexes ──
CREATE INDEX IF NOT EXISTS kroma_documents_owner_updated_idx
  ON kroma_documents (owner_sub, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_documents_status_updated_idx
  ON kroma_documents (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_documents_mime_idx
  ON kroma_documents (mime);
-- kroma_chunks_doc_idx + embedding ivfflat + tsv GIN from 001 untouched.
```

Notes:

- Canonical vocabulary is **lowercase**: `pending | indexing | indexed | failed | archived`. Spec prose uses `PENDING / INDEXING / INDEXED / FAILED / ARCHIVED` interchangeably; DB values are lowercase. Legacy `'ready'` MUST be migrated to `'indexed'` (equality alias `status='ready'` forbidden in new code).
- No RLS in Phase 1 (app-layer predicates normative, per `01 §4` pattern). RLS deferred.
- `kroma_chunks` DDL **unchanged** (CASCADE already correct). `content_tsv` regenerates automatically on re-INSERT; no trigger needed.
- `chunk_config` records the effective `{chunk_tokens, chunk_overlap}` per `version` for audit/reproducibility; informational, not a substitute for reindex params.

### 2.3 State Machine (normative)

```
                    ┌─────────────┐  upload / ingest OK
                    │   PENDING   │◄───────────────────── reindex requested
                    └──────┬──────┘  (row committed, embed queued)   from FAILED/ARCHIVED
                           │ BackgroundTasks starts
                           ▼
                    ┌─────────────┐  embed/chunk error
                    │  INDEXING   │─────────────────────┐
                    └──────┬──────┘                     │
                  success │                            ▼
                          ▼                     ┌─────────────┐
                    ┌─────────────┐             │   FAILED    │ (error truncated ≤2000 chars,
                    │   INDEXED   │────────────►│             │  chunks of prior version kept
                    └──────┬──────┘  archive    │             │  iff they existed, else zero)
                           │ (DELETE)           └──────┬──────┘
                           ▼                         │ reindex (POST /reindex)
                    ┌─────────────┐                   │ restores via INDEXING
                    │  ARCHIVED   │◄──────────────────┘
                    └──────┬──────┘  reindex also restores ARCHIVED→INDEXING→INDEXED
                           │ purge (POST /purge, irreversible)
                           ▼
                        (row gone, CASCADE chunks+roles, orphan count = 0)
```

Rules:

1. Content-affecting changes (file bytes, `chunk_tokens`/`overlap`, `embedding_model`) MUST transition through `INDEXING`; metadata-only `PATCH` (title/roles) MUST NOT change `status` or `version`.
2. `FAILED` retains `error` (truncated) and prior `version` chunks when they exist; `ARCHIVED` retains chunks but sets `archived_at`.
3. Only `INDEXED` rows are retrieval-eligible (`WHERE d.status='indexed'` in retriever — extends current `status='ready'` check).
4. `purge` is terminal; no restore. `purge` while `pending/indexing` → `409 ARCHIVE_CONFLICT`.

### 2.4 Pydantic v2 DTOs (additive to `02 §2`, `app/schemas`)

```python
class DocumentDetail(BaseModel):  # extends DocMeta, full lifecycle view
    id: UUID; title: str; mime: str; status: Literal["pending","indexing","indexed","failed","archived"]
    chunks: int = 0; version: int = 1
    owner_sub: str; uri: str | None = None
    allowed_roles: list[str] = []  # from kroma_document_roles
    chunk_config: dict = Field(default_factory=lambda: {"chunk_tokens": 600, "chunk_overlap": 90})
    embedding_model: str = "gemini-embedding-001"; embedding_dim: int = 768
    error: str | None = None  # truncated ≤500 chars on read; full ≤2000 in DB
    created_at: datetime | None = None; updated_at: datetime | None = None
    archived_at: datetime | None = None

class DocumentUpdate(BaseModel):  # PATCH body; all optional, at least one required
    title: str | None = Field(default=None, min_length=1, max_length=300)
    allowed_roles: list[str] | None = None  # ⊆ {admin,editor,viewer}; [] = owner-only
    owner_sub: str | None = None  # admin-only; non-admin supply → 404 (leak-safe, treated as invisible op)
    uri: str | None = Field(default=None, max_length=2000)

class DocumentList(BaseModel):
    items: list[DocumentDetail]; total: int  # items carry chunks+version (JOIN-aggregated)

class ReindexRequest(BaseModel):
    chunk_tokens: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=500)
    embedding_model: str | None = None  # must equal pinned model Phase 1 else 422
    # overlap must be < chunk_tokens when both resolved (validate in handler).

class ReindexResponse(BaseModel):
    doc_id: UUID; job_id: UUID; status: Literal["indexing"]; version: int  # version = old+1, reserved synchronously

class DocumentStatus(BaseModel):
    doc_id: UUID; status: str; chunks: int; version: int
    error: str | None = None; updated_at: datetime | None = None

class DocMeta(BaseModel):  # EXTENDED (02 §2), additive only
    id: UUID; title: str; mime: str; status: str; chunks: int = 0
    created_at: datetime | None = None
    version: int = 1  # NEW additive field; old clients ignore
```

Validation: `title` stripped, blank → `422 VALIDATION`; `allowed_roles` elements ⊆ `{admin,editor,viewer}`, deduped; `owner_sub` non-empty string when present; `PATCH` with zero actionable fields → `422`; `embedding_model` mismatch → `422 EMBEDDING_MISMATCH`; `error` never client-settable.

### 2.5 TypeScript Types (`apps/web/lib`, additive)

```typescript
export type DocStatus = "pending" | "indexing" | "indexed" | "failed" | "archived";
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
export type DocumentStatus = { doc_id: string; status: DocStatus; chunks: number; version: number; error: string | null; updated_at: string | null };
export type UploadJob = {
  id: string; label: string;
  state: "reading" | "uploading" | "indexing" | "done" | "error";
  detail: string; doc_id: string | null;
};
```

### 2.6 Config Knobs (env, extends `03 §6` + `app/core/config.py`)

```python
doc_upload_max_bytes: int = 20_971_520  # DOC_UPLOAD_MAX_BYTES — server multipart cap (20 MB)
doc_web_max_bytes: int = 2_000_000      # DOC_WEB_MAX_BYTES — web guard (existing 2 MB, unchanged)
url_max_bytes: int = 50_000             # URL_MAX_BYTES — unchanged
doc_reindex_async: bool = True          # DOC_REINDEX_ASYNC — False only for tests (sync inline)
embed_batch: int = 32                   # unchanged (03 §6)
pgvector_dim: int = 768                 # unchanged; reindex validates equality
```

---

## 3. System Interface & Contracts

Base `{API_URL}/api/v1`. Auth `Authorization: Bearer <Auth.js JWT>` (`get_current_principal` + `require_role` per `02 §5`). Errors `02 §4` shape `{"detail":{"code","message"}}` + `X-Trace-Id` header. **Leak-safe rule (normative):** any operation on a doc the caller cannot see returns `404 {"code":"NOT_FOUND","message":"not found or forbidden"}` — identical body to a random nonexistent UUID — never `403` for existence. (`403 FORBIDDEN` is reserved exclusively for role-gate failures, e.g. `viewer` calling an `editor+` route.)

| Method & Path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `POST /api/v1/ingest` (legacy) | `editor+` | `IngestRequest{title, content\|url, mime, allowed_roles}` (JSON) | `202 {doc_id, status, chunks}` | Unchanged contract; internally maps `ready→indexed`; sets `version=1`, `chunk_config`, `embedding_model`. |
| `POST /api/v1/documents/upload` | `editor+` | `multipart/form-data: file(binary, required), title?(text, default=filename sans ext), allowed_roles?(JSON array str, default=["viewer"]), chunk_tokens?(int), chunk_overlap?(int)` | `202 {doc_id, status:"pending"\|"indexing", chunks:0, job_id}` | **New.** Server parses PDF/MD/TXT; commits `pending` row synchronously, embeds in `BackgroundTasks`. Size `> DOC_UPLOAD_MAX_BYTES` → `413`. |
| `GET /api/v1/documents?limit=20&offset=0&status=&mime=&q=` | `viewer+` | query filters | `DocumentList` | RBAC-filtered (§3.1). `status` ∈ lifecycle vocab or `all` (default excludes `archived`); `mime` exact; `q` ILIKE on title (trigram-assisted). `limit` clamp `1–100`. |
| `GET /api/v1/documents/{id}` | visible only | — | `DocumentDetail` | Non-visible → `404`. `error` truncated to 500 chars. |
| `GET /api/v1/documents/{id}/status` | visible only | — | `DocumentStatus` | Lightweight poll; same `404` rule. |
| `PATCH /api/v1/documents/{id}` | owner+`editor+` or `admin` (else `404`) | `DocumentUpdate` (≥1 field) | `DocumentDetail` | `owner_sub` present + caller not `admin` → `404` (not `403`). Bumps `updated_at`, never `version`/`status`. |
| `DELETE /api/v1/documents/{id}` | owner+`editor+` or `admin` (else `404`) | — | `204` (no body) | **Soft-archive**: `status='archived'`, `archived_at=now()`. Already archived → `204` no-op. `pending/indexing` → archive allowed (cancels job marker; background task MUST no-op on completion — §4.2). |
| `POST /api/v1/documents/{id}/reindex` | owner+`editor+` or `admin` (else `404`) | `ReindexRequest{}` (empty = same config) | `202 ReindexResponse{job_id, version}` | Reserves `version+1` + `status='indexing'` synchronously; embed async. `pending/indexing` in flight → `409 REINDEX_IN_PROGRESS`. `archived` → allowed (restores). Dim/model mismatch → `422`. |
| `POST /api/v1/documents/{id}/purge` | owner or `admin` (else `404`) | — | `204` | **Hard-delete**: `DELETE FROM kroma_documents` (CASCADE). `pending/indexing` → `409`. Irreversible. Frozen choice: no `DELETE?hard=true` variant. |

### 3.1 Ownership & Visibility SQL Predicates (normative, never post-filter)

```sql
-- Visibility fragment (reads: list/get/status). $1=user_sub, $2=user_roles TEXT[].
-- Admin bypasses share check but still passes role gate in code.
(d.owner_sub = $1 OR 'admin' = ANY($2) OR EXISTS (
  SELECT 1 FROM kroma_document_roles r WHERE r.doc_id = d.id AND r.role = ANY($2)
))

-- Single-doc guard (get/status/patch/delete/reindex/purge): select first, act second.
SELECT d.id, d.owner_sub, d.status, d.version FROM kroma_documents d
WHERE d.id = $1 AND (d.owner_sub = $2 OR 'admin' = ANY($3) OR EXISTS (
  SELECT 1 FROM kroma_document_roles r WHERE r.doc_id = d.id AND r.role = ANY($3)
));
-- Miss → 404 {"code":"NOT_FOUND","message":"not found or forbidden"}.

-- Write guard (patch/delete/reindex): owner+editor or admin.
-- After visibility SELECT: allow iff (row.owner_sub = sub AND max_role_level(roles) >= editor)
--   OR ('admin' = ANY(roles)). Else → 404 (NOT 403) to avoid ownership oracle.
-- Purge guard: allow iff (row.owner_sub = sub) OR ('admin' = ANY(roles)). Else → 404.

-- List (RBAC WHERE, newest-first, filters):
SELECT d.*, COALESCE(c.chunks,0)::int AS chunks, COALESCE(r.roles,'{}') AS allowed_roles
FROM kroma_documents d
LEFT JOIN (SELECT doc_id, COUNT(*) AS chunks FROM kroma_chunks GROUP BY doc_id) c ON c.doc_id = d.id
LEFT JOIN (SELECT doc_id, array_agg(role) AS roles FROM kroma_document_roles GROUP BY doc_id) r ON r.doc_id = d.id
WHERE (d.owner_sub = $1 OR 'admin' = ANY($2) OR EXISTS (
  SELECT 1 FROM kroma_document_roles rr WHERE rr.doc_id = d.id AND rr.role = ANY($2)))
  AND ($3::text IS NULL OR $3 = 'all' OR d.status = $3)       -- default excludes archived: caller passes status filter; NULL means 'indexed','pending','indexing','failed'
  AND ($4::text IS NULL OR d.mime = $4)
  AND ($5::text IS NULL OR d.title ILIKE '%'||$5||'%')
ORDER BY d.updated_at DESC LIMIT $6 OFFSET $7;
-- + SELECT COUNT(*) with identical WHERE AS total.
-- Default (no status param): status IN ('pending','indexing','indexed','failed').
```

### 3.2 Request / Response Examples (non-breaking, additive)

```http
POST /api/v1/documents/upload  Authorization: Bearer <editor JWT>
Content-Type: multipart/form-data; boundary=---
--file: handbook.pdf (8_388_608 bytes); title: "Handbook"; allowed_roles: ["viewer"]
→ 202 {"doc_id":"a1b2…","status":"indexing","chunks":0,"job_id":"j9…"}
  Headers: X-Trace-Id: <uuid>

GET /api/v1/documents?limit=20&offset=0&status=indexed
→ 200 {"items":[{"id":"a1b2…","title":"Handbook","mime":"application/pdf","status":"indexed","chunks":42,"version":1,"owner_sub":"google-oauth2|108…","allowed_roles":["viewer"],…}],"total":7}

PATCH /api/v1/documents/a1b2…  Authorization: Bearer <owner+editor JWT>
{"allowed_roles":["editor"]}
→ 200 {…,"allowed_roles":["editor"],"version":1,"status":"indexed"}  // version/status untouched

POST /api/v1/documents/a1b2…/reindex  {"chunk_tokens":900,"chunk_overlap":120}
→ 202 {"doc_id":"a1b2…","job_id":"j10…","status":"indexing","version":2}

GET /api/v1/documents/a1b2…/status
→ 200 {"doc_id":"a1b2…","status":"indexing","chunks":0,"version":2,"error":null,…}
→ (later) 200 {"doc_id":"a1b2…","status":"indexed","chunks":31,"version":2,…}

DELETE /api/v1/documents/a1b2…  → 204  // archived_at set; retrieval excludes
POST /api/v1/documents/a1b2…/purge  → 204  // row + chunks + roles gone
```

### 3.3 Frontend Client Functions (`apps/web/lib/api-client.ts` contracts, additive)

```typescript
uploadDocument(args: {
  file: File; title?: string; allowed_roles?: string[];
  chunk_tokens?: number; chunk_overlap?: number; jwt?: string;
  onProgress?: (pct: number) => void;
}): Promise<{ doc_id: string; status: DocStatus; chunks: number; job_id: string }>;
listDocuments(
  jwt?: string, limit?: number, offset?: number,
  filters?: { status?: DocStatus | "all"; mime?: string; q?: string },
): Promise<DocumentList>;
getDocument(jwt: string | undefined, id: string): Promise<DocumentDetail>;
getDocumentStatus(jwt: string | undefined, id: string): Promise<DocumentStatus>;
patchDocument(jwt: string | undefined, id: string, body: { title?: string; allowed_roles?: string[]; owner_sub?: string; uri?: string }): Promise<DocumentDetail>;
archiveDocument(jwt: string | undefined, id: string): Promise<void>;   // DELETE
purgeDocument(jwt: string | undefined, id: string): Promise<void>;     // POST /purge
reindexDocument(jwt: string | undefined, id: string, body?: { chunk_tokens?: number; chunk_overlap?: number }): Promise<ReindexResponse>;
// deleteDocument(jwt, id) kept as alias of archiveDocument (soft) — hard purge MUST call purgeDocument explicitly.
```

`Documents.tsx` migration: dropzone accepts `.txt/.md/.pdf` (PDF now uploads binary via `FormData`, no longer rejected); per-job `UploadJob` state machine `reading→uploading→indexing→done|error` with `GET status` polling while `indexing`; status filter chips + `ARCHIVED` toggle; row actions `Reindex / Archive / Purge` gated by `(doc.owner_sub === session.sub && canUpload) || isAdmin`; `localStorage` untouched (no thread cache for docs); errors mapped: `413` oversize, `422` mime/validation, `404` leak-safe, `409` reindex-in-flight.

---

## 4. Business Logic & Edge Cases

### 4.1 Ingestion & Parsing Strategies (upload complements ingest)

1. **Route selection:** `POST /ingest` (JSON `content`/`url`, Phase 1–2 behavior preserved) vs `POST /documents/upload` (multipart `file`). Both converge on `plan_ingest()` → `chunk_text()` → embed batch → persist. `upload` MUST NOT accept raw `content` JSON alongside `file` (→ `422` if both shapes mixed via wrong content-type).
2. **MIME resolution:** trust order — explicit `mime` form field (if allowlisted) > `file.content_type` > filename extension (`.pdf→application/pdf`, `.md/.markdown→text/markdown`, else `text/plain`). Unresolvable/unsupported → `422 VALIDATION` before any DB write. Stored `mime` is the normalized token (`split(';')[0].strip().lower()`), matching `validate_mime()`.
3. **PDF (`application/pdf`):** server-side `pypdf` extraction (§code truth `extract_pdf_text`): per-page `extract_text()`, join `\n\n`. Encrypted/unreadable/zero-text PDFs → `status='failed'`, `error='pdf extract yielded no text…'` (truncated ≤2000 chars in DB, ≤500 on read). Scanned-image PDFs (no text layer) fail with actionable message suggesting OCR out of scope (deferred, no OCR Phase 1).
4. **Markdown / TXT:** bytes decoded UTF-8 (`errors="replace"`), then `clean_text()` (normalize whitespace, strip control chars) + `language_warning()` heuristic (warn-only, never block — `03 §1`).
5. **URL ingestion (legacy path):** unchanged — 50KB cap, readability-plain decode, timeout 15s. URL + `file` both present → `422`.
6. **Size gates:** multipart `file` `> DOC_UPLOAD_MAX_BYTES` (20 MB) → `413 PAYLOAD_TOO_LARGE` before parse; web client pre-guards at 2 MB (`DOC_WEB_MAX_BYTES`) with friendly message but server is authoritative. `title` defaults to sanitized filename sans extension (255-char cap, stripped, blank → `422`); explicit `title` overrides.
7. **Commit order (crash-safe):** `INSERT kroma_documents(status='pending', version, chunk_config, embedding_model/dim)` committed synchronously → return `202 {doc_id, job_id}` → `BackgroundTasks` runs extract→chunk→embed→`status='indexing'`→chunks/roles→`'indexed'`. Crash between commit and embed leaves retryable `pending` (reindex recovers — §4.2). Total-parse failure → `failed` + `error`, zero chunks (same as current `except → status='failed'` behavior).

### 4.2 Re-chunking & Dynamic Embedding Updates (async job)

1. **Reservation (sync, in request):** visibility + write guard (§3.1) → reject `409` if `status IN ('pending','indexing')` (job in flight; `job_id` of in-flight job echoed in message) → resolve effective `chunk_tokens/overlap` (request override else stored `chunk_config` else global defaults) → validate `0 ≤ overlap < tokens`, `tokens 100–2000`, `embedding_model` equals pinned model else `422` → `UPDATE … SET status='indexing', version=version+1, chunk_config=$new, updated_at=now() WHERE id=$1 AND version=$old` (optimistic guard; lost update → `409`) → return `202 {job_id (gen_random_uuid), version}`.
2. **Execution (background):** re-read source text — authoritative source is **stored chunk content reassembled in `ord` order** when original bytes are unavailable (multipart bytes are NOT retained Phase 1; `uri`/URL re-fetched only if the doc was URL-ingested and `content` absent, else reassemble). Re-run `chunk_text(text, target, overlap)` → embed batch-32 (retry 3× exponential, per `03 §3`) → in a **single transaction**: `DELETE FROM kroma_chunks WHERE doc_id=$1; <INSERT new chunks>; UPDATE kroma_documents SET status='indexed', error=NULL, updated_at=now() WHERE id=$1`. Failure → `UPDATE … SET status='failed', error=$truncated WHERE id=$1` (**prior-version chunks are already deleted at this point** — see rule 3).
3. **Failure atomicity trade-off (frozen):** to bound memory, Phase 1 deletes old chunks inside the same transaction that inserts new ones; a mid-transaction crash rolls back to the **pre-reindex state** (`indexing` + old chunks intact — Postgres atomicity). Only a post-embed, post-commit failure path (non-transactional embed error before `BEGIN`) leaves `failed` with prior chunks intact. There is NO window where a doc has zero chunks while `indexed` — `indexed` is set only after inserts commit.
4. **Cancellation by archive:** if `DELETE` (archive) lands while reindex runs, the background task MUST check `SELECT status` before final `status='indexed'` flip; if `archived`, it keeps the **new chunks** (so restore is coherent) but leaves `status='archived'` (never flips archived→indexed implicitly).
5. **Restore:** `POST /reindex` on `archived` or `failed` is the supported restore path (no separate restore endpoint Phase 1): `archived→indexing→indexed` clears `archived_at`; `failed→indexing→indexed` clears `error`.
6. **Idempotency:** `(job_id)` is informational (logged, returned for polling correlation); the guard against double-execution is `(doc_id, version)` reservation + `409` on in-flight. Retrying the same `POST /reindex` twice quickly yields one `202` + one `409` (never two version bumps).

### 4.3 Orphan Chunk Cleanup & Cascade Deletion

```sql
-- Purge (single transaction, normative order):
BEGIN;
-- 1. Guard (visibility + purge permission + status not pending/indexing) — §3.1.
-- 2. Delete roles explicitly for audit count (CASCADE would suffice; explicit for metrics):
DELETE FROM kroma_document_roles WHERE doc_id = $1;
-- 3. Delete chunks explicitly (CASCADE would suffice; explicit for deleted-count metric):
DELETE FROM kroma_chunks WHERE doc_id = $1;
-- 4. Delete doc (CASCADE backstop for any missed edge):
DELETE FROM kroma_documents WHERE id = $1;
COMMIT;
-- 5. Verify (same txn or immediate follow-up, logged):
SELECT COUNT(*) FROM kroma_chunks WHERE doc_id = $1;  -- MUST be 0, else ERROR alert
```

- Archive (`DELETE`) deletes **nothing** except flipping status — chunks/roles retained byte-for-byte.
- `kroma_chunks.doc_id ON DELETE CASCADE` remains the backstop; explicit deletes are normative for countable purge metrics (`purge{chunks_deleted}` span).
- Orphan invariant: `SELECT COUNT(*) FROM kroma_chunks c LEFT JOIN kroma_documents d ON d.id=c.doc_id WHERE d.id IS NULL` MUST always be `0` (FK guarantees; nightly check logged, alert on violation).
- `ivfflat` index needs no explicit maintenance on delete (Postgres handles); after bulk purges (>10% of chunks) operators SHOULD run `ANALYZE kroma_chunks` (runbook note, not API behavior).

### 4.4 Vector Index Synchronization

1. **Eligibility:** retriever SQL gains `AND d.status = 'indexed'` (replacing the current `d.status = 'ready'` literal — the only retriever change in this spec). `pending/indexing/failed/archived` rows are invisible to hybrid search regardless of role shares. `ts_rank`/`content_tsv` regenerate automatically per re-INSERT (generated column).
2. **Write format:** unchanged — `vec_literal "[f1,…,f768]"` passed as `$::vector` (code truth `ingest.py:156`, `retriever.py:40`). Reindex validates `len(vector) == embedding_dim == 768` before INSERT; mismatch aborts job → `failed`, never partial insert.
3. **Index tuning:** `SET LOCAL ivfflat.probes = 10` retained; `lists=100` retained Phase 1. Post-reindex of a large doc (>1000 chunks) the worker issues `ANALYZE kroma_chunks` best-effort (failure swallowed, fakes-compatible).
4. **Model pinning:** `embedding_model` stored per doc per version. Phase 1 only the pinned model is accepted; a future model migration requires a dedicated spec + migration (out of scope — no silent mixed-model corpora).

### 4.5 Soft-Delete vs Hard-Purge Semantics (frozen)

| Aspect | `DELETE /{id}` (soft-archive) | `POST /{id}/purge` (hard-purge) |
|---|---|---|
| Auth | owner+`editor+` or `admin`, else `404` | owner (any role) or `admin`, else `404` |
| Effect | `status='archived'`, `archived_at=now()`, `updated_at=now()`; chunks+roles kept | rows in `kroma_documents` + `kroma_chunks` + `kroma_document_roles` deleted |
| Retrieval | excluded immediately | excluded (row gone) |
| List default | hidden (opt-in `?status=archived` / `all`) | gone |
| Idempotency | archived→`204` no-op; purged→`404` | purged→`404`; in-flight (`pending/indexing`)→`409` |
| Restore | `POST /reindex` (→`indexed`, clears `archived_at`) | impossible |

### 4.6 Permission Verification Before Deletion / Re-indexing (normative order)

1. Authenticate (`401` on missing/bad JWT, missing `sub` — `02 §5` unchanged).
2. Role gate per route (`editor+` for upload/reindex-archive family at the FastAPI dependency layer → `403` on failure — the ONLY `403` in this spec).
3. Visibility SELECT (§3.1 single-doc guard) → miss → `404` uniform.
4. Operation-specific check in Python (owner+editor vs owner-vs-admin matrix §4.5) → fail → `404` uniform (deliberately NOT `403` — prevents ownership/share oracle; Spec 06 pattern).
5. State check (`pending/indexing` → `409` for purge/concurrent-reindex) → `409 REINDEX_IN_PROGRESS` / `ARCHIVE_CONFLICT`.
6. Execute + bump `updated_at` (all mutating ops) + emit tracing span.

| Caller \ Op | List/Get/Status | Upload | Patch | Reindex | Archive | Purge |
|---|---|---|---|---|---|---|
| `viewer`, shared doc | ✅ | `403` | `404` | `404` | `404` | `404` |
| `viewer`, unshared/foreign | `404` | `403` | `404` | `404` | `404` | `404` |
| `editor`, own doc | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (ownership suffices) |
| `editor`, shared-not-own | ✅ | ✅ (new doc) | `404` | `404` | `404` | `404` |
| `admin`, any doc | ✅ | ✅ | ✅ (+`owner_sub` transfer) | ✅ | ✅ | ✅ |

### 4.7 Validation, Fallbacks, Error Handling

| Case | Behavior |
|------|----------|
| Missing/expired JWT, bad `iss/aud`, missing `sub` | `401 UNAUTHENTICATED` (unchanged). |
| `viewer` uploads or calls `editor+` route | `403 FORBIDDEN` (role gate, before existence checks). |
| Guessed UUID / foreign unshared doc on any `/{id}` route | `404 NOT_FOUND {"message":"not found or forbidden"}` — byte-identical to truly-missing id. |
| `PATCH owner_sub` by non-admin | `404` (treated as invisible op, not `403`). |
| Unsupported mime / empty-after-clean / `>2000` chunks | `422 VALIDATION` (upload) or `failed` + `error` (async phase discovers it). |
| File `> 20 MB` | `413 PAYLOAD_TOO_LARGE {"code":"PAYLOAD_TOO_LARGE"}`. |
| `chunk_overlap ≥ chunk_tokens`, `tokens` out of `100–2000` | `422 VALIDATION`. |
| `embedding_model` ≠ pinned | `422 EMBEDDING_MISMATCH`. |
| Concurrent `POST /reindex` while `pending/indexing` | `409 REINDEX_IN_PROGRESS` (echoes in-flight `version`). |
| `POST /purge` while `pending/indexing` | `409 ARCHIVE_CONFLICT` (retry after `indexed`/`failed`). |
| Embed provider down mid-reindex | job → `failed` + truncated `error`; prior chunks preserved per §4.2 rule 3; retry via `POST /reindex`. |
| SSE/query during `indexing` | retrieval sees pre-reindex chunks until commit flip (no partial mix — single-txn swap); immediately after commit sees new chunks. |
| `PATCH` with zero fields / blank title / bad roles | `422 VALIDATION`. |

---

## 5. Acceptance Criteria (Gherkin)

```gherkin
Feature: Document Lifecycle Management

  Background:
    Given users "alice" (roles ["editor"]) and "bob" (roles ["viewer"]) with valid JWTs
      And PGVECTOR_DIM 768 and chunk defaults 600/90 and hybrid weights 0.7/0.3 unchanged
      And legacy POST /api/v1/ingest still accepts JSON

  Scenario: Multipart PDF upload complements JSON ingest
    When "alice" POSTs /api/v1/documents/upload multipart file "handbook.pdf" (8 MB) title "Handbook" allowed_roles ["viewer"]
    Then response is 202 with doc_id UUID, status in {"pending","indexing"}, and job_id UUID
    When "alice" polls GET /api/v1/documents/{id}/status until terminal
    Then status becomes "indexed" with chunks > 0 and version 1 and error null
      And GET /api/v1/documents/{id} shows mime "application/pdf" and allowed_roles ["viewer"]
      And "bob" GETs /api/v1/documents/{id} successfully (shared viewer)
      And hybrid retrieval for a query from the PDF can cite [doc:Handbook:ord]

  Scenario: Metadata update does not re-embed
    Given doc D owned by "alice" indexed version 1 with 42 chunks
    When "alice" PATCHes /api/v1/documents/D {"allowed_roles":["editor"]}
    Then response is 200 with allowed_roles ["editor"] and version still 1 and status still "indexed"
      And chunk count is still 42
      And "bob" (viewer) GETs /api/v1/documents/D → 404 uniform
      And "bob" GET /api/v1/documents does not contain D

  Scenario: Admin-only ownership transfer
    When "alice" PATCHes /api/v1/documents/D {"owner_sub":"bob"} as non-admin
    Then response is 404 (not 403), owner unchanged
    When an "admin" PATCHes /api/v1/documents/D {"owner_sub":"bob"}
    Then response is 200 with owner_sub "bob"

  Scenario: Async reindex bumps version and swaps chunks atomically
    Given doc D owned by "alice" indexed version 1
    When "alice" POSTs /api/v1/documents/D/reindex {"chunk_tokens":900,"chunk_overlap":120}
    Then response is 202 with version 2 and job_id and status "indexing"
    When "alice" POSTs /api/v1/documents/D/reindex {} immediately again
    Then response is 409 {"code":"REINDEX_IN_PROGRESS"}
    When polling GET /api/v1/documents/D/status until terminal
    Then status becomes "indexed" with version 2 and updated chunk count
      And no query during indexing ever mixed v1+v2 chunks (single-txn swap)
      And kroma_documents.chunk_config equals {"chunk_tokens":900,"chunk_overlap":120}

  Scenario: Soft-delete hides but retains, purge destroys
    When "alice" DELETEs /api/v1/documents/D
    Then response is 204 and GET status shows "archived" with archived_at set
      And hybrid retrieval no longer cites D
      And default GET /api/v1/documents excludes D but ?status=archived includes it
      And "bob" GETs /api/v1/documents/D → 404
    When "alice" POSTs /api/v1/documents/D/reindex {}
    Then doc restores to "indexed" with version bumped and archived_at cleared
    When "alice" DELETEs D again then POSTs /api/v1/documents/D/purge
    Then purge is 204
      And GET /api/v1/documents/D is 404 and status is 404
      And SELECT COUNT(*) FROM kroma_chunks WHERE doc_id=D equals 0
      And orphan check (chunks with no parent doc) equals 0
      And second purge returns 404 (idempotent)

  Scenario: Purge blocked while indexing
    Given doc E in status "indexing" (upload in flight)
    When "alice" POSTs /api/v1/documents/E/purge
    Then response is 409 {"code":"ARCHIVE_CONFLICT"}

  Scenario: Cross-tenant isolation is leak-safe 404 on every lifecycle route
    Given doc D owned by "alice" with allowed_roles ["editor"] (bob has viewer only)
    When "bob" GETs /api/v1/documents/D
      Or "bob" GETs /api/v1/documents/D/status
      Or "bob" PATCHes /api/v1/documents/D {"title":"x"}
      Or "bob" POSTs /api/v1/documents/D/reindex {}
      Or "bob" DELETEs /api/v1/documents/D
      Or "bob" POSTs /api/v1/documents/D/purge
    Then every response is 404 with body identical to a random nonexistent UUID
      And "bob" GET /api/v1/documents never contains D

  Scenario: Failed ingest surfaces truncated error and is retryable
    When "alice" uploads a zero-text encrypted PDF
    Then terminal status is "failed" with error non-null truncated to ≤500 chars on read
      And retrieval never cites the doc
    When "alice" POSTs /api/v1/documents/{id}/reindex {} after fixing source expectations
    Then a new version is reserved and the job runs (no stuck state)

  Scenario: Legacy ingest path preserved
    When "alice" POSTs /api/v1/ingest {"title":"Note","content":"hello world …","mime":"text/plain"}
    Then response is 202 with status "indexed" (never legacy "ready") and version 1
      And GET /api/v1/documents/{id} shows the doc with chunk_config defaults
```

### Exit Gates (Phase mapping, `05` cross-cutting)

- `uv run pytest -q` green incl. new `tests/test_documents_lifecycle.py` (upload multipart incl. PDF fixture, status machine transitions, patch-no-reembed, reindex version swap + `409`, archive-vs-purge matrix, cross-tenant `404` uniformity diffed byte-for-byte, orphan-zero check, retriever excludes non-`indexed`).
- `npm run build --workspace apps/web` green; E2E upload→poll→filter→reindex→archive→restore→purge passes with role-gated buttons; Lighthouse `≥ 85` unaffected.
- `npm run spec:check` green (this file additive; `02` normative claims untouched except `ready→indexed` alias which is documented here).
- Manual: `UPDATE … SET status='ready'` legacy row migrates to `indexed` on `003` run; `ANALYZE` runbook verified after bulk reindex on Neon.

---

## Appendix — Normative Deltas Index (for reviewers)

- **SQL:** `003` normalizes `ready→indexed`, adds `updated_at`, `archived_at`, `version`, `chunk_config`, `embedding_model/dim`, `status/version/dim` CHECKs, 3 indexes. `kroma_chunks` + CASCADE untouched. Purge order + orphan invariant frozen (§4.3).
- **API:** 1 legacy-kept (`POST /ingest`) + 7 lifecycle routes (`upload`, `list+filters`, `get`, `status`, `patch`, `archive-DELETE`, `reindex`, `purge`). All `/{id}` reads/writes visibility-scoped; `404`-not-`403` on existence; `403` only for role gates; `409` for in-flight conflicts; `413` for oversize; `422` for validation/dim mismatch.
- **State:** `pending→indexing→indexed ⇄ archived`, `→failed→indexing` retry. Only `indexed` retrieval-eligible. Metadata PATCH never bumps version; reindex always does.
- **RBAC matrix:** §4.6 table normative — owner+`editor+`/admin for patch/reindex/archive; owner/admin for purge; `owner_sub` transfer admin-only; uniform `404` oracle protection.
- **Web:** `api-client.ts` +8 fns (§3.3), `deleteDocument` aliased to soft-archive; `Documents.tsx` binary-PDF upload, `UploadJob` machine, status polling, filter chips, gated actions.
- **Out of scope:** OCR for scanned PDFs, file-byte retention/version diff, chunk-level edit, bulk import/export, pgqueue/Redis job backend, RLS policies, retention/TTL auto-purge, per-doc embedding-model migration, full-text `q` ranking beyond `ILIKE`.

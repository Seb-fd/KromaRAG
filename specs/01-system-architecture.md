# 01 — KromaRAG System Architecture

> Single source of truth for topology, data flow, DB schema, RBAC. All code must conform.

## 1. Topology

```
┌──────────────┐  HTTPS/SSE   ┌──────────────────┐  asyncpg    ┌─────────────────────┐
│ apps/web     │ ───────────► │ apps/api         │ ──────────► │ Postgres + pgvector │
│ Next.js App  │ ◄─────────── │ FastAPI (async)  │             │ DATABASE_URL only   │
│ Vercel       │  SSE stream  │ Cloud Run/Render │             │ Neon-compatible     │
└──────┬───────┘              └────┬───┬────┬────┘             └─────────────────────┘
       │ Auth.js                   │   │    │
       │ JWT (JWKS)                │   │    └──► Groq API (fallback: llama-3.3-70b)
       └───────────────────────────┘   └───────► Gemini API (primary: 2.0-flash + text-embedding-004)
                                           │
                                           └──► LangSmith (tracing, Prometheus-style token metrics)
```

- **Frontend:** Next.js App Router, React Server Components for shells, Client Components for chat/SSE. Auth.js v5 session; JWT forwarded as `Authorization: Bearer`.
- **Backend:** Python 3.12, FastAPI async, Pydantic v2, `asyncpg` pool, `sse-starlette` for streaming. Stateless; scales horizontally on Cloud Run.
- **DB:** Fully generic Postgres 15+ with `pgvector` extension. Connection **only** via `DATABASE_URL=postgresql+asyncpg://…`. No provider keys. Works on Neon (pooled), Render, self-hosted. See §3.
- **LLM routing:** Gemini primary (long context, embeddings), Groq fallback on timeout/5xx/rate-limit. Deterministic router in `services/llm_router.py`.

## 2. Data Flow

### 2.1 Query (streaming SSE, async pipeline)
1. `apps/web` → `POST /api/v1/query` with Auth.js JWT. `Accept: text/event-stream`.
2. FastAPI `Depends(get_current_principal)` validates JWT (JWKS, iss/aud/exp) → `Principal{sub, email, roles}`.
3. LangGraph run (see `03`): `router → retriever (RBAC-filtered hybrid search) → reranker → generator → guardrail`.
4. Tokens streamed as SSE `token` events; `source` events carry chunk citations; terminal `done` carries `trace_id`, token counts.
5. LangSmith background-logged; never blocks stream.

### 2.2 Ingestion (async background)
`POST /api/v1/ingest` (role: `admin|editor`) → validate → store `kroma_documents` → chunk → embed (Gemini batch) → upsert `kroma_chunks` → update `tsvector` → return `doc_id`. Large files → `BackgroundTasks` + `GET /api/v1/documents/{id}/status` polling (Phase 2: pgqueue/Redis optional).

## 3. DB Schema — Postgres + pgvector, `kroma_` prefix

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Documents (one row per ingested file/URL)
CREATE TABLE kroma_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_sub TEXT NOT NULL,               -- Auth.js JWT sub
  title TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT 'text/plain',
  uri TEXT,
  status TEXT NOT NULL DEFAULT 'ready',  -- pending|ready|failed
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Chunks (embedding + full-text)
CREATE TABLE kroma_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id UUID NOT NULL REFERENCES kroma_documents(id) ON DELETE CASCADE,
  ord INT NOT NULL,
  content TEXT NOT NULL,
  content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding VECTOR(768) NOT NULL,        -- PGVECTOR_DIM, Gemini text-embedding-004
  token_count INT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON kroma_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists=100);
CREATE INDEX ON kroma_chunks USING GIN (content_tsv);

-- RBAC
CREATE TABLE kroma_roles (name TEXT PRIMARY KEY); -- admin|editor|viewer
CREATE TABLE kroma_user_roles (user_sub TEXT NOT NULL, role TEXT NOT NULL REFERENCES kroma_roles(name), PRIMARY KEY (user_sub, role));
CREATE TABLE kroma_document_roles (doc_id UUID REFERENCES kroma_documents(id) ON DELETE CASCADE, role TEXT REFERENCES kroma_roles(name), PRIMARY KEY (doc_id, role));
-- Open doc = row per allowed role. No row = owner-only (owner_sub check).

CREATE TABLE kroma_conversations (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_sub TEXT NOT NULL, title TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE kroma_messages (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), conv_id UUID REFERENCES kroma_conversations(id) ON DELETE CASCADE, role TEXT, content TEXT, trace_id TEXT, created_at TIMESTAMPTZ DEFAULT now());
```

Neon notes: use pooled host for `DATABASE_URL`; `ivfflat` index built after bulk load (`ANALYZE`). No Supabase-specific DDL.

## 4. RBAC Access Policy

- Roles: `admin` > `editor` > `viewer`. Hierarchy enforced in code: `admin ⊇ editor ⊇ viewer`.
- JWT claim `roles: string[]` is authoritative for route gating (see `02` §5). Document visibility = `owner_sub == sub OR EXISTS(kroma_document_roles WHERE doc_id AND role = ANY(principal.roles)) OR admin`.
- Enforcement layers: (1) FastAPI dependency `require_role("editor")` → 403; (2) SQL `WHERE` filter in retriever (never post-filter in Python alone); (3) optional Postgres RLS as defense-in-depth (Phase 2).
- Auth.js is IdP; backend is stateless validator via JWKS (`JWT_ISSUER/.well-known/jwks.json`), cached 10 min. Clock skew ±60s.

## 5. Non-functionals

- p95 first-token < 2.5s (Groq fallback < 1.5s); SSE heartbeat `:ping` every 15s.
- Secrets only via env; `DATABASE_URL` never logged. CORS allowlist from `CORS_ORIGINS`.
- All cross-service contracts frozen in `02-api-contracts.md`.

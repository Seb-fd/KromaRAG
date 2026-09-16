# KromaRAG — Knowledge & Role-Based Vector Architecture

Production-grade, enterprise-ready **RAG-enabled Agentic Assistant with RBAC**.

> Spec-Driven Development (SDD): [`specs/`](./specs) is the single source of truth. No code without a spec update.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](apps/api/pyproject.toml)
[![Node >=22](https://img.shields.io/badge/node-%3E%3D22-green.svg)](package.json)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI_0.115-009688.svg)](apps/api/app/main.py)
[![Next.js 14](https://img.shields.io/badge/frontend-Next.js_14-black.svg)](apps/web/package.json)
[![LangGraph](https://img.shields.io/badge/agent-LangGraph-orange.svg)](apps/api/app/agent/graph.py)
[![Neon pgvector](https://img.shields.io/badge/db-Neon_pgvector_VECTOR(768)-336791.svg)](apps/api/migrations/001_kroma_schema.sql)
[![Gemini primary / Groq fallback](https://img.shields.io/badge/LLM-Gemini_%2B_Groq_fallback-purple.svg)](apps/api/app/agent/llm.py)
[![LangSmith](https://img.shields.io/badge/observability-LangSmith-yellow.svg)](apps/api/app/services/tracing.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey.svg)](package.json)

---

## Table of Contents

- [Quickstart / Local Development](#quickstart--local-development)
- [1. Stack & Requirements](#1-stack--requirements)
- [2. Architecture](#2-architecture)
- [3. Hybrid Search Math](#3-hybrid-search-math)
- [4. RBAC Enforcement](#4-rbac-enforcement)
- [5. Dual-Layer Citation Defense](#5-dual-layer-citation-defense-doctitleord)
- [6. Evaluation Framework](#6-evaluation-framework)
- [7. API Contracts](#7-api-contracts)
- [8. Setup](#8-setup)
  - [8.1 Prerequisites](#81-prerequisites)
  - [8.2 Environment Setup](#82-environment-setup)
  - [8.3 Database (Neon / any Postgres + pgvector)](#83-database-neon--any-postgres--pgvector)
  - [8.4 API Startup](#84-api-startup)
  - [8.5 Pytest Verification](#85-pytest-verification)
  - [8.6 Re-embedding Existing Chunks (`reembed.py`)](#86-re-embedding-existing-chunks-reembedpy)
  - [8.7 Next.js Dev Server](#87-nextjs-dev-server)
- [9. Monorepo Layout](#9-monorepo-layout)
- [10. Observability & Costs](#10-observability--costs)
- [11. SDD Workflow & Roadmap](#11-sdd-workflow--roadmap)
- [12. Troubleshooting](#12-troubleshooting)

---

## Quickstart / Local Development

Run the backend and frontend as two separate processes. See [8. Setup](#8-setup) for full environment, database, and env-var details.

Default local ports:

- **API (FastAPI):** `http://localhost:8000`
- **Web UI (Next.js):** `http://localhost:3000`

### Backend (FastAPI)

```powershell
# From apps/api with your virtual environment activated:
Set-Location apps/api
python -m uvicorn app.main:app --reload --port 8000

# Or from repo root with your virtual environment activated
# (ensure PYTHONPATH resolves `app.*`, e.g. $env:PYTHONPATH = "apps/api"):
python -m uvicorn app.main:app --reload --port 8000
```

Verify: `http://localhost:8000/health` (liveness) and `http://localhost:8000/ready` (DB readiness).

### Frontend (Next.js)

```powershell
# From apps/web:
Set-Location apps/web
npm run dev

# Or from repo root (npm workspace):
npm run dev --workspace apps/web
```

Open `http://localhost:3000`. Set `NEXT_PUBLIC_API_URL=http://localhost:8000` in `apps/web/.env.local` so the Web UI points at the local API.

---

## 1. Stack & Requirements

| Layer | Tech | Source of truth |
|---|---|---|
| Frontend | Next.js 14 App Router, React 18, Tailwind 3, Auth.js v5 — hosted Vercel | `apps/web/package.json`, `apps/web/auth.ts` |
| Backend | **Python 3.12+**, FastAPI `>=0.115` (async), Pydantic v2, `sse-starlette`, `asyncpg` — hosted Cloud Run / Render | `apps/api/pyproject.toml`, `apps/api/app/main.py` |
| Agent | LangGraph (`router → retriever → reranker → generator → guardrail → evaluator`), LangChain Core | `apps/api/app/agent/graph.py` |
| DB | Generic Postgres 15+ + `pgvector` via `DATABASE_URL=postgresql+asyncpg://…` — Neon pooled / Render / self-hosted. `kroma_` prefix, `VECTOR(768)`, `ivfflat` + `GIN` | `apps/api/migrations/001_kroma_schema.sql`, `specs/01-system-architecture.md §3` |
| Embeddings | `gemini-embedding-001` with `output_dimensionality=768` (`PGVECTOR_DIM=768`), batch 32, 3× retry, LRU-500/TTL-10min query cache | `apps/api/app/services/embeddings.py` |
| LLM | **Gemini primary** (`gemini-3.6-flash`) with **Groq fallback** (`openai/gpt-oss-120b`) on timeout / 429 / 404-NOT_FOUND / 5xx | `apps/api/app/agent/llm.py`, `apps/api/app/core/config.py` |
| Observability | LangSmith (`LANGCHAIN_PROJECT=KromaRAG`), structured JSON logs, `X-Trace-Id` | `apps/api/app/services/tracing.py`, `specs/04-observability-and-evals.md` |
| Skills | `npm autoskills` (`npx autoskills`) | `package.json` |

**Requirements:**

- Python `>=3.12` (`requires-python` in `apps/api/pyproject.toml`) + [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`.
- Node `>=22.0.0` (`engines` in root `package.json`).
- Any Postgres 15+ with `pgvector` (`vector`, `pg_trgm` extensions). No provider-specific keys — only `DATABASE_URL`.
- Keys: `GOOGLE_API_KEY` / `GEMINI_API_KEY` (AI Studio, Generative Language API enabled), `GROQ_API_KEY`, `LANGSMITH_API_KEY` (optional but required for prod tracing), `AUTH_SECRET` (≥32 chars, shared web ↔ API).

> Legacy note (verified in code): `text-embedding-004` was retired (404 `NOT_FOUND` on `v1beta` as of 2026 — see `app/services/embeddings.py`). The live default is `gemini-embedding-001`. Spec `01` still references `text-embedding-004` / `gemini-2.0-flash` / `llama-3.3-70b` as legacy values; code + `.env.example` are authoritative: `gemini-3.6-flash` + `gemini-embedding-001` + `openai/gpt-oss-120b`.

---

## 2. Architecture

Verified against `specs/01-system-architecture.md §1–§2`, `app/main.py`, `app/agent/graph.py`, `app/agent/llm.py`.

```mermaid
flowchart LR
    subgraph WEB["apps/web — Next.js (Vercel)"]
        UI[Chat UI / Upload<br/>SSE reader]
        AUTH[Auth.js v5<br/>JWT iss/aud/exp<br/>roles[]]
    end

    subgraph API["apps/api — FastAPI async (Cloud Run / Render)"]
        SEC[get_current_principal<br/>require_role<br/>JWKS + HS256]
        LG[LangGraph<br/>router → retriever → reranker<br/>→ generator → guardrail → evaluator]
        RET[hybrid_search tool<br/>0.7 cosine + 0.3 ts_rank]
        LLM[generate_with_fallback<br/>timeout 25s]
    end

    subgraph DB["Postgres + pgvector (Neon)"]
        DOCS[(kroma_documents)]
        CHUNKS[(kroma_chunks<br/>VECTOR 768 + tsvector<br/>ivfflat + GIN)]
        RBAC[(kroma_roles<br/>kroma_user_roles<br/>kroma_document_roles)]
        CONV[(kroma_conversations<br/>kroma_messages)]
    end

    subgraph EXT["Providers"]
        GEM[Gemini API<br/>primary: gemini-3.6-flash<br/>embed: gemini-embedding-001]
        GROQ[Groq API<br/>fallback: openai/gpt-oss-120b]
        LS[LangSmith<br/>project KromaRAG]
    end

    UI -- POST /api/v1/query<br/>Bearer JWT<br/>Accept: text/event-stream --> SEC
    UI -- POST /api/v1/ingest editor+<br/>GET /api/v1/documents<br/>DELETE admin --> SEC
    AUTH -- JWKS /.well-known/jwks.json<br/>HS256 AUTH_SECRET --> SEC
    SEC --> LG
    LG --> RET
    RET -- asyncpg<br/>RBAC WHERE<br/>probes=10 --> CHUNKS
    RET -- RBAC check --> DOCS
    RET -- role join --> RBAC
    LG --> LLM
    LLM -- primary --> GEM
    LLM -- timeout/429/404/5xx --> GROQ
    LG -. background trace<br/>trace_id, usage, scores .-> LS
    LG -- SSE source/token/done<br/>X-Trace-Id --> UI
    LG --> CONV
```

**Data flows (`specs/01 §2`):**

Query (streaming SSE, async pipeline):

1. `apps/web` → `POST /api/v1/query` with Auth.js JWT, `Accept: text/event-stream`.
2. FastAPI `Depends(get_current_principal)` validates JWT (JWKS, `iss`/`aud`/`exp`, skew ±60s) → `Principal{sub, email, roles}`.
3. LangGraph run: `router` (greeting/smalltalk → `direct`, else `retrieval`) → `retriever` (RBAC-filtered hybrid search, max 3 tool steps) → `reranker` (blended-score sort + citation attach) → `generator` (Gemini, fallback Groq) → `guardrail` (no-evidence refusal + citation normalization + PII redact) → `evaluator` (offline scores).
4. Tokens stream as SSE `token` events; `source` event (exactly once, first) carries chunk citations; terminal `done` carries `conv_id`, `trace_id`, token counts. Heartbeat `: ping` every 15s.
5. LangSmith logging is background-batched and never blocks the stream.

Ingestion (async background):

`POST /api/v1/ingest` (`admin|editor`) → mime-validate → extract → clean → semantic chunk → Gemini batch embed → `INSERT kroma_documents + kroma_chunks + kroma_document_roles` → update `content_tsv` (generated column) → `202 {doc_id, status: pending|ready, chunks: N}`. Large files → `BackgroundTasks` + `GET /api/v1/documents/{id}/status` polling.

---

## 3. Hybrid Search Math

Source: `specs/03-rag-and-agent-workflow.md §4`, implemented in `apps/api/app/services/retriever.py` (`HYBRID_SQL`, `build_hybrid_params`, `rerank_blended`, `hybrid_search`).

Weights (env-overridable): `HYBRID_VECTOR_W=0.7`, `HYBRID_TEXT_W=0.3` (`app/core/config.py`, `specs/03 §6`).

```sql
-- $1: query_embedding VECTOR(768), $2: plainto_tsquery string, $3: user_sub, $4: user_roles TEXT[], $5: fetch_limit (= top_k*3)
SELECT c.id AS chunk_id, c.doc_id, d.title, c.content, c.ord,
  (0.7 * (1 - (c.embedding <=> $1::vector))
   + 0.3 * coalesce(ts_rank(c.content_tsv, plainto_tsquery('english', $2)), 0)) AS score
FROM kroma_chunks c JOIN kroma_documents d ON d.id = c.doc_id
WHERE (d.owner_sub = $3 OR EXISTS (
  SELECT 1 FROM kroma_document_roles r WHERE r.doc_id = d.id AND r.role = ANY($4)
))
AND d.status = 'ready'
ORDER BY c.embedding <=> $1::vector
LIMIT $5
```

What each term means:

- `c.embedding <=> $1::vector` — pgvector **cosine distance** (`vector_cosine_ops`, `ivfflat`, `lists=100`, `SET LOCAL ivfflat.probes = 10`). Converted to similarity via `1 - distance`, weighted `0.7`.
- `ts_rank(c.content_tsv, plainto_tsquery('english', $2))` — Postgres full-text rank over the generated `content_tsv` column (`to_tsvector('english', content)`, `GIN` index), weighted `0.3`. `coalesce(..., 0)` guards NULL.
- **Two-stage retrieval:** SQL fetches `top_k * 3` candidates ordered by vector distance (`ORDER BY ... LIMIT $5`, `EXPLAIN` budget <300ms @100K chunks on Neon), then Python `rerank_blended()` sorts by the blended `score`, drops rows with `score < SCORE_MIN` (default `0.25`), and caps to final `top_k` (default 6, clamped 1–20). Empty result → guardrail emits the no-evidence refusal.
- Embeddings: query embedded once via `embed_query()` (LRU-500, TTL 10 min, keyed by normalized query), dim validated `== 768` (`ValueError` otherwise). Vector passed as pgvector-canonical literal `"[0.123456,...]"` cast `$1::vector`.

Config knobs (`TOP_K_DEFAULT=6`, `CHUNK_TOKENS=600`, `CHUNK_OVERLAP=90`, `SCORE_MIN=0.25`, `LLM_TIMEOUT_S=25`, `EMBED_BATCH=32`).

---

## 4. RBAC Enforcement

Source: `specs/01 §4`, `specs/02 §5`, `app/core/security.py`, `app/services/retriever.py`.

Roles: `admin > editor > viewer` (`admin ⊇ editor ⊇ viewer`). Hierarchy map in code: `{viewer: 1, editor: 2, admin: 3}`.

JWT (Auth.js v5 → FastAPI) — backend **MUST** validate (`get_current_principal` + `require_role`):

```jsonc
{
  "sub": "google-oauth2|108241234567890", // → kroma owner_sub / user_sub (required, else 401)
  "email": "admin@kromarag.com",
  "roles": ["viewer"],                    // ★ authoritative, ⊆ ["admin","editor","viewer"]
  "iss": "http://localhost:3000",         // == JWT_ISSUER
  "aud": "kromarag-web",                  // == JWT_AUDIENCE
  "exp": 1726400000,                      // skew ±60s
  "iat": 1726396400,
  "jti": "uuid"
}
```

1. Parse `Bearer`; fetch JWKS from `{JWT_ISSUER}/.well-known/jwks.json` (cached 600s), try RS256, then fallback HS256 with `AUTH_SECRET` (Auth.js `jwt.encode` path).
2. Verify signature, then `iss`, `aud`, `exp` (leeway 60s).
3. Normalize roles: keep only `admin|editor|viewer`; default `["viewer"]` if empty and `sub` present; missing `sub` → `401 {code: UNAUTHENTICATED}`.
4. Route gating: `require_role("editor")` for ingest/upload, `require_role("viewer")` for query/documents, `admin` or owner+editor for delete. Denial → `403 {code: FORBIDDEN, message: "requires role editor"}`.

Document visibility SQL filter (in `WHERE`, **never post-filter in Python alone**):

```sql
WHERE (d.owner_sub = $3 OR EXISTS (
  SELECT 1 FROM kroma_document_roles r
  WHERE r.doc_id = d.id AND r.role = ANY($4)  -- $4 = principal.roles TEXT[]
))
AND d.status = 'ready'
```

- Open doc = one row per allowed role in `kroma_document_roles`. No row = owner-only (`owner_sub` check).
- Same pattern guards `get_doc_metadata(doc_id)` (`ANY($3)`) and `GET /api/v1/documents` listing. Unauthorized → `403`/`404` / `None` (no leak).
- Optional Postgres RLS as defense-in-depth (Phase 2 roadmap).

---

## 5. Dual-Layer Citation Defense (`[doc:Title:ord]`)

Source: `app/agent/prompts.py` (`SYSTEM_PROMPT_V2`, `RAG_USER_TEMPLATE_V2`), `app/agent/guardrails.py` (`normalize_citations`, `apply_guardrails`), `app/agent/graph.py` (`_rag_prompt`, `node_guardrail`), tested in `tests/test_citations.py`.

Canonical form: **`[doc:<Title>:<ord>]`** — `<Title>` sanitized (no `[ ] :` / newlines → `-`, collapsed whitespace, ≤120 chars, mirrors `lib/citations.ts`), `<ord>` a plain integer chunk order (e.g. `[doc:KromaRAG Overview:0]`).

**Layer 1 — Prompt constraint (prevent):** `SYSTEM_PROMPT_V2` instructs: answer ONLY from provided chunks; inline citations MUST use exactly `[doc:<title>:<ord>]` copied verbatim from the `Valid citations` allowlist; place at end of sentence; cite every 2–3 sentences; explicit `FORBIDDEN` list — `⌗` badges, bare `Title:ord`, timestamps/decimals (`:00.50`), numeric-only `[1]`, Markdown-link citations, invented titles/ords. `_rag_prompt()` builds both the `Valid citations` allowlist and the `[doc:T:O] content` context (content truncated to 1200 chars/chunk).

**Layer 2 — Guardrail normalization (enforce):** `normalize_citations(answer, chunks)` (pure, unit-tested):

1. Converts legacy `⌗ Title:00.50` badges → `[doc:Title:ord]` when `(title, ord)` is in retrieved chunks (decimal timestamp stripped), else strips the `⌗` artifact and keeps bare title text.
2. Normalizes `[doc:Title:00.50]` → `[doc:Title:0]` when valid; drops invented `[doc:...]` / placeholder `[doc:Title:Ord]` / numeric `[1]` tags (sets `citations-normalized` flag).
3. Never touches fenced code blocks (split on ` ``` ` before rewriting); collapses leftover blank lines.
4. `apply_guardrails()` then: no chunks + answer ≠ `NO_EVIDENCE` → return `"I don't have evidence in your accessible docs to answer that."` (`no-evidence` flag); PII-redact emails / SSNs / phones ≥8 digits (`pii-redacted`, phone regex skips `[doc:T:0]`-shaped strings); truncate to 4000 chars (`truncated`).

`direct` (greeting/smalltalk) route skips citation normalization (PII redact only); `retrieval` route always passes through both layers.

---

## 6. Evaluation Framework

Source: `specs/04-observability-and-evals.md §3`, `app/agent/evaluator.py`, `app/agent/graph.py` (`node_evaluator`), `app/core/costs.py`.

Offline deterministic heuristic (CI-gating; production may swap in an LLM judge):

| Metric | Definition in `evaluator.evaluate()` | Gate |
|---|---|---|
| **Faithfulness** (hallucination) | Per-sentence: grounded if it contains `[doc:…:\d+]` OR ≥40% of its content words (stop-word filtered) overlap retrieved chunk words. `grounded / sentences`. Any `forbidden_claims` hit → `0.0`. `NO_EVIDENCE` refusal → `1.0`. | `≥ 0.85` |
| **Context recall** | Expected docs in `top_k` (dataset `expected_doc_ids` vs retrieved). Eval dataset target. | `≥ 0.8` |
| **Answer relevance** | Query↔answer content-word overlap: `min(1.0, \|qw∩aw\| / \|qw\| × 1.5)`. Maps to LLM-judge 0–1 (×5 = 1–5 scale). Empty answer → `0.0`. | judge `≥ 4/5` |
| **RBAC leakage** | `1.0 - unauthorized_chunks / total_chunks` vs `allowed_doc_ids`. Must be perfect. | `== 1.0` (0 leaked chunks, critical) |

- Dataset `kroma-evals/v1` (20–50 QAs, versioned CSV in `apps/api/evals/`): `{query, roles, expected_doc_ids, ideal_answer, forbidden_claims}`. Recall@6 ≥ 0.8 exit gate (Phase 2).
- CI gate (Phase 3+): `uv run pytest evals/` blocks merge if faithfulness < 0.85 or leakage > 0 (any unauthorized chunk).
- Every LangGraph run ends in `node_evaluator` (after guardrail) and attaches `state["eval"]`; LangSmith stores `{trace_id, user_sub_hash, roles, top_k, model, fallback_used, chunk_ids, scores}` (raw JWT / email plaintext / full doc content never logged; excerpts ≤500 chars).

---

## 7. API Contracts

Source: `specs/02-api-contracts.md`, `app/main.py`, `app/api/v1/*`. Base: `{API_URL}/api/v1`. Auth: `Authorization: Bearer <Auth.js JWT>` except `/health`, `/ready`.

| Method & Path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `GET /health` | none | — | `200 {"status":"ok","service":"kromarag-api","version":"0.1.0"}` | liveness, no DB |
| `GET /ready` | none | — | `200 {"ready":true,"db":"ok"}` / `503` | `SELECT 1` via asyncpg pool |
| `POST /api/v1/ingest` | `editor+` | `IngestRequest{title, content?, url?, mime, allowed_roles=["viewer"]}` (multipart or JSON) | `202 {"doc_id":"uuid","status":"pending\|ready","chunks":N}` | large → background |
| `POST /api/v1/query` | `viewer+` | `QueryRequest{query (1–4000 chars), top_k=6 (1–20), stream=true, conv_id?}` | `text/event-stream` SSE | see below |
| `GET /api/v1/documents` | `viewer+` | `?limit=20&offset=0` | `{"items":[DocMeta],"total":N}` | RBAC-filtered |
| `GET /api/v1/documents/{id}` | owner or role | — | `DocMeta{id,title,mime,status,chunks,created_at}` | 403/404 |
| `DELETE /api/v1/documents/{id}` | `admin` or owner+editor | — | `204` | cascade chunks |

SSE (`POST /api/v1/query`) — headers `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Trace-Id: <uuid>`:

```
event: source
data: {"chunks":[{"chunk_id":"…","doc_id":"…","title":"…","score":0.87}]}

event: token
data: {"delta":"Hello"}

event: done
data: {"conv_id":"…","trace_id":"…","usage":{"prompt_tokens":412,"completion_tokens":98,"model":"gemini-3.6-flash"}}

event: error
data: {"code":"LLM_TIMEOUT","message":"fallback to groq"}
```

Rules: `source` first (exactly once), `token` 0..N, `done` exactly once. Frontend (`apps/web/lib/api-client.ts`) uses `fetch` + `ReadableStream`, not `EventSource` (POST body). Errors: `UNAUTHENTICATED(401)`, `FORBIDDEN(403)`, `NOT_FOUND(404)`, `VALIDATION(422)`, `LLM_TIMEOUT(504)`, `DB_UNAVAILABLE(503)` — always with `trace_id` header.

---

## 8. Setup

### 8.1 Prerequisites

```powershell
# Python 3.12+ (verify)
python --version   # >= 3.12

# uv (recommended installer for the API)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Node >= 22 (verify)
node --version     # >= v22
```

Plus: a Postgres 15+ database with `pgvector` (Neon pooled host recommended), a Google AI Studio key (`GOOGLE_API_KEY`), a Groq key (`GROQ_API_KEY`), and optionally a LangSmith key.

### 8.2 Environment Setup

```powershell
# 1. Clone
git clone <repo> KromaRAG
Set-Location KromaRAG

# 2. Root env (generic DB URL — no provider keys)
Copy-Item .env.example .env
# Edit .env: DATABASE_URL, GOOGLE_API_KEY, GROQ_API_KEY, LANGSMITH_API_KEY, AUTH_SECRET
#   DATABASE_URL=postgresql+asyncpg://user:password@host:5432/kromarag
#   PGVECTOR_DIM=768
#   GEMINI_MODEL=gemini-3.6-flash / EMBEDDING_MODEL=gemini-embedding-001
#   GROQ_MODEL=openai/gpt-oss-120b / LLM_PRIMARY=gemini / LLM_FALLBACK=groq
#   JWT_ISSUER=http://localhost:3000 / JWT_AUDIENCE=kromarag-web
#   CORS_ORIGINS=http://localhost:3000 / NEXT_PUBLIC_API_URL=http://localhost:8000

# 3. API env (see apps/api/.env.example) + Web env (see apps/web/.env.local.example)
Copy-Item apps/api/.env.example apps/api/.env
Copy-Item apps/web/.env.local.example apps/web/.env.local
# Set AUTH_SECRET (same ≥32-char value in BOTH files — HS256 JWTs) + NEXT_PUBLIC_API_URL
```

`scripts/check-specs.js` enforces: `DATABASE_URL` must be generic `postgresql+asyncpg://`, no `SUPABASE_*` keys, and `02-api-contracts.md` must define `"sub"`, `"roles"`, `JWT_ISSUER`, `get_current_principal`.

### 8.3 Database (Neon / any Postgres + pgvector)

Point `DATABASE_URL` at your pooled host, then apply the idempotent schema (`apps/api/migrations/001_kroma_schema.sql`):

```powershell
# Option A — psql (strip the +asyncpg driver prefix first)
psql "postgresql://user:password@host:5432/kromarag" -f apps/api/migrations/001_kroma_schema.sql

# Option B — via API migrator
$env:PYTHONPATH = "apps/api"
uv run --project apps/api python -m app.services.migrate
```

This creates `vector` + `pg_trgm` extensions, `kroma_documents`, `kroma_chunks` (`embedding VECTOR(768)`, generated `content_tsv`, `ivfflat ... WITH (lists=100)` + `GIN(content_tsv)` indexes), `kroma_roles`/`kroma_user_roles`/`kroma_document_roles`, and `kroma_conversations`/`kroma_messages`. Build the `ivfflat` index after bulk loads, then `ANALYZE` (see `reembed.py`).

### 8.4 API Startup

```powershell
Set-Location apps/api
uv sync
uv run uvicorn app.main:app --reload --port 8000
# liveness: http://localhost:8000/health
# readiness (DB SELECT 1): http://localhost:8000/ready
```

From repo root the same server is `npm run dev:api`. CORS allowlist comes from `CORS_ORIGINS`; `DATABASE_URL` is never logged.

### 8.5 Pytest Verification

```powershell
Set-Location apps/api
uv run pytest -q
```

Offline suite (no network/DB — fakes injected), currently 7 files in `tests/`: `test_health.py`, `test_ingest.py`, `test_query.py`, `test_retriever.py` (0.7/0.3 weights + RBAC `WHERE` + `probes=10` + `SCORE_MIN`), `test_citations.py` (legacy `⌗`/decimal/invented/numeric + fence preservation), `test_agent.py` (LangGraph nodes, Gemini→Groq fallback, guardrail), `test_embeddings.py` (768-dim batch, retired-model 404 hint). Also run the SDD gate from root: `npm run spec:check` (`node scripts/check-specs.js`).

### 8.6 Re-embedding Existing Chunks (`reembed.py`)

In-place re-embed of `kroma_chunks` with `gemini-embedding-001` (`output_dimensionality=768`) — preserves `id`/`doc_id`/`ord` (and therefore citations). Needed after the `text-embedding-004` retirement or any model change.

```powershell
# From repo root (or apps/api); PYTHONPATH must expose `app.*`
$env:PYTHONPATH = "apps/api"

# Always dry-run first (embeds first batch only, no UPDATEs)
python apps/api/scripts/reembed.py --dry-run
python apps/api/scripts/reembed.py --limit 50 --dry-run

# Full run (batch 32, 0.5s sleep to avoid 429s, then REINDEX + ANALYZE)
python apps/api/scripts/reembed.py --batch-size 32

# Scoped / maintenance variants
python apps/api/scripts/reembed.py --doc-id <uuid>
python apps/api/scripts/reembed.py --no-reindex   # skip REINDEX/ANALYZE
python apps/api/scripts/reembed.py --verbose
```

Flow: preflight (`COUNT`, `vector_dims(embedding) != 768`, NULL embeddings, index check) → `SELECT id/doc_id/ord/content ORDER BY doc_id,ord` → embed batches → `UPDATE kroma_chunks SET embedding=$1::vector` → `REINDEX INDEX kroma_chunks_embedding_idx` + `ANALYZE kroma_chunks` → postflight verify. Requires `DATABASE_URL` (Neon pooler OK) + `GOOGLE_API_KEY`/`GEMINI_API_KEY`; warns if `EMBEDDING_MODEL` ≠ `gemini-embedding-001`; per-batch failures are logged without aborting the run.

### 8.7 Next.js Dev Server

```powershell
# From repo root
npm install
Copy-Item apps/web/.env.local.example apps/web/.env.local
# Set AUTH_SECRET (shared with FastAPI) + NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev --workspace apps/web
# http://localhost:3000 → /login → Continue as viewer/editor/admin (demo IdP)
```

Sign-in mints a short-lived JWT (`sub,email,roles[]`, `aud=kromarag-web`) sent as `Authorization: Bearer` on every API call. Switching roles in the sidebar re-issues the JWT — FastAPI enforces it (upload: editor+, delete: admin). Dark/light toggle in the sidebar. Production build check: `npm run build --workspace apps/web`.

---

## 9. Monorepo Layout

```
apps/web/              # Next.js 14 frontend (App Router, RSC shells + client chat/SSE)
  app/                 # routes (/login, chat, upload)
  auth.ts              # Auth.js JWT callback (roles from kroma_user_roles) + HS256/RS256 encode
  lib/api-client.ts    # fetch + ReadableStream SSE reader (source|token|done)
  lib/citations.ts     # frontend citation mirror (sanitize_title round-trip)
  components/ middleware.ts
apps/api/              # FastAPI backend (Python 3.12+, async)
  app/main.py          # lifespan (asyncpg pool warmup), CORS, /health /ready + /api/v1 routers
  app/api/v1/          # health | documents | ingest | query (SSE via sse-starlette)
  app/core/            # config (generic DATABASE_URL + RAG knobs) | security (JWKS/HS256 RBAC) | costs | logging
  app/services/        # db (pool) | embeddings (gemini-embedding-001/768) | retriever (HYBRID_SQL)
                       # ingest | migrate | tracing (LangSmith)
  app/agent/           # graph (LangGraph nodes) | llm (Gemini→Groq) | prompts (V2 + allowlist)
                       # guardrails (citation defense + PII) | evaluator (faithfulness/relevance/leakage)
  app/schemas/         # Pydantic v2 (Principal, QueryRequest, ChunkResult, IngestRequest, DocMeta)
  scripts/reembed.py   # Option B in-place re-embedder (768d, REINDEX+ANALYZE)
  migrations/001_kroma_schema.sql  # idempotent kroma_* DDL
  tests/               # test_health|ingest|query|retriever|citations|agent|embeddings
specs/                 # SDD source of truth
  01-system-architecture.md  02-api-contracts.md  03-rag-and-agent-workflow.md
  04-observability-and-evals.md  05-development-roadmap.md
scripts/check-specs.js # SDD validator (specs exist, generic DB, JWT claims)
```

Chunking (specs/03 §2, `app/rag/chunker.py`): recursive semantic split, target **600 tokens** / overlap **90** (~15%, `tiktoken cl100k`), boundaries `["\n## ","\n### ","\n\n",". ","; "]`, never split code fences, `ord` preserves order, metadata `{ord, token_count, heading_path}`, max 2000 chunks/doc. Ingestion supports `text/plain, text/markdown, application/pdf` (pypdf) + URL fetch (readability, 50KB cap); failures set `status=failed` with retry idempotent on `doc_id`.

---

## 10. Observability & Costs

- Env: `LANGCHAIN_TRACING_V2=true`, `LANGSMITH_API_KEY`, `LANGCHAIN_PROJECT=KromaRAG`, `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com`.
- Trace hierarchy per query: `kromarag.query(trace_id)` → `router` → `retriever.sql` → `embed` → `generator.gemini|groq` → `guardrail`. Metadata: `{trace_id, user_sub_hash, roles, top_k, model, fallback_used, chunk_ids, scores}`.
- SSE `done.usage` carries `{prompt_tokens, completion_tokens, model}`; per-request cost logged to `kroma_usage` (Phase 2 table) + stdout JSON. Cost table (`app/core/costs.py`, indicative per 1M, pin quarterly): `gemini-3.6-flash $1.50/$7.50`, legacy `gemini-2.0-flash $0.10/$0.40`, `groq llama-3.3-70b $0.59/$0.79`, `openai/gpt-oss-120b $0.15/$0.60`, `text-embedding-004 $0.02`. Alert if p95 cost/query > $0.02 or fallback rate > 15%.
- Structured JSON logs (`trace_id`, `route`, `latency_ms`, `model`); `ERROR` on fallback, 5xx, RBAC-deny bursts. Non-functionals: p95 first-token < 2.5s (Groq fallback < 1.5s), p95 retrieval < 400ms.

---

## 11. SDD Workflow & Roadmap

1. Every PR must reference a `specs/0X-*.md` section; contracts in `specs/02` must match `apps/api/app/schemas/*` + `core/security.py`.
2. Run: `uv run pytest -q` (api) + `npm run build --workspace apps/web` + `npm run spec:check`.
3. Manual check: `GET /health`, `POST /api/v1/query` SSE stream, RBAC denial for unauthorized role, fallback chaos test (break Gemini → Groq still answers).

Phases (`specs/05`): **1** Setup & Ingestion → **2** Hybrid Retriever (recall@6 ≥ 0.8, RBAC leakage test) → **3** LangGraph Agent (SSE citations, faithfulness ≥ 0.85, 403 on bad role) → **4** Frontend Integration (E2E login→query→cite→upload, Lighthouse ≥ 85, pooled prod `DATABASE_URL`). Risks: Neon cold start → pooled + `ready` warmup; Gemini quota → Groq auto-fallback; Auth.js clock skew → ±60s leeway.

Utilize `autoskills` (auto-detects stack from `package.json`):

```powershell
npx autoskills --dry-run   # preview (no writes)
npx autoskills -y          # install all detected skills
npx autoskills             # interactive selector
```

Requires Node >= 22. Skills land in `.claude/skills/` etc. (gitignored) + generates `CLAUDE.md`. Re-run after adding deps. See https://www.autoskills.sh / `npx autoskills -h`.

---

## 12. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `embedding model 'text-embedding-004' not found (404 NOT_FOUND)` | Retired model. Set `EMBEDDING_MODEL=gemini-embedding-001` in `apps/api/.env`, then run `reembed.py --dry-run` → full `reembed.py`. |
| `missing GOOGLE_API_KEY/GEMINI_API_KEY` / invalid-key on embed | Use an AI Studio key (typically `AIza…`) with Generative Language API enabled. Vertex/OAuth tokens need `genai.Client(vertexai=True, …)` instead of `api_key` auth. |
| `vector_dims(embedding) != 768` rows in preflight | Mixed-dimension corpus. Run full `reembed.py` (no `--limit`), then verify postflight = 0 mismatches. |
| Gemini 404 / quota → answers stop | Expected path: `generate_with_fallback` retries on 404/429/5xx/timeout and fails over to Groq (`fallback_used=true`, `event: error {code: LLM_TIMEOUT}`). Check `GROQ_API_KEY` + `X-Trace-Id` logs. |
| `401 UNAUTHENTICATED` / `403 FORBIDDEN` | Check `AUTH_SECRET` matches web ↔ API, `JWT_ISSUER`/`JWT_AUDIENCE`, clock skew (±60s), and `roles[]` on the JWT (viewer can't `POST /ingest`; delete needs admin/owner). |
| Slow retrieval / `ivfflat` warnings | Ensure index built after bulk load + `ANALYZE kroma_chunks`; `reembed.py` does `REINDEX` + `ANALYZE` automatically. Use Neon pooled host; check `ready` warmup. |

---

## Docs

- `specs/01-system-architecture.md` — topology, SSE/async, `kroma_` schema, RBAC
- `specs/02-api-contracts.md` — OpenAPI, SSE events, Pydantic, Auth.js JWT claims
- `specs/03-rag-and-agent-workflow.md` — ingestion, hybrid search, LangGraph nodes
- `specs/04-observability-and-evals.md` — LangSmith, cost, evals
- `specs/05-development-roadmap.md` — 4-phase milestones

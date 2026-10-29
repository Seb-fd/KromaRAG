# KromaRAG — Knowledge & Role-Based Vector Architecture

Production-grade, enterprise-ready **RAG-enabled Agentic Assistant with RBAC**.

> Spec-Driven Development (SDD): [`specs/`](./specs) is the single source of truth. No code without a spec update. Currently 9 specs: `01–05` (core) + `06` (conversations) + `07` (document lifecycle) + `08` (prod hardening) + `09` (telemetry & ops).

[![CI](https://github.com/Seb-fd/KromaRAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Seb-fd/KromaRAG/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](apps/api/pyproject.toml)
[![Node >=22](https://img.shields.io/badge/node-%3E%3D22-green.svg)](package.json)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI_0.115-009688.svg)](apps/api/app/main.py)
[![Next.js 16](https://img.shields.io/badge/frontend-Next.js_16-black.svg)](apps/web/package.json)
[![React 19](https://img.shields.io/badge/frontend-React_19-black.svg)](apps/web/package.json)
[![LangGraph](https://img.shields.io/badge/agent-LangGraph-orange.svg)](apps/api/app/agent/graph.py)
[![Neon pgvector](https://img.shields.io/badge/db-Neon_pgvector_VECTOR(768)-336791.svg)](apps/api/migrations/001_kroma_schema.sql)
[![Gemini primary / Groq fallback](https://img.shields.io/badge/LLM-Gemini_%2B_Groq_fallback-purple.svg)](apps/api/app/agent/llm.py)
[![LangSmith](https://img.shields.io/badge/observability-LangSmith-yellow.svg)](apps/api/app/services/tracing.py)
[![Cloud Run + Vercel](https://img.shields.io/badge/deploy-Docker_%2B_Vercel-blue.svg)](apps/api/Dockerfile)
[![k6 load](https://img.shields.io/badge/load-k6-orange.svg)](loadtests/k6/query_sse.js)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey.svg)](LICENSE)

---

## Portfolio / Demo (30-second pitch)

Chat over your private documents with **grounded answers, citations, and role-based access**. Upload docs → ask questions → get streamed answers with `[doc:Title:ord]` badges that open the exact source chunk. Three roles (`admin > editor > viewer`) enforced in SQL, not just UI.

**Why it's production-shaped:** spec-driven (`specs/` is source of truth, CI-gated), hybrid vector + full-text retrieval, deterministic Gemini → Groq fallback, SSE streaming with trace IDs, LangSmith tracing + Prometheus metrics, 166 backend + 28 frontend tests (coverage 75%), 12 required CI jobs (pytest/ruff/mypy/pip-audit/eslint/tsc/vitest/build/audit/docker/gitleaks), reproducible `uv.lock` builds, Docker + staging topology (Render API + Vercel web).

| | |
|---|---|
| **Live demo** | `https://kroma-rag.vercel.app` (API `https://kromarag-api-staging.onrender.com` — wake cold start, then smoke: `scripts/deploy/smoke.sh`) |
| **Demo login** | Clerk Google SSO: `viewer` (read-only) vs `editor` (upload + ask) vs `admin` (delete + `/metrics`); roles in dashboard `publicMetadata.roles` |
| **Try locally** | Backend `http://localhost:8000/docs`, web `http://localhost:3000` — see [Quickstart](#quickstart--local-development) |
| **Screenshots** | See [`docs/demo-checklist.md`](docs/demo-checklist.md) — capture chat + documents at 388px and 1280px after staging is green |

Recruiters: start at [2. Architecture](#2-architecture), then [10. Production Hardening](#10-production-hardening--deployment-spec08) for the deploy story.

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
- [8. Conversations & History](#8-conversations--history-spec06)
- [9. Document Lifecycle](#9-document-lifecycle-spec07)
- [10. Production Hardening & Deployment](#10-production-hardening--deployment-spec08)
- [11. Telemetry & Operations](#11-telemetry--operations-spec09)
- [12. Setup](#12-setup)
  - [12.1 Prerequisites](#121-prerequisites)
  - [12.2 Environment Setup](#122-environment-setup)
  - [12.3 Database (Neon / any Postgres + pgvector)](#123-database-neon--any-postgres--pgvector)
  - [12.4 API Startup](#124-api-startup)
  - [12.5 Pytest Verification](#125-pytest-verification)
  - [12.6 Re-embedding Existing Chunks (`reembed.py`)](#126-re-embedding-existing-chunks-reembedpy)
  - [12.7 Next.js Dev Server](#127-nextjs-dev-server)
  - [12.8 Docker / Prod Parity](#128-docker--prod-parity)
  - [12.9 Load Tests (k6)](#129-load-tests-k6)
- [13. Monorepo Layout](#13-monorepo-layout)
- [14. Observability & Costs](#14-observability--costs)
- [15. SDD Workflow & Roadmap](#15-sdd-workflow--roadmap)
- [16. Troubleshooting](#16-troubleshooting)

---

## Quickstart / Local Development

Run the backend and frontend as two separate processes. See [12. Setup](#12-setup) for full environment, database, and env-var details.

Default local ports:

- **API (FastAPI):** `http://localhost:8000` (or `http://127.0.0.1:8000`)
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

Verify: `http://localhost:8000/health` (liveness, alias `/healthz`) and `http://localhost:8000/ready` (DB readiness, alias `/readyz`).

### Frontend (Next.js)

```powershell
# From apps/web:
Set-Location apps/web
npm run dev

# Or from repo root (npm workspace):
npm run dev --workspace apps/web
```

Open `http://localhost:3000`. Set `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000` (or `http://localhost:8000`) in `apps/web/.env.local` so the Web UI points at the local API. In dev, `NEXT_PUBLIC_USE_PROXY=1` routes browser calls through the Next.js same-origin rewrite proxy (`next.config.js` `rewrites()`), bypassing CORS/CSP/IPv6 loopback issues; in prod (Vercel) set `0`/unset and call Cloud Run directly.

---

## 1. Stack & Requirements

| Layer | Tech | Source of truth |
|---|---|---|
| Frontend | Next.js 16 App Router, React 19, Tailwind 3, Clerk (`kromarag-dev` / `kromarag-prod`) — hosted Vercel | `apps/web/package.json`, `apps/web/proxy.ts`, `apps/web/lib/kroma-token.ts`, `apps/web/next.config.js` |
| Backend | **Python 3.12+**, FastAPI `>=0.115` (async), Pydantic v2, `sse-starlette`, `asyncpg` — hosted Cloud Run | `apps/api/pyproject.toml`, `apps/api/app/main.py` |
| Agent | LangGraph (`router → retriever → reranker → generator → guardrail → evaluator`), LangChain Core | `apps/api/app/agent/graph.py` |
| DB | Generic Postgres 15+ + `pgvector` via `DATABASE_URL=postgresql+asyncpg://…` — Neon pooled / Render / self-hosted. `kroma_` prefix, `VECTOR(768)`, `ivfflat` + `GIN` | `apps/api/migrations/001_kroma_schema.sql`, `specs/01-system-architecture.md §3` |
| Migrations | `001` base schema → `002` conversations/history → `003` documents lifecycle (idempotent, advisory-locked) | `apps/api/migrations/`, `apps/api/app/services/migrate.py` |
| Embeddings | `gemini-embedding-001` with `output_dimensionality=768` (`PGVECTOR_DIM=768`), batch 32, 3× retry, LRU-500/TTL-10min query cache | `apps/api/app/services/embeddings.py` |
| LLM | **Gemini primary** (`gemini-3.6-flash`) with **Groq fallback** (`openai/gpt-oss-120b`) on timeout / 429 / 404-NOT_FOUND / 5xx | `apps/api/app/agent/llm.py`, `apps/api/app/core/config.py` |
| Observability | LangSmith (`LANGCHAIN_PROJECT=KromaRAG`), W3C `traceparent` join/echo, Prometheus `GET /metrics` (admin + allowlist), structured JSON logs, `X-Trace-Id` | `apps/api/app/services/tracing.py`, `apps/api/app/core/middleware.py`, `apps/api/app/api/v1/metrics.py`, `specs/04-observability-and-evals.md`, `specs/09_telemetry_and_operations_spec.md` |
| Load | k6 normative (`query_sse.js`, `upload_burst.js`, `soak.js`) | `loadtests/k6/`, `specs/09 §3.4` |
| Skills | `npm autoskills` (`npx autoskills`) | `package.json` |

**Requirements:**

- Python `>=3.12` (`requires-python` in `apps/api/pyproject.toml`) + [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`.
- Node `>=22.0.0` (`engines` in root `package.json`).
- Any Postgres 15+ with `pgvector` (`vector`, `pg_trgm` extensions). No provider-specific keys — only `DATABASE_URL`.
- Keys: `GOOGLE_API_KEY` / `GEMINI_API_KEY` (AI Studio, Generative Language API enabled), `GROQ_API_KEY`, `LANGSMITH_API_KEY` (optional but required for prod tracing), Clerk `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` + server-only `CLERK_SECRET_KEY` (per app: `kromarag-dev` / `kromarag-prod`), plus dev-only `AUTH_SECRET` (same value in `apps/web/.env.local` + `apps/api/.env` for the non-prod HS256 bypass; never Vercel/Cloud Run).
- k6 binary only for load runs (`loadtests/k6/`).

> Legacy note (verified in code): `text-embedding-004` was retired (404 `NOT_FOUND` on `v1beta` as of 2026 — see `app/services/embeddings.py`). The live default is `gemini-embedding-001`. Legacy values `text-embedding-004` / `gemini-2.0-flash` / `llama-3.3-70b(-versatile)` appear in specs `01/03/04` history and `app/core/costs.py` for cost continuity; code + `.env.example` are authoritative: `gemini-3.6-flash` + `gemini-embedding-001` + `openai/gpt-oss-120b`.

---

## 2. Architecture

Verified against `specs/01-system-architecture.md §1–§2`, `app/main.py`, `app/agent/graph.py`, `app/agent/llm.py`, `specs/06–09`.

```mermaid
flowchart LR
    subgraph WEB["apps/web — Next.js (Vercel)"]
        UI["Chat UI / Upload<br/>SSE reader"]
        AUTH["Clerk session<br/>template kromarag<br/>email+roles+aud"]
    end

    subgraph API["apps/api — FastAPI async (Cloud Run)"]
        SEC["get_current_principal<br/>require_role<br/>Clerk JWKS RS256"]
        RL["RateLimit + SecurityHeaders<br/>CORS exact allowlist"]
        LG["LangGraph<br/>router → retriever → reranker<br/>→ generator → guardrail → evaluator"]
        RET["hybrid_search tool<br/>0.7 cosine + 0.3 ts_rank"]
        LLM["generate_with_fallback<br/>timeout 25s"]
        CONV["conversations<br/>threads + history"]
        DOCSAPI["documents lifecycle<br/>upload/patch/reindex/purge"]
        MET["GET /metrics<br/>admin + allowlist"]
    end

    subgraph DB["Postgres + pgvector (Neon)"]
        DOCS[("kroma_documents<br/>status: pending/indexing/indexed/failed/archived")]
        CHUNKS[("kroma_chunks<br/>VECTOR 768 + tsvector<br/>ivfflat + GIN")]
        RBAC[("kroma_roles<br/>kroma_user_roles<br/>kroma_document_roles")]
        CONVDB[("kroma_conversations<br/>kroma_messages")]
    end

    subgraph EXT["Providers"]
        GEM["Gemini API<br/>primary: gemini-3.6-flash<br/>embed: gemini-embedding-001"]
        GROQ["Groq API<br/>fallback: openai/gpt-oss-120b"]
        LS["LangSmith<br/>project KromaRAG"]
    end

    UI -- "POST /api/v1/query<br/>Bearer Clerk JWT<br/>Accept: text/event-stream" --> SEC
    UI -- "POST /api/v1/ingest editor+<br/>POST /documents/upload<br/>GET /documents, /conversations" --> SEC
    AUTH -- "JWKS /.well-known/jwks.json<br/>RS256 kid (+prev-set)" --> SEC
    SEC --> RL
    RL --> LG
    LG --> RET
    RET -- "asyncpg<br/>RBAC WHERE<br/>probes=10" --> CHUNKS
    RET -- "RBAC check" --> DOCS
    RET -- "role join" --> RBAC
    LG --> LLM
    LLM -- "primary" --> GEM
    LLM -- "timeout/429/404/5xx" --> GROQ
    LG -. "background trace<br/>trace_id, usage, scores" .-> LS
    LG -- "SSE source/token/done<br/>X-Trace-Id + traceparent" --> UI
    LG --> CONV
    DOCSAPI --> DOCS
    MET -. "scrape<br/>admin JWT + allowlist" .-> UI
```

**Data flows (`specs/01 §2`, extended by `06 §4`, `07 §4`, `09 §3.1`):**

Query (streaming SSE, async pipeline):

1. `apps/web` → `POST /api/v1/query` with Auth.js JWT, `Accept: text/event-stream`, optional W3C `traceparent` (joined to app `trace_id`, echoed back as `X-Trace-Id` + `traceparent`).
2. FastAPI `Depends(get_current_principal)` validates JWT (JWKS, `iss`/`aud`/`exp`, skew ±60s) → `Principal{sub, email, roles}`. Tiered rate limits apply (`query 100/min/sub`, `admin 300/min`, `unauth 10/min/IP`).
3. LangGraph run: `router` (greeting/smalltalk → `direct`, else `retrieval`) → history load (last-N turns, prompt-only) → `retriever` (RBAC-filtered hybrid search, only `status='indexed'`, max 3 tool steps) → `reranker` (blended-score sort + citation attach) → `generator` (Gemini, fallback Groq) → `guardrail` (no-evidence refusal + citation normalization + PII redact) → `evaluator` (offline scores).
4. Tokens stream as SSE `token` events; `source` event (exactly once, first) carries chunk citations; terminal `done` carries `conv_id`, `trace_id`, `traceparent`, token counts, `history_tokens`. Heartbeat `: ping` every 15s.
5. LangSmith logging is background-batched and never blocks the stream. History pair `(user, assistant)` persisted atomically after terminal `done` (idempotent on `trace_id`).

Ingestion (async background):

`POST /api/v1/ingest` (`admin|editor`, JSON) or `POST /api/v1/documents/upload` (`editor+`, multipart) → mime-validate → extract → clean → semantic chunk → Gemini batch embed → `INSERT kroma_documents + kroma_chunks + kroma_document_roles` → update `content_tsv` (generated column) → `202 {doc_id, status: pending|indexing|indexed, chunks, job_id}`. Large files → `BackgroundTasks` + `GET /api/v1/documents/{id}/status` polling. Re-chunk/re-embed via `POST /{id}/reindex` (reserves `version+1`, `409` if in flight).

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
AND d.status = 'indexed'  -- was 'ready' pre-003; legacy alias still accepted on reads
ORDER BY c.embedding <=> $1::vector
LIMIT $5
```

What each term means:

- `c.embedding <=> $1::vector` — pgvector **cosine distance** (`vector_cosine_ops`, `ivfflat`, `lists=100`, `SET LOCAL ivfflat.probes = 10`). Converted to similarity via `1 - distance`, weighted `0.7`.
- `ts_rank(c.content_tsv, plainto_tsquery('english', $2))` — Postgres full-text rank over the generated `content_tsv` column (`to_tsvector('english', content)`, `GIN` index), weighted `0.3`. `coalesce(..., 0)` guards NULL.
- **Two-stage retrieval:** SQL fetches `top_k * 3` candidates ordered by vector distance (`ORDER BY ... LIMIT $5`, `EXPLAIN` budget <300ms @100K chunks on Neon), then Python `rerank_blended()` sorts by the blended `score`, drops rows with `score < SCORE_MIN` (default `0.25`), and caps to final `top_k` (default 6, clamped 1–20). Empty result → guardrail emits the no-evidence refusal. Retriever SQL has `SET LOCAL statement_timeout=3000ms` in prod (`RETRIEVER_TIMEOUT_MS`).
- Embeddings: query embedded once via `embed_query()` (LRU-500, TTL 10 min, keyed by normalized query), dim validated `== 768` (`ValueError` otherwise). Vector passed as pgvector-canonical literal `"[0.123456,...]"` cast `$1::vector`.

Config knobs (`TOP_K_DEFAULT=6`, `CHUNK_TOKENS=600`, `CHUNK_OVERLAP=90`, `SCORE_MIN=0.25`, `LLM_TIMEOUT_S=25`, `EMBED_BATCH=32`, `EMBED_TIMEOUT_S=8`, `LLM_FALLBACK_TIMEOUT_S=12`).

---

## 4. RBAC Enforcement

Source: `specs/01 §4`, `specs/02 §5`, `app/core/security.py`, `app/services/retriever.py`, extended by `specs/06 §3.1` + `specs/07 §3.1` (leak-safe `404`).

Roles: `admin > editor > viewer` (`admin ⊇ editor ⊇ viewer`). Hierarchy map in code: `{viewer: 1, editor: 2, admin: 3}`.

JWT (Clerk `kromarag` template → FastAPI) — backend **MUST** validate (`get_current_principal` + `require_role`):

```jsonc
{
  "sub": "user_2abcXYZ123",               // → kroma owner_sub / user_sub (required, else 401)
  "email": "admin@kromarag.com",          // top-level from the template
  "roles": ["viewer"],                    // ★ authoritative, ⊆ ["admin","editor","viewer"]
  "iss": "https://kromarag-prod.clerk.accounts.dev", // == JWT_ISSUER (dev app issuer for staging)
  "aud": "kromarag-web",                  // == JWT_AUDIENCE (template carries it)
  "exp": 1726400000,                      // skew ±60s
  "iat": 1726396400,
  "jti": "uuid"
}
```

Roles live in Clerk `publicMetadata.roles` (Dashboard → Users → Metadata; default `["viewer"]`).
One-time cutover note: old `demo|email` / `google-oauth2|…` subs orphan — backfill
`owner_sub`/`user_sub` by email from the Clerk user export (see `scripts/deploy/README.md`).

1. Parse `Bearer`; fetch JWKS from `{JWT_ISSUER}/.well-known/jwks.json` (cached 600s + prev-set fallback), verify RS256 `kid`. HS256 dev-bypass only when NOT prod (prod rejects kid-less tokens outright).
2. Verify signature, then `iss`, `aud`, `exp` (leeway 60s).
3. Normalize roles: keep only `admin|editor|viewer`; default `["viewer"]` if empty and `sub` present; missing `sub` → `401 {code: UNAUTHENTICATED}`.
4. Route gating: `require_role("editor")` for ingest/upload/reindex-archive family, `require_role("viewer")` for query/documents/conversations, `admin` or owner+editor for delete paths. Denial → `403 {code: FORBIDDEN, message: "requires role editor"}`. `403` is reserved for role gates; existence probes return `404`.
5. Tiered rate limits run as middleware (`app/core/rate_limit.py`, `app/core/middleware.py`): `query 100/min/sub` (`300` admin), `ingest/upload/reindex 20/min/sub`, `read 300/min/sub`, `unauth 10/min/IP`. Breach → `429 {code: RATE_LIMITED}` + `Retry-After` + `X-RateLimit-*`. Probes (`/health*`, `/ready*`) and `/metrics` scrape are exempt.

Document/thread visibility SQL filter (in `WHERE`, **never post-filter in Python alone**):

```sql
-- Documents (spec 07 §3.1): $1=user_sub, $2/$3=user_roles TEXT[]
WHERE (d.owner_sub = $1 OR 'admin' = ANY($2) OR EXISTS (
  SELECT 1 FROM kroma_document_roles r
  WHERE r.doc_id = d.id AND r.role = ANY($2)
))
-- Conversations (spec 06 §3.1): owner-scoped, leak-safe
SELECT id FROM kroma_conversations WHERE id = $1 AND user_sub = $2;
-- Miss → 404 {"code":"NOT_FOUND"} (never 403, byte-identical to random UUID)
```

- Open doc = one row per allowed role in `kroma_document_roles`. No row = owner-only (`owner_sub` check).
- Same pattern guards `get_doc_metadata(doc_id)` and `GET /api/v1/documents` listing. Unauthorized → `403`/`404` / `None` (no leak).
- Operation matrix (`07 §4.6`): patch/reindex/archive = owner+`editor+` or `admin`; purge = owner (any role) or `admin`; `owner_sub` transfer = `admin`-only (non-admin probe → `404`, not `403`).
- Optional Postgres RLS as defense-in-depth (deferred to future hardening).

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

`direct` (greeting/smalltalk) route skips citation normalization (PII redact only); `retrieval` route always passes through both layers. History context (spec 06) is never citable as `[doc:...]`; chunks win on conflict.

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

Source: `specs/02-api-contracts.md` + `specs/06 §3` + `specs/07 §3` + `specs/08 §3.4` + `specs/09 §3.2`, `app/main.py`, `app/api/v1/*`. Base: `{API_URL}/api/v1` (ops plane `/health*`, `/ready*`, `/metrics` at root). Auth: `Authorization: Bearer <Clerk JWT (template kromarag)>` except `/health*`, `/ready*`.

| Method & Path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `GET /health`, `GET /healthz` | none, rate-limit exempt | — | `200 {"status":"ok","service":"kromarag-api","version":"0.1.0"}` | liveness, no DB, p95 <50ms |
| `GET /ready`, `GET /readyz` | none, rate-limit exempt | — | `200 {"ready":true,"db":"ok","pool":{...},"models":{...}}` / `503` | `SELECT 1` via asyncpg pool |
| `GET /metrics` | `admin` JWT **AND** scraper allowlist | — | Prometheus `0.0.4` text | dual-gated; external → `404`, non-admin → `403`; see §11 |
| `POST /api/v1/ingest` | `editor+` | `IngestRequest{title, content?, url?, mime, allowed_roles=["viewer"]}` (JSON) | `202 {"doc_id":"uuid","status":"pending\|indexing\|indexed","chunks":N}` | legacy path kept; `ready→indexed` |
| `POST /api/v1/documents/upload` | `editor+` | `multipart: file + title? + allowed_roles? + chunk_tokens? + chunk_overlap?` | `202 {doc_id, status, chunks:0, job_id}` | complements `/ingest`; 20 MB cap → `413` |
| `POST /api/v1/query` | `viewer+` | `QueryRequest{query (1–4000 chars), top_k=6 (1–20), stream=true, conv_id?}` | `text/event-stream` SSE or JSON | see below; `conv_id` tightened (06) |
| `GET /api/v1/documents?limit&offset&status&mime&q` | `viewer+` | filters | `{items:[DocumentDetail],total:N}` | RBAC-filtered; default hides `archived` |
| `GET /api/v1/documents/{id}` | visible only | — | `DocumentDetail` | non-visible → uniform `404` |
| `GET /api/v1/documents/{id}/status` | visible only | — | `{doc_id,status,chunks,version,error,updated_at}` | lightweight poll, p95 <100ms |
| `PATCH /api/v1/documents/{id}` | owner+`editor+` or `admin` | `{title?, allowed_roles?, owner_sub? (admin-only), uri?}` | `DocumentDetail` | metadata-only, never bumps `version` |
| `DELETE /api/v1/documents/{id}` | owner+`editor+` or `admin` | — | `204` | soft-archive (`archived`), retains chunks |
| `POST /api/v1/documents/{id}/reindex` | owner+`editor+` or `admin` | `{chunk_tokens?, chunk_overlap?, embedding_model?}` | `202 {doc_id,job_id,status:"indexing",version}` | async; in-flight → `409` |
| `POST /api/v1/documents/{id}/purge` | owner or `admin` | — | `204` | hard-delete CASCADE; `pending/indexing` → `409` |
| `POST /api/v1/conversations` | `viewer+` | `{title?}` | `201 ConversationMeta` | explicit thread create |
| `GET /api/v1/conversations?limit&offset` | `viewer+` | — | `{items:[ConversationMeta],total:N}` | owner-scoped, newest-first |
| `GET /api/v1/conversations/{id}` | owner | — | `ConversationMeta` | non-owner → `404` (never `403`) |
| `PATCH /api/v1/conversations/{id}` | owner | `{title}` | `ConversationMeta` | rename; blank → `422` |
| `DELETE /api/v1/conversations/{id}` | owner | — | `204` | hard-delete cascade |
| `GET /api/v1/conversations/{id}/messages?limit&before_id` | owner | keyset cursor | `{items asc, next_before_id}` | replay after SSE disconnect |
| `GET /api/v1/documents/{id}` (legacy alias) | owner or role | — | `DocMeta` | 403/404 |
| `DELETE /api/v1/documents/{id}` (legacy form) | `admin` or owner+editor | — | `204` | now soft-archive per 07 |

SSE (`POST /api/v1/query`) — headers `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Trace-Id: <uuid>`, `traceparent: 00-...`:

```
event: source
data: {"chunks":[{"chunk_id":"…","doc_id":"…","title":"…","score":0.87}]}

event: token
data: {"delta":"Hello"}

event: done
data: {"conv_id":"…","trace_id":"…","traceparent":"00-…","history_tokens":210,"usage":{"prompt_tokens":412,"completion_tokens":98,"model":"gemini-3.6-flash"}}

event: error
data: {"code":"LLM_TIMEOUT","message":"fallback to groq"}
```

Rules: `source` first (exactly once), `token` 0..N, `done` exactly once. Frontend (`apps/web/lib/api-client.ts`) uses `fetch` + `ReadableStream`, not `EventSource` (POST body). Errors: `UNAUTHENTICATED(401)`, `FORBIDDEN(403)`, `NOT_FOUND(404)`, `VALIDATION(422)`, `PAYLOAD_TOO_LARGE(413)`, `REINDEX_IN_PROGRESS / ARCHIVE_CONFLICT (409)`, `RATE_LIMITED(429)`, `LLM_TIMEOUT(504)`, `DB_UNAVAILABLE(503)`, `METRICS_UNAVAILABLE(503)` — always with `X-Trace-Id` (+ `traceparent`).

---

## 8. Conversations & History (spec 06)

Source: `specs/06_conversations_and_history_spec.md`, `app/api/v1/conversations.py`, `app/services/conversations.py`, `migrations/002_conversations_history.sql`.

- Threads created explicitly (`POST /conversations`) or implicitly on first `POST /query` without `conv_id` (server mints UUIDv4, auto-titles from first query truncated to 60 chars).
- **All turns persist** — both `direct` and `retrieval` routes — as `(user, assistant)` pairs after terminal `done` (single transaction, idempotent on `trace_id` unique index). Total-LLM-failure still persists the friendly turn; pre-`source` exceptions persist nothing.
- History injection: last-N turns (`HISTORY_MAX_TURNS=10`, `HISTORY_MAX_CHARS=6000`, `HISTORY_MAX_TOKENS=1500`, drop-oldest-first) loaded verbatim and injected **after chunks, before current query** in `node_generator`. Retriever embeds current query only (unit-test enforced); history never cited as `[doc:...]`; chunks win on conflict.
- Ownership: every thread/message SQL carries `user_sub` predicate; cross-tenant access → uniform `404` (never `403`). `GET messages` uses keyset cursor (`limit` 1–100, `before_id` must belong to thread).
- Frontend: `store.tsx` migrated from `localStorage kromarag.convs.v1` to API threads (local cache read-through, server authoritative); `api-client.ts` gains `create/list/get/rename/deleteConversationThread` + `getMessages`; `streamQuery` accepts `conv_id`.

---

## 9. Document Lifecycle (spec 07)

Source: `specs/07_document_lifecycle_mgmt_spec.md`, `app/api/v1/documents.py`, `app/services/ingest.py`, `migrations/003_kroma_documents_lifecycle.sql`.

- Two ingest paths: `POST /ingest` (JSON `content|url`, legacy kept) **complements** `POST /documents/upload` (multipart binary, server-side `pypdf` for PDFs). MIME resolution: explicit field > `content_type` > extension; unsupported → `422` before DB write. Server cap `DOC_UPLOAD_MAX_BYTES=20 MB` (`413`); web pre-guard 2 MB.
- State machine (lowercase DB values): `pending → indexing → indexed ⇄ archived`, `pending/indexing → failed → indexing` (retry via reindex). Only `indexed` is retrieval-eligible (`AND d.status='indexed'`; legacy `ready` migrated to `indexed` by `003`). `PATCH` (title/roles/`owner_sub`) never changes `status`/`version`.
- Reindex: `POST /{id}/reindex` reserves `version+1` + `status='indexing'` synchronously, re-chunks/re-embeds in `BackgroundTasks` (single-transaction chunk swap, `ANALYZE` after large docs), then `→indexed` (or `→failed` with truncated `error`). Concurrent → `409 REINDEX_IN_PROGRESS`; `archived/failed` restore via reindex (clears `archived_at`/`error`). Model pinned to `gemini-embedding-001`/768 Phase 1 (mismatch → `422 EMBEDDING_MISMATCH`).
- Archive vs purge: `DELETE /{id}` = soft-archive (`archived_at`, chunks retained, hidden from default list + retrieval, idempotent); `POST /{id}/purge` = irreversible hard-delete (`DELETE` + CASCADE, orphan check `= 0`). Purge while `pending/indexing` → `409 ARCHIVE_CONFLICT`.
- `GET /documents` filters `status|mime|q` (title `ILIKE`), `GET /{id}/status` lightweight poll; `DocumentDetail` carries `chunks/version/chunk_config/embedding_model/error≤500 chars`.
- Frontend: `Documents.tsx` binary-PDF upload via `FormData`, `UploadJob` machine (`reading→uploading→indexing→done|error`), status filter chips, `Reindex/Archive/Purge` actions gated by `(owner && editor+) || admin`.

---

## 10. Production Hardening & Deployment (spec 08)

Source: `specs/08_production_hardening_and_deployment_spec.md`, `app/main.py`, `app/core/{config,security,middleware,rate_limit}.py`, `apps/api/Dockerfile`, `apps/api/scripts/docker-entrypoint.sh`.

- Topology normative: **Cloud Run (API) + Vercel (Web) + Neon pooled pgBouncer**. Docker multi-stage is the build artifact (no separate Render target; `apps/web/Dockerfile` is fallback-only — web builds natively on Vercel).
- Containers: `apps/api/Dockerfile` (uv build → slim runtime, non-root `appuser`, `migrate → pool warmup → exec CMD`, `HEALTHCHECK /healthz`, single Uvicorn `--workers 1`, honors `$PORT`, `STOPSIGNAL SIGTERM`). Tags immutable `:git-<sha>` + floating `:prod`.
- API posture: `TrustedHost → CORS (exact allowlist, `max_age=600`, no `*` in prod) → RateLimit → Auth` request order; every response carries security headers (`HSTS`, restrictive `CSP frame-ancestors 'none'`, `nosniff`, `DENY`, `Referrer-Policy`, `COOP/CORP`, `Permissions-Policy`), `X-Trace-Id`, `X-RateLimit-*`. Web `next.config.js` mirrors headers + `output:standalone` + same-origin `rewrites()` for dev.
- DB: prod runtime **only** via pooled DSN (`-pooler` + `sslmode=require`, fail-closed boot guard); one-shot jobs use direct `DATABASE_URL_SYNC`. Pool `min=2/max=20/command_timeout=10s/statement_cache_size=0` (pgBouncer txn mode) + `pool_recycle=300s`. Pool exhaust → `503 DB_UNAVAILABLE + Retry-After: 5`; `readyz` flips `503`, `healthz` stays `200`.
- LLM budgets: `25s` total = `embed ≤8s + SQL ≤3s + primary ≤15s`, fallback `≤12s` inside stream. Exactly one Groq attempt on retryable `LLMError` (`timeout/429/404-shape/5xx`); `fallback_used=true` + mid-stream informational `error` event + `ERROR` log. Both down → friendly SSE triple (transport `200`).
- Secrets: prod via Cloud Run Secret Manager / Vercel env only, never in logs/images. Clerk identity: `kromarag-dev` / `kromarag-prod` apps (template `kromarag`), backend verifies JWKS `kid` + prev-set fallback (no operator rotation). `ENV=production` fail-closed on non-Clerk issuer, `*` CORS, non-pooled DSN.
- Migrations at entrypoint: `001→002→003` idempotent, advisory-locked, per-file transactions, additive-only; post-verify orphan `= 0` + dim `768`.

---

## 11. Telemetry & Operations (spec 09)

Source: `specs/09_telemetry_and_operations_spec.md`, `app/core/{tracecontext,middleware}.py`, `app/api/v1/metrics.py`, `app/services/metrics.py`, `loadtests/k6/`.

- **LangSmith stays system of record** (`kromarag.query → router → history.load → retriever.sql → embed → generator.gemini|groq → guardrail → evaluator`); OTel layer only joins/inject W3C context. Ingress `traceparent` validated (`00-<32hex>-<16hex>-<02hex>`), joined to app `trace_id` (dash-strip 1:1), fresh `span_id` minted; invalid → mint fresh (never `400`). Every response + SSE headers + `done` echo `X-Trace-Id` + `traceparent`. DB hop annotated (`SET LOCAL application_name='kromarag:<hex8>'` + `/* trace_id */` comment); outbound LLM `httpx` injects `traceparent`.
- **`/metrics`** (ops plane, not `/api/v1`): Prometheus `0.0.4`, dual-protected — network filter first (IAM/VPC/`METRICS_SCRAPE_ALLOWLIST`; miss → oracle-safe `404`), then `require_role("admin")` (`401/403`). Rate-limit exempt, `no-store`, cardinality-shed at 500 series. Frozen RED/USE catalogue (18 families): `http_server_duration_seconds`, `http_server_requests_total`, `http_rate_limited_total`, `llm_tokens_total`, `llm_fallback_total`, `llm_request_duration_seconds`, `vector_search_duration_seconds`, `embed_duration_seconds`, `db_pool_{in_use,idle,wait_seconds,errors}`, `sse_stream_duration_seconds`, `sse_tokens_total`, `history_tokens_total`, `doc_reindex_total`, `doc_purge_total`, `kromarag_info`, `kromarag_metrics_cardinality_shed_total`. Labels: template-only `route`, numeric `code`, pinned `model`; never `sub/email/doc_id/query`. Histograms carry `trace_id` exemplars.
- **k6 normative** (Locust deferred; `hey`/`curl` smoke only): `query_sse.js` (ramp→hold→spike→recover, `100/min/sub` ceiling, 5+ subs, thresholds `http_req_failed<1%`, `sse_first_token_p95<2500ms`, `sse_done_p95<26000ms`, framing `>99%`), `upload_burst.js` (50KB/2MB/8MB fixtures, `20/min/sub`, `upload_accept_p95<500ms`, `status_poll_p95<100ms`), `soak.js` (30-min mixed, zero `readyz` flaps). Every k6 call sends unique `traceparent`. Chaos flags (`CHAOS_SQL_DELAY_MS`, `CHAOS_GEMINI_DOWN`, `CHAOS_POOL_MAX`) staging-only, forced off in prod (boot `RuntimeError` otherwise).
- Logs: one JSON line/request (`trace_id`, `span_id`, `traceparent`, `sub_hash`, `latency_ms`, `pool_wait_ms`, `model`, `fallback_used`, `status`); DSN/JWT scrubbed, excerpts `≤500` chars.

---

## 12. Setup

### 12.1 Prerequisites

```powershell
# Python 3.12+ (verify)
python --version   # >= 3.12

# uv (recommended installer for the API)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Node >= 22 (verify)
node --version     # >= v22

# k6 (only for load runs)
k6 version
```

Plus: a Postgres 15+ database with `pgvector` (Neon pooled host recommended), a Google AI Studio key (`GOOGLE_API_KEY`), a Groq key (`GROQ_API_KEY`), and optionally a LangSmith key.

### 12.2 Environment Setup

```powershell
# 1. Clone
git clone <repo> KromaRAG
Set-Location KromaRAG

# 2. Root env (generic DB URL — no provider keys)
Copy-Item .env.example .env
# Edit .env: DATABASE_URL, GOOGLE_API_KEY, GROQ_API_KEY, LANGSMITH_API_KEY,
# NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY + CLERK_SECRET_KEY (Clerk dashboard, per app)
#   DATABASE_URL=postgresql+asyncpg://user:password@host:5432/kromarag
#   PGVECTOR_DIM=768
#   GEMINI_MODEL=gemini-3.6-flash / EMBEDDING_MODEL=gemini-embedding-001
#   GROQ_MODEL=openai/gpt-oss-120b / LLM_PRIMARY=gemini / LLM_FALLBACK=groq
#   JWT_ISSUER=http://localhost:3000 (dev bypass) / https://<app>.clerk.accounts.dev (prod)
#   JWT_AUDIENCE=kromarag-web
#   CORS_ORIGINS=http://localhost:3000 / NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
#   OTEL_ENABLED=true / METRICS_ENABLED=true (see specs/09 §2.5)
#   CHAOS_*=off (staging-only; prod boot fails if set)

# 3. API env (see apps/api/.env.example) + Web env (see apps/web/.env.local.example)
Copy-Item apps/api/.env.example apps/api/.env
Copy-Item apps/web/.env.local.example apps/web/.env.local
# Set Clerk keys (per app: kromarag-dev locally) + NEXT_PUBLIC_API_URL.
# Dev-only: AUTH_SECRET (same value in BOTH files for the non-prod HS256 bypass).
# Prod: JWT_ISSUER=https://<prod-app>.clerk.accounts.dev (no AUTH_SECRET in prod).
```

`scripts/check-specs.js` enforces the SDD gate for specs `01`–`09`:
generic driver URL in `.env.example`, no provider-specific keys,
required JWT claims and principal helper, plus the `06`–`09` deltas
(history caps, lifecycle states, health probes, tracing and metrics).

### 12.3 Database (Neon / any Postgres + pgvector)

Point `DATABASE_URL` at your pooled host, then apply the idempotent schema (`001→002→003` in order):

```powershell
# Option A — psql (strip the +asyncpg driver prefix first)
psql "postgresql://user:password@host:5432/kromarag" -f apps/api/migrations/001_kroma_schema.sql
psql "postgresql://user:password@host:5432/kromarag" -f apps/api/migrations/002_conversations_history.sql
psql "postgresql://user:password@host:5432/kromarag" -f apps/api/migrations/003_kroma_documents_lifecycle.sql

# Option B — via API migrator (advisory-locked, lexicographic order)
$env:PYTHONPATH = "apps/api"
uv run --project apps/api python -m app.services.migrate
```

`001` creates `vector` + `pg_trgm` extensions, `kroma_documents`, `kroma_chunks` (`embedding VECTOR(768)`, generated `content_tsv`, `ivfflat ... WITH (lists=100)` + `GIN(content_tsv)` indexes), `kroma_roles`/`kroma_user_roles`/`kroma_document_roles`, and `kroma_conversations`/`kroma_messages`. `002` adds `updated_at`, token/model/route cols, role CHECKs, `trace_uidx`, history indexes. `003` normalizes `ready→indexed` and adds `updated_at/archived_at/version/chunk_config/embedding_model/dim` + lifecycle indexes. Build the `ivfflat` index after bulk loads, then `ANALYZE` (see `reembed.py`). Prod runtime uses pooled DSN (`-pooler` + `sslmode=require`); jobs/migrations use direct `DATABASE_URL_SYNC`.

### 12.4 API Startup

```powershell
Set-Location apps/api
uv sync
uv run uvicorn app.main:app --reload --port 8000
# liveness: http://localhost:8000/health (alias /healthz)
# readiness (DB SELECT 1): http://localhost:8000/ready (alias /readyz)
# metrics (admin + allowlist): http://localhost:8000/metrics
```

From repo root the same server is `npm run dev:api`. CORS allowlist comes from `CORS_ORIGINS`; `DATABASE_URL` is never logged. Prod honors `$PORT` (Cloud Run) and runs `--workers 1` per instance.

### 12.5 Pytest Verification

```powershell
Set-Location apps/api
uv run pytest -q
```

Offline suite (no network/DB — fakes injected), now 12 files in `tests/`: `test_health.py` (incl. `/healthz`/`/readyz` split), `test_ingest.py`, `test_query.py`, `test_retriever.py` (0.7/0.3 weights + RBAC `WHERE` + `indexed` gate + `probes=10` + `SCORE_MIN`), `test_citations.py` (legacy `⌗`/decimal/invented/numeric + fence preservation), `test_agent.py` (LangGraph nodes, Gemini→Groq fallback, guardrail), `test_embeddings.py` (768-dim batch, retired-model 404 hint), `test_conversations.py` (ownership matrix, pagination, truncation caps, idempotent write, retriever-isolation), `test_documents_lifecycle.py` (multipart incl. PDF, state machine, patch-no-reembed, reindex `409`, archive-vs-purge, cross-tenant `404`, orphan-zero), `test_prod_hardening.py` (rate-limit matrix, headers, fallback single-attempt, timeout slices, dual-secret, fail-closed boot), `test_telemetry_ops.py` (traceparent join, `/metrics` 404/401/403/200 matrix, exemplars, cardinality), `test_release_audit.py`. Also run the SDD gate from root: `npm run spec:check` (`node scripts/check-specs.js`, now 9 specs).

### 12.6 Re-embedding Existing Chunks (`reembed.py`)

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

### 12.7 Next.js Dev Server

```powershell
# From repo root
npm install
Copy-Item apps/web/.env.local.example apps/web/.env.local
# Set Clerk dev keys (kromarag-dev) + NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
# Dev: NEXT_PUBLIC_USE_PROXY=1 (same-origin proxy); Prod (Vercel): 0/unset + API_URL=https://<cloud-run-host>
npm run dev --workspace apps/web
# http://localhost:3000 → /login → Clerk sign-in (Google SSO), or the dev-only
# email+role bypass form (non-prod builds only; stamps the local HS256 bypass)
```

Sign-in resolves a short-lived Clerk JWT (template `kromarag`: `sub=user_xxx`,
`email`, `roles[]`, `aud=kromarag-web`, 60s TTL) per request, sent as
`Authorization: Bearer` on every API call. Roles are managed in the Clerk
dashboard (`publicMetadata.roles`) — the sidebar shows them read-only.
FastAPI enforces them (upload/reindex/archive: editor+ or owner rules;
delete/purge: admin/owner; `/metrics`: admin only). Dark/light toggle in the
sidebar. Production build check: `npm run build --workspace apps/web` (verifies `headers()` + `output:standalone`).

### 12.8 Docker / Prod Parity

```powershell
# API image (spec 08 §2.2 normative shape)
docker build -f apps/api/Dockerfile apps/api -t kromarag-api:spec08
# Run with prod guards (pooled DSN placeholder trips fail-closed demo on defaults)
docker run --env-file .env -p 8000:8000 kromarag-api:spec08
# Web: Vercel is primary; standalone output supports Docker-host fallback
```

Entrypoint order: `migrate (001→002→003) → pool warmup probe (/readyz loop ≤60s) → exec uvicorn`. Migration failure aborts boot (prior revision keeps traffic). Prod Cloud Run: `concurrency=80`, `timeout=300s`, `min-instances=1`, `CPU=2/mem=2Gi`. Vercel preview envs point at staging Cloud Run + Neon branch (never prod DB).

### 12.9 Load Tests (k6)

```powershell
# Staging only by default; prod requires K6_TARGET=prod + change-window approval
$env:K6_BASE_URL = "https://staging-api.example"
$env:K6_VIEWER_JWT = "<csv-5+-subs>"
$env:K6_EDITOR_JWT = "<csv-2-subs>"
$env:K6_ADMIN_JWT = "<admin-jwt>"
k6 run --summary-export=summary.json loadtests/k6/query_sse.js
k6 run loadtests/k6/upload_burst.js
k6 run loadtests/k6/soak.js
```

See `loadtests/k6/README.md` + `fixtures/README.md` for corpus, thresholds (release-blockers), and chaos-flag usage (`CHAOS_*` staging-only).

---

## 13. Monorepo Layout

```
apps/web/              # Next.js 16 frontend (App Router, RSC shells + client chat/SSE)
  app/                 # routes (/login, chat, upload, api/dev-token [dev-only])
  proxy.ts             # Clerk edge gate (protect() pages; /api/* public at edge, FastAPI enforces)
  next.config.js       # headers() security set (incl. Clerk CSP) + output:standalone + rewrites() proxy
  lib/kroma-token.ts   # single token source (Clerk template kromarag per request + dev bypass)
  lib/api-client.ts    # fetch + ReadableStream SSE reader (source|token|done) + conversations/documents/metrics fns
  lib/citations.ts     # frontend citation mirror (sanitize_title round-trip)
  lib/store.tsx        # thread store (server-authoritative, localStorage read-through)
  lib/roles.ts         # role helpers (canUpload/isAdmin)
  components/ (Chat, Documents, Sidebar, AppShell, Message, Inspector, Toasts, Providers)
apps/api/              # FastAPI backend (Python 3.12+, async)
  app/main.py          # lifespan (asyncpg pool warmup), CORS, SecurityHeaders, RateLimit + /health* /ready* /metrics + /api/v1 routers
  app/api/v1/          # health (aliases) | metrics (dual-gated) | conversations | documents (lifecycle) | ingest | query (SSE via sse-starlette)
  app/core/            # config (generic DATABASE_URL + RAG/history/lifecycle/prod/telemetry knobs) | security (JWKS/HS256 + rotation RBAC)
                       # costs | logging (JSON) | middleware (headers+trace) | rate_limit | tracecontext (W3C)
  app/services/        # db (pool) | embeddings (gemini-embedding-001/768) | retriever (HYBRID_SQL, indexed-only)
                       # ingest (upload/reindex) | conversations | migrate (001→002→003) | tracing (LangSmith) | metrics (Prometheus)
  app/agent/           # graph (LangGraph nodes + history) | llm (Gemini→Groq) | prompts (V2 + allowlist)
                       # guardrails (citation defense + PII) | evaluator (faithfulness/relevance/leakage)
  app/schemas/         # Pydantic v2 (Principal, QueryRequest+conv_id, ChunkResult, IngestRequest, DocMeta/DocumentDetail, Conversation*/Message*)
  scripts/reembed.py   # in-place re-embedder (768d, REINDEX+ANALYZE)
  scripts/docker-entrypoint.sh  # migrate-then-serve
  Dockerfile           # multi-stage prod artifact (uv build → slim non-root runtime)
  migrations/001_kroma_schema.sql  # idempotent kroma_* DDL
  migrations/002_conversations_history.sql  # history hardening
  migrations/003_kroma_documents_lifecycle.sql  # lifecycle (ready→indexed + versioning)
  tests/               # 12 files (health|ingest|query|retriever|citations|agent|embeddings|conversations|documents_lifecycle|prod_hardening|telemetry_ops|release_audit)
specs/                 # SDD source of truth (9 files)
  01-system-architecture.md  02-api-contracts.md  03-rag-and-agent-workflow.md
  04-observability-and-evals.md  05-development-roadmap.md
  06_conversations_and_history_spec.md  07_document_lifecycle_mgmt_spec.md
  08_production_hardening_and_deployment_spec.md  09_telemetry_and_operations_spec.md
scripts/check-specs.js # SDD validator (9 specs, generic DB, JWT claims, 06/07/08/09 deltas)
loadtests/k6/          # query_sse.js + upload_burst.js + soak.js + lib/{sse,auth}.js + fixtures/
```

Chunking (specs/03 §2, `app/rag/chunker.py`): recursive semantic split, target **600 tokens** / overlap **90** (~15%, `tiktoken cl100k`), boundaries `["\n## ","\n### ","\n\n",". ","; "]`, never split code fences, `ord` preserves order, metadata `{ord, token_count, heading_path}`, max 2000 chunks/doc. Ingestion supports `text/plain, text/markdown, application/pdf` (pypdf) + URL fetch (readability, 50KB cap); failures set `status=failed` with retry idempotent on `doc_id` (retry via `POST /reindex`).

---

## 14. Observability & Costs

- Env: `LANGCHAIN_TRACING_V2=true`, `LANGSMITH_API_KEY`, `LANGCHAIN_PROJECT=KromaRAG` (staging: `KromaRAG-staging`), `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com`, plus `OTEL_ENABLED`, `OTEL_SAMPLING_OK=0.1` / `OTEL_SAMPLING_ERROR=1.0`, `METRICS_ENABLED=true`, `METRICS_SCRAPE_ALLOWLIST`.
- Trace hierarchy per query: `kromarag.query(trace_id)` → `router` → `history.load{msg_count,truncated}` → `retriever.sql` → `embed` → `generator.gemini|groq` → `guardrail` → `evaluator`. Metadata: `{trace_id, user_sub_hash, roles, top_k, model, fallback_used, chunk_ids, scores, history_tokens}`. W3C `traceparent` joined on ingress, echoed on every response + `done`; DB annotated via `application_name` + SQL comment; LLM `httpx` injects `traceparent`.
- `/metrics` Prometheus catalogue (18 families, §11): HTTP RED, tokens, fallback rate, `vector_search_duration_seconds` (p95 `<400ms`), pool gauges, SSE lifetimes, `doc_reindex/purge` counters. Scrape every 15s (admin JWT, allowlisted IP).
- SSE `done.usage` carries `{prompt_tokens, completion_tokens, model}` + `history_tokens`; per-request cost logged to `kroma_usage` (Phase 2 table) + stdout JSON. Cost table (`app/core/costs.py`, indicative per 1M, pin quarterly): `gemini-3.6-flash $1.50/$7.50`, `openai/gpt-oss-120b $0.15/$0.60`, `gemini-embedding-001 $0.02/1M tokens`, legacy `gemini-2.0-flash $0.10/$0.40`, `llama-3.3-70b-versatile $0.59/$0.79`, `text-embedding-004 $0.02`. Alert if p95 cost/query > $0.02 or fallback rate > 15% (10min).
- Structured JSON logs (`trace_id`, `span_id`, `traceparent`, `route`, `sub_hash`, `latency_ms`, `pool_wait_ms`, `model`, `fallback_used`); `ERROR` on fallback, 5xx, RBAC-deny bursts, pool exhaust. Non-functionals: p95 first-token < 2.5s (Groq fallback < 1.5s incremental), p95 retrieval < 400ms, history load < 150ms, `GET /documents` < 250ms, `GET status` < 100ms, `healthz` < 50ms, `readyz` < 300ms.
- k6 SLO evidence: `query_sse` + `upload_burst` thresholds green before traffic shift; 30-min `soak` with zero `readyz` flaps; dashboards (latency p50/p95/p99, fallback %, pool wait, `429` by bucket) screenshotted into release notes.

---

## 15. SDD Workflow & Roadmap

1. Every PR must reference a `specs/0X-*.md` section; contracts in `specs/02` must match `apps/api/app/schemas/*` + `core/security.py`; lifecycle/history/telemetry deltas must match `06/07/08/09` predicates.
2. Run: `uv run pytest -q` (api, from `apps/api`) + `npm run build --workspace apps/web` + `npm run spec:check` (9 specs) + `docker build -f apps/api/Dockerfile apps/api`.
3. Manual check: `GET /healthz`, `GET /readyz`, `POST /api/v1/query` SSE stream (`source→token→done` + `traceparent`), RBAC denial for unauthorized role, fallback chaos test (break Gemini → Groq still answers with `fallback_used=true`), cross-tenant `404` uniformity, `/metrics` 404-vs-403-vs-200 matrix.

Phases (`specs/05` + `06/07/08/09` exit gates): **1** Setup & Ingestion → **2** Hybrid Retriever (recall@6 ≥ 0.8, RBAC leakage test) → **3** LangGraph Agent (SSE citations, faithfulness ≥ 0.85, 403 on bad role) → **4** Frontend Integration (E2E login→query→cite→upload, Lighthouse ≥ 85, pooled prod `DATABASE_URL`) → **5** Conversations (follow-up uses history, 404 oracle, replay, no duplicates) → **6** Lifecycle (upload→reindex→archive→purge, orphan-zero) → **7** Hardening (429/503, headers, rotation, migrations) → **8** Telemetry (trace join, `/metrics`, k6 SLOs). Risks: Neon cold start → pooled + `ready` warmup + `min-instances=1`; Gemini quota → Groq auto-fallback; Auth.js clock skew → ±60s leeway; pool fan-out → pgBouncer + `max=20`.

Utilize `autoskills` (auto-detects stack from `package.json`):

```powershell
npx autoskills --dry-run   # preview (no writes)
npx autoskills -y          # install all detected skills
npx autoskills             # interactive selector
```

Requires Node >= 22. Skills land in `.claude/skills/` etc. (gitignored) + generates `CLAUDE.md`. Re-run after adding deps. See https://www.autoskills.sh / `npx autoskills -h`.

---

## 16. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `embedding model 'text-embedding-004' not found (404 NOT_FOUND)` | Retired model. Set `EMBEDDING_MODEL=gemini-embedding-001` in `apps/api/.env`, then run `reembed.py --dry-run` → full `reembed.py`. |
| `missing GOOGLE_API_KEY/GEMINI_API_KEY` / invalid-key on embed | Use an AI Studio key (typically `AIza…`) with Generative Language API enabled. Vertex/OAuth tokens need `genai.Client(vertexai=True, …)` instead of `api_key` auth. |
| `vector_dims(embedding) != 768` rows in preflight | Mixed-dimension corpus. Run full `reembed.py` (no `--limit`), then verify postflight = 0 mismatches. |
| Gemini 404 / quota → answers stop | Expected path: `generate_with_fallback` retries on 404/429/5xx/timeout and fails over to Groq (`fallback_used=true`, `event: error {code: LLM_TIMEOUT}`). Check `GROQ_API_KEY` + `X-Trace-Id`/`traceparent` logs + `llm_fallback_total`. |
| `401 UNAUTHENTICATED` / `403 FORBIDDEN` | Check the Clerk session JWT (template `kromarag`: fresh per request, 60s TTL — stale copies 401), `JWT_ISSUER=https://<app>.clerk.accounts.dev` / `JWT_AUDIENCE=kromarag-web`, clock skew (±60s), and `roles[]` from `publicMetadata.roles` (viewer can't `POST /ingest`/`/upload`; delete needs admin/owner; `/metrics` needs admin). Dev bypass: same `AUTH_SECRET` in `apps/web/.env.local` + `apps/api/.env`, non-prod only. |
| `404` on own doc/thread | Leak-safe uniform `404` also covers foreign/unshared ids. Verify `owner_sub` vs JWT `sub`, `allowed_roles` shares, and (docs) `status` filter default hides `archived` (use `?status=all`/`archived`). |
| `429 RATE_LIMITED` + `Retry-After` | Tiered buckets: `query 100/min` (`300` admin), `ingest/upload/reindex 20/min`, `unauth 10/min/IP`. Back off per `Retry-After`; probes + `/metrics` exempt. Check `http_rate_limited_total{bucket}`. |
| `503 DB_UNAVAILABLE` + `Retry-After: 5` | Pool exhaust / Neon unreachable / SQL >3s (`RETRIEVER_TIMEOUT_MS`). Check `readyz` (should be `503` while down), `db_pool_*` series, pooled DSN (`-pooler` + `sslmode=require`). |
| `409 REINDEX_IN_PROGRESS` / `ARCHIVE_CONFLICT` | Job in flight (`pending/indexing`). Poll `GET /{id}/status`; `Retry-After: 10` on reindex `409`. Purge only from terminal states. |
| `413 PAYLOAD_TOO_LARGE` | File > `DOC_UPLOAD_MAX_BYTES` (20 MB server; web pre-guard 2 MB). Compress/split or raise server cap deliberately. |
| `422 VALIDATION` / `EMBEDDING_MISMATCH` | Bad `chunk_tokens/overlap` (`100–2000`, `overlap<tokens`), bad `allowed_roles`, blank title, or `embedding_model` ≠ pinned `gemini-embedding-001`. |
| Slow retrieval / `ivfflat` warnings | Ensure index built after bulk load + `ANALYZE kroma_chunks`; `reembed.py` does `REINDEX` + `ANALYZE` automatically. Use Neon pooled host; check `ready` warmup; watch `vector_search_duration_seconds` p95. |
| `readyz 503` but `healthz 200` | By design: pool down pulls instance from traffic. Restore DSN/connectivity; no restart needed (pool re-acquires). |
| Boot `RuntimeError` in prod (`jwt_issuer_invalid`, `jwt_audience_invalid`, `cors_wildcard_rejected`, `chaos_enabled_in_production`, `database_url_invalid`) | Fail-closed guards (`08 §6`, `09 §2.5`): set Clerk `JWT_ISSUER=https://<app>.clerk.accounts.dev` (https + non-localhost) + `JWT_AUDIENCE=kromarag-web`, exact `CORS_ORIGINS` (no `*`), pooled DSN, `CHAOS_*` off. |
| `/metrics` returns `404` for admin | Network gate first: need trusted ingress (`X-Cloud-Run-Internal` / `METRICS_SCRAPE_ALLOWLIST`); non-prod empty allowlist allows all, prod fail-closed denies. External callers always get oracle-safe `404`. |
| SSE disconnect mid-stream / duplicate writes | Client replays via `GET .../messages` (dedup by `trace_id` unique index). Either zero new rows or exactly one pair; re-delivered `done` never double-writes. |

---

## Docs

- `specs/01-system-architecture.md` — topology, SSE/async, `kroma_` schema, RBAC (live models: `gemini-3.6-flash` + `gemini-embedding-001`; legacy values marked)
- `specs/02-api-contracts.md` — OpenAPI, SSE events (+`traceparent`/`history_tokens`), Pydantic, Auth.js JWT claims
- `specs/03-rag-and-agent-workflow.md` — ingestion, hybrid search, LangGraph nodes (live embed/LLM pins)
- `specs/04-observability-and-evals.md` — LangSmith, cost (pinned table + legacy rows), evals
- `specs/05-development-roadmap.md` — 4-phase milestones (+ `06/07/08/09` exit gates in each spec)
- `specs/06_conversations_and_history_spec.md` — threads, history injection caps, `002` DDL, leak-safe `404`
- `specs/07_document_lifecycle_mgmt_spec.md` — upload/reindex/archive/purge, `003` DDL (`ready→indexed`), RBAC matrix
- `specs/08_production_hardening_and_deployment_spec.md` — Cloud Run+Vercel, rate limits, headers, rotation, timeouts, `001→002→003` ops
- `specs/09_telemetry_and_operations_spec.md` — W3C trace join, `/metrics` catalogue, k6 suites + thresholds
- `loadtests/k6/README.md` — how to run `query_sse` / `upload_burst` / `soak` (staging-first, SLO evidence)

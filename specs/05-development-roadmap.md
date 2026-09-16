# 05 — KromaRAG Development Roadmap

> 4 phases. Each milestone lists spec ref, tasks, exit criteria. Update spec before code (SDD).

## Phase 1 — Setup & Ingestion (Week 1) [THIS SCAFFOLD]

- Refs: `01` §1-3, `02` §1-2.
- Tasks: monorepo `/apps/web|/api`, root configs, `DATABASE_URL` generic, `GET /health|/ready`, `POST /ingest` (text/md), `kroma_*` DDL, `uv sync` + `npm build` green.
- Exit: `pytest -q` passes; `POST /ingest` → chunks in `kroma_chunks`; `npx autoskills --dry-run` detects Next.js/FastAPI.

## Phase 2 — Hybrid Retriever (Week 2)

- Refs: `03` §2-4, `02` §1 (`GET /documents`).
- Tasks: `rag/chunker.py` (600/90), Gemini embed batch, hybrid SQL with RBAC `WHERE`, `GET /documents` RBAC-filtered, `ivfflat` tuning on Neon.
- Exit: recall@6 ≥ 0.8 on `evals/seed.csv`; p95 retrieval < 400ms; RBAC leakage test passes.

## Phase 3 — LangGraph Agent (Week 3)

- Refs: `03` §5-6, `02` §3+§5, `04`.
- Tasks: `agent/graph.py` (5 nodes), Gemini→Groq fallback, `hybrid_search` + `get_doc_metadata` tools, SSE `source|token|done`, Auth.js JWT `security.py` (JWKS, roles), LangSmith tracing + `done.usage`.
- Exit: SSE streams with citations; faithfulness ≥ 0.85; fallback chaos test (kill Gemini → Groq answers); 403 on bad role.

## Phase 4 — Frontend Integration (Week 4)

- Refs: `01` §2, `02` §3.
- Tasks: Auth.js login + `auth.ts` JWT callback (roles from `kroma_user_roles`), chat UI (Tailwind, SSE reader), doc upload page (`editor+`), Vercel env (`NEXT_PUBLIC_API_URL`), Cloud Run deploy + `CORS_ORIGINS`.
- Exit: E2E login→query→cite→upload on preview; Lighthouse perf ≥ 85; prod `DATABASE_URL` pooled.

## Cross-cutting

- Every phase: update spec first, add `tests/test_*.py`, run `uv run pytest -q` + `npm run build`.
- Risks: Neon cold start → pooled + `ready` warmup; Gemini quota → Groq auto-fallback; Auth.js clock skew → ±60s leeway.

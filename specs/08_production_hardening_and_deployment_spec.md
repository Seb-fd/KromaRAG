# 08 — Production Hardening & Deployment

> Operational, security, infrastructure, and deployment standards to transition KromaRAG from local development to a secure, resilient, production-grade cloud deployment on **Cloud Run (API) + Vercel (Web) + Neon PostgreSQL + pgBouncer**, with Docker as the portable build artifact. Closes the prod-readiness gap in `01`–`07`: `01` fixes topology but leaves hardening non-normative, `02` freezes `/health` + `/ready` with no k8s aliases / rate-limit / header contract, `03` defines Gemini → Groq fallback with no timeout budget or circuit discipline, `04` defines tracing with dev-grade logging. **Spec-first, no implementation code** — requirements, topology, contracts, predicates, and rules only.

- **Status:** Draft (Build mode, approved 2026-09-16)
- **Refs:** `01 §1–§5` (topology, SSE/async, `kroma_*` schema, RBAC, NFRs), `02 §1–§6` (endpoints, Pydantic, SSE, errors, JWT, versioning), `03 §5–§6` (LangGraph nodes, fallback, knobs), `04 §1–§4` (LangSmith, costs, evals, logs/alerts), `05` (phases, exit discipline), `06 §3.1–§4.6` (leak-safe `404`, atomic writes, idempotency), `07 §2.2–§4.4` (lifecycle states, `003`, retriever `indexed` gate, purge order)
- **Code truth:** `app/main.py:20-43`, `app/core/config.py:6-61`, `app/core/security.py:13-77`, `app/core/logging.py:1-10`, `app/core/costs.py`, `app/services/db.py:9-44`, `app/services/migrate.py:13-43`, `app/agent/llm.py:43-162`, `app/api/v1/health.py`, `apps/api/Dockerfile:1-7`, `apps/api/pyproject.toml:1-37`, `apps/web/next.config.js:1-6`, `apps/web/proxy.ts`, `.env.example:1-42`, `migrations/001_kroma_schema.sql`, `migrations/002_conversations_history.sql`, `migrations/003_kroma_documents_lifecycle.sql`, `scripts/check-specs.js:1-25`
- **Decisions (approved 2026-09-16):** (1) **Cloud Run + Vercel** normative; Docker multi-stage as build artifact only (no separate Render target); (2) **Tiered rate limits** — `POST /query 100/min`, `POST /ingest|upload 20/min`, unauth/IP `10/min`, all `429 + Retry-After`; (3) **Health aliases** — keep `/health` + `/ready`, add `/healthz` + `/readyz` same handlers (non-breaking); (4) **Clerk identity + kid-rollover** — Clerk `kromarag-dev` / `kromarag-prod` apps issue session JWTs (template `kromarag`: email+roles+aud, 60s); backend verifies `RS256` via JWKS `kid` with current-set + cached-prev-set fallback (no operator action; JWKS cache 600s). HS256 dev-bypass is non-prod only (prod rejects kid-less tokens outright).

---

## 1. Requirements & User Stories

### 1.1 Feature Objective

Ship KromaRAG to production without changing any frozen product contract: same SSE shape, same RBAC predicates, same `indexed`-only retrieval, same `404`-leak-safe behavior — but behind rate limits, strict CORS, security headers, secret rotation, pooled DB access, budgeted LLM fallback, warm instances, structured observability, and reproducible container + migration pipelines. Every prod failure must degrade gracefully (fallback answer, `503` with `Retry-After`, or `readyz` 503 pulling the instance) and never leak secrets, PII, JWTs, or tenant existence.

### 1.2 Functional Requirements

| ID | Requirement |
|----|-------------|
| SEC-01 | API enforces **tiered rate limits** (§3.2): `POST /api/v1/query` 100 req/min per authenticated `sub` (admin elevated, §3.2); `POST /api/v1/ingest` + `POST /api/v1/documents/upload` + `POST /{id}/reindex` 20 req/min per `sub`; unauthenticated + auth-failure paths 10 req/min per IP. Breach → `429 {"code":"RATE_LIMITED"}` + `Retry-After` + `X-RateLimit-*` headers. |
| SEC-02 | **CORS** is exact-allowlist only (§3.1): prod `CORS_ORIGINS=https://kromarag.vercel.app` (comma-separated additions only). No `*` with `allow_credentials`. Preflight cached `max_age=600`. Non-listed origin → no `Access-Control-Allow-Origin` (browser blocks; server still returns body for non-browser callers). |
| SEC-03 | **Security headers** on every API response (§3.1): `Strict-Transport-Security`, `Content-Security-Policy` (restrictive, API carries no inline scripts), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`. Web (`next.config.js headers()`) adds same + `Permissions-Policy` + `X-DNS-Prefetch-Control`. Header absence is a release-blocker. |
| SEC-04 | **Secrets never in code/logs/images** (§6): all prod secrets via Cloud Run Secret Manager / Vercel Encrypted Env only. `DATABASE_URL`, `*_API_KEY`, `CLERK_SECRET_KEY` never logged, never echoed in `/health*`/`/ready*`, never baked into Docker layers. Boot **fail-closed**: refuse to serve traffic in `ENV=production` when any required secret is missing or the Clerk issuer is not https/non-localhost. |
| SEC-05 | **JWT rollover without downtime** (§3.3): Clerk issues (template `kromarag`); backend verifies by JWKS `kid` with current-set + cached-prev-set fallback (no operator action). Clock skew `±60s` retained (`02 §5`). |
| SEC-06 | **URL ingest SSRF guard** (§4.5): `fetch_url_text` allows `http/https` only, max 3 redirects, `connect=3s/read=15s` timeout, `URL_MAX_BYTES=50000` cap. Host must resolve to public IP — private/loopback/link-local/multicast/reserved ranges + `169.254.169.254` IMDS denied. DNS rebinding checked on every redirect hop. Violation → `422 URL_FETCH_BLOCKED` (no fetch), never a stack trace. |
| RES-01 | **Timeout budget** enforced end-to-end (§4.1): per-query ceiling `LLM_TIMEOUT_S=25` decomposed `embed ≤8s + retriever SQL ≤3s + primary LLM ≤15s attempt`; fallback attempt `≤12s` inside SSE stream window. Any hop exceeding its slice aborts that hop only and triggers the defined fallback, never a hung stream. |
| RES-02 | **LLM fallback mechanics** deterministic (§4.2): Gemini `gemini-3.6-flash` primary → Groq `openai/gpt-oss-120b` on `timeout / 429 / 404-NOT_FOUND / 5xx` (retryable `LLMError` only, exactly one fallback attempt). Success sets `fallback_used=true` + `event: error {code:LLM_TIMEOUT|LLM_RATE_LIMIT|LLM_ERROR}` + `ERROR`-level structured log. Both providers down → friendly `503` SSE triple (`source:[] + error + done`) with pair persisted per `06 §4.2`. |
| RES-03 | **DB resilience via pooling** (§2.4): prod connects **only** through Neon pooled host (`-pooler`, `sslmode=require`) with `asyncpg min=2, max=20, command_timeout=10s, statement_cache_size=0` (pgBouncer transaction mode) + `pool_recycle=300s`. Pool exhaustion → `503 {code:DB_UNAVAILABLE}` + `Retry-After: 5`, never a stack trace. `readyz` fails while pool is down so the orchestrator pulls the instance. |
| SCALE-01 | **Cold-start mitigation** (§2.5): Cloud Run `min-instances=1` on API service; `lifespan` warms `asyncpg` pool (`init_pool()`) before accepting traffic; lazy-import heavy deps (`google-genai`, `tiktoken`) outside request path; `/readyz` is the startup/readiness gate. p95 cold penalty budget `≤ 3s` on top of warm p95s. |
| SCALE-02 | **Worker configuration** (§2.2): Cloud Run `concurrency=80`, `timeout=300s`, `max-instances` capped with cost alert; container runs **single Uvicorn worker** per instance (`--workers 1`, Cloud Run scales horizontally — never multi-worker inside one instance); documented Gunicorn+`UvicornWorker` alternative (`WEB_CONCURRENCY=2×CPU+1`) for Render/Docker-host targets only. `BackgroundTasks` remain in-process Phase 1 (pgqueue/Redis deferred, no new infra). |
| SCALE-03 | **CDN / caching for Web** (§2.3): Vercel Edge cache `/_next/static/*` immutable `max-age=31536000`; `no-store` on `/api/*`, `/login`, chat SSE; ISR/`revalidate` on document-list reads only. API sends `Cache-Control: no-store` on all `/api/v1/*` + SSE `Cache-Control: no-cache` retained (`02 §3`). |
| OBS-01 | **Health probes** (§5.1): `GET /health` + alias `GET /healthz` = liveness (no DB, `<50ms`); `GET /ready` + alias `GET /readyz` = readiness (`SELECT 1` + pool stats + optional model-reachability `degraded` flag). Cloud Run `livenessProbe → /healthz`, `startupProbe/readinessProbe → /readyz`. |
| OBS-02 | **Structured JSON logging** (§5.2): every request emits one JSON line `{ts, level, trace_id, route, sub_hash, latency_ms, model, fallback_used, status}`; `ERROR` on fallback / 5xx / RBAC-deny bursts / pool exhaust. Raw `sub`/email/JWT/doc-content never logged (hash + `≤500`-char excerpts per `04 §1`). |
| OBS-03 | **Production tracing** (§5.3): LangSmith `LANGCHAIN_PROJECT=KromaRAG` with background batching (never blocks SSE), sampling `1.0` on errors / `0.1` on success, cost alert `p95 cost/query > $0.02` or `fallback rate > 15%` (`04 §2`). |

### 1.3 User Stories

- **US-1 Safe launch:** As an operator, I deploy the multi-stage images + run `001→002→003` against pooled Neon so the service passes `healthz/readyz` and serves traffic with zero contract breakage.
- **US-2 Abuse survival:** As an operator, a scraper hammering `/query` gets `429 + Retry-After` while legitimate users stay under p95 budgets.
- **US-3 Provider outage:** As a viewer, when Gemini times out I still get a Groq-backed streamed answer annotated `fallback_used=true` with no duplicate history write.
- **US-4 Key rollover:** As a security engineer, Clerk signing-key rotations pass with zero forced logouts (JWKS cache + prev-set fallback; no operator action).
- **US-5 Fast diagnosis:** As an on-call, a `readyz 503` + JSON log with `trace_id` tells me in under a minute whether the fault is DB, pool, or LLM.

### 1.4 Non-Functional Requirements

| Category | Target |
|----------|--------|
| Performance | Warm p95 preserved: first-token `< 2.5s` (Groq fallback `< 1.5s` incremental), retrieval `< 400ms`, history load `< 150ms`, `GET /documents` `< 250ms`, `GET status` `< 100ms`; `healthz` p95 `< 50ms`, `readyz` p95 `< 300ms` (pooled). Cold-start penalty `≤ 3s` with `min-instances=1`. |
| Availability | 99.9% monthly per Cloud Run + Neon SLA composition; `min-instances=1` + multi-zone Neon; `readyz` gating ensures no traffic to unready instances. RPO `≤ 5min` (Neon PITR), RTO `≤ 15min` (image rollback). |
| Security | OWASP ASVS L2 baseline: exact CORS, HSTS preload, CSP, no-sniff, deny-frame, secrets fail-closed, dual-secret rotation, uniform `404` oracle protection (`06`/`07` patterns retained), dependency scan (`trivy`/`pip-audit` high=0 to release). |
| Scaling | Stateless API; horizontal scale on Cloud Run request-concurrency; pool `max=20`/instance with pgBouncer absorbing fan-out; `max-instances` set with billing alert (no unbounded scale). |
| Reliability | Single fallback attempt only (no retry storms); idempotent migrations re-runnable; purge/orphan invariants from `07 §4.3` hold under deploy; SSE exactly-once `done` preserved across fallback. |
| Observability | 100% error traces, 10% success sample; `X-Trace-Id` on every response incl. `429/503`; dashboards: latency p50/p95/p99, fallback %, pool wait, `readyz` flap rate. |
| Configurability | All §6 knobs env-overridable with documented defaults; `ENV=production` flips fail-closed guards on (dev defaults rejected). |

---

## 2. Infrastructure & Deployment Topology

### 2.1 Normative Topology (Cloud Run + Vercel)

```
                           HTTPS                    HTTPS (pooled, sslmode=require)
┌──────────────────┐  ┌──────────────────┐  ┌─────────────────────┐  ┌──────────────┐
│ Vercel (web)     │  │ Cloud Run (api)  │  │ Neon Postgres       │  │ Providers    │
│ Next.js 16       │─►│ FastAPI + Uvicorn│─►│ pgBouncer (txn)     │  │ Gemini 3.6   │
│ Edge CDN cache   │  │ min-inst=1       │  │ kroma_* + VECTOR    │  │ Groq 120b    │
│ Clerk session    │  │ /healthz /readyz │  │ 768 + ivfflat + GIN │  │ LangSmith    │
│ template kromarag│◄─│ verify kid+prev  │  │ PITR + pooled host  │  │ (background) │
└──────────────────┘  └──────────────────┘  └─────────────────────┘  └──────────────┘
         ▲
         │ issues (email+roles+aud, 60s)
┌──────────────────┐
│ Clerk            │
│ kromarag-dev     │
│ kromarag-prod    │
└──────────────────┘
```

- **Frontend:** Vercel project `kromarag-web`, `NEXT_PUBLIC_API_URL=https://<cloud-run-host>`, Clerk `kromarag-dev` (preview/dev) / `kromarag-prod` (prod) apps issue session JWTs via the `kromarag` template (separate keys; never share). Preview deployments point at staging Cloud Run + staging Neon branch (never prod DB).
- **Backend:** Cloud Run service `kromarag-api`, region pinned (e.g. `europe-west1`), `concurrency=80`, `timeout=300s`, `min-instances=1`, `max-instances=<quota with alert>`, `CPU=2, memory=2Gi` baseline. Secrets mounted from Secret Manager as env (never in image).
- **DB:** Neon project with `main` (prod) + `staging` branch; prod connection **only** via pooled host (`*-pooler.*.neon.tech`, `sslmode=require`). Direct (non-pooled) host reserved for `migrate.py` one-shot jobs and `ANALYZE` maintenance.
- **Docker role:** multi-stage images are the reproducible build artifact for Cloud Run deploy, local prod-parity (`docker compose up --build`), and rollback (immutable tag per git SHA). Vercel builds Web natively; `web/Dockerfile` is fallback for Docker hosts only.

### 2.2 Containerization (production multi-stage contracts)

**Backend `apps/api/Dockerfile` (normative shape — implementation follows in code PR):**

```dockerfile
# stage 1 — build: resolve deps with uv lockfile, no app code
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

# stage 2 — runtime: slim, non-root, migrate-then-serve entrypoint
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ENV=production WEB_CONCURRENCY=1
WORKDIR /srv
RUN useradd -r -u 10001 appuser && apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /srv/.venv /srv/.venv
COPY app ./app
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "15"]
```

Rules:

1. **No secrets in layers:** `COPY .env` forbidden; `.dockerignore` MUST exclude `.env*`, `__pycache__`, `.pytest_cache`, `node_modules`.
2. **Entrypoint order (normative):** `migrate (001→002→003, §2.4) → pool warmup probe (`/readyz` loop ≤60s) → exec CMD`. Migration failure aborts boot (CrashLoop, never serve stale schema).
3. **Workers:** Cloud Run = exactly `--workers 1` per instance (horizontal scale provides concurrency). Gunicorn alternative permitted **only** for non-Cloud-Run Docker hosts: `gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w $WEB_CONCURRENCY --timeout 120 --graceful-timeout 30` with `WEB_CONCURRENCY=2×CPU+1`.
4. **Signals:** `STOPSIGNAL SIGTERM`, graceful drain `30s`; in-flight SSE streams complete `done` before shutdown when within drain window.
5. **Tags:** immutable `:git-<sha>` + floating `:prod`; rollback = redeploy prior `:git-<sha>` (no `latest` in prod). `--source` deploys (no registry) carry the SHA as an immutable Cloud Run `--revision-suffix git-<sha>` instead; traffic shift is the floating pointer (`scripts/deploy/10-cloudrun-prod.sh`, `11-cloudrun-staging.sh`).

**Frontend `apps/web/Dockerfile` (fallback artifact; Vercel is primary):**

```dockerfile
FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
RUN useradd -r -u 10001 nextuser
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER nextuser
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r=>{if(!r.ok)process.exit(1)})"
CMD ["node", "server.js"]
```

Requires `next.config.js` `output: "standalone"` (additive; Vercel ignores it safely). `NEXT_PUBLIC_*` baked at **build** time — one image per environment (staging vs prod never share a web image).

**Compose (local prod-parity, non-normative reference):** `docker compose --profile prod up --build` runs `api` (prod Dockerfile, `ENV=production`, pooled `DATABASE_URL`) + `web` (standalone) + optional `db` (local `pgvector/pgvector:pg16` for offline parity only — never a prod substitute).

### 2.3 Web Topology — Vercel + CDN / Caching Strategy (Next.js 14)

- **Routes:** `/login` (Clerk `<SignIn>` + dev-only bypass form in non-prod), `/` chat (client SSE reader), `/documents` upload/list, `/api/dev-token` (dev-only HS256 mint, 404 in prod), `/api/health` lightweight (for container fallback only; prod traffic calls Cloud Run directly via `NEXT_PUBLIC_API_URL`, never Next.js-rewritten).
- **`next.config.js` additions (additive):**
  ```js
  // output: "standalone" (Docker fallback), headers() → HSTS/CSP/nosniff/deny-frame (mirrors §3.1),
  // images.remotePatterns strict, poweredByHeader: false
  ```
- **Cache contract:**

  | Surface | Policy |
  |---|---|
  | `/_next/static/*` | `public, max-age=31536000, immutable` (Vercel default — keep) |
  | `/api/*`, `/login`, chat SSE | `no-store` (never cache JWT-bearing or streamed responses) |
  | Document list reads | ISR `revalidate=30s` client-side `stale-while-revalidate` only; mutations (`upload/patch/reindex/archive/purge`) invalidate client cache immediately |
  | API `/api/v1/*` | `Cache-Control: no-store` always; SSE `Cache-Control: no-cache` + `: ping` 15s retained (`02 §3`) |

- **Env:** `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (per-env Clerk app key) + server-only `CLERK_SECRET_KEY`. Preview envs use staging API + Clerk dev-app values via Vercel Environment scoping (Production / Preview / Development never cross-wired). No `AUTH_SECRET*` / Google OAuth client on Vercel (Google SSO lives in the Clerk dashboard).

### 2.4 Database Operations — Neon Production Setup

1. **Connection strings (normative):**
   - Runtime (API): `DATABASE_URL=postgresql+asyncpg://<user>:<pw>@<host>-pooler.<region>.neon.tech:5432/<db>?sslmode=require` (pooled, pgBouncer transaction mode).
   - One-shot jobs (`migrate.py`, `ANALYZE`, `reembed.py`): `DATABASE_URL_SYNC=postgresql://<user>:<pw>@<host>.<region>.neon.tech:5432/<db>?sslmode=require` (direct, non-pooled). The async migrator strips `+asyncpg` to `postgresql://` (existing `_dsn()` behavior retained).
2. **Pool discipline (`app/services/db.py` prod values):** `min_size=2, max_size=20, timeout=10, command_timeout=10, statement_cache_size=0, max_inactive_connection_lifetime=300`. `statement_cache_size=0` is REQUIRED under pgBouncer transaction pooling (prepared-statement mismatch otherwise). `init_pool()` failure MUST NOT crash boot (existing behavior: pool `None`, `/healthz` 200, `/readyz` 503) — orchestrator holds traffic until `readyz` passes.
3. **pgBouncer posture:** Neon-managed pooler, `pool_mode=transaction`, `max_client_conn` sized ≥ `max-instances × 20`; server-side `idle_in_transaction_session_timeout=60s`; no client-supplied `statement_timeout` overrides.
4. **Extensions/indexes untouched:** `vector`, `pg_trgm`; `ivfflat (lists=100)` + `GIN(content_tsv)`; `SET LOCAL ivfflat.probes = 10` per retrieval; `ANALYZE kroma_chunks` after bulk ingest/reindex/purge `>10%` (§4.4 of `07` retained).
5. **Migration execution strategy (`001`, `002`, `003` via `migrate.py` entrypoint):**
   - Order: lexicographic `001_kroma_schema.sql → 002_conversations_history.sql → 003_kroma_documents_lifecycle.sql` (existing `sorted(glob)` retained). All three MUST be idempotent (`IF NOT EXISTS`, `DO`-guarded `CHECK`s, `ready→indexed` backfill safe re-run).
   - Entrypoint runs migrations with a Postgres advisory lock (`pg_advisory_lock(hashtext('kroma_migrate'))`) so concurrent Cloud Run revisions never interleave DDL. Lock failure → wait ≤120s → abort boot with `ERROR` log.
   - Each file executes in its own transaction; failure stops the chain and fails boot (no partial-serve). There is NO down-migration: rollback = forward-fix migration (new file), never `DROP`.
   - Zero-downtime rule: migrations are **additive-only** (`ADD COLUMN IF NOT EXISTS`, new indexes `CONCURRENTLY` where supported, backfills batched). The old code revision MUST remain readable against the migrated schema until the new revision is `readyz`-healthy (expand-then-contract). Destructive DDL requires a dedicated spec.
   - Post-deploy verify (logged): `SELECT COUNT(*) FROM kroma_chunks c LEFT JOIN kroma_documents d ON d.id=c.doc_id WHERE d.id IS NULL` = 0; `vector_dims` sample = 768; `readyz` 200 on all instances before traffic shift completes.

### 2.5 Scalability & Performance Implementation Notes

- **Cloud Run knobs (normative defaults):** `concurrency=80`, `timeout=300s` (covers 25s LLM budget + SSE heartbeats + slow clients), `cpu=2`, `memory=2Gi`, `min-instances=1`, `max-instances=20` (raise only with billing alert), `cpu-throttling=false` for steady tail latency.
- **Uvicorn flags (prod):** `--workers 1 --loop uvloop --http httptools --timeout-keep-alive 15 --limit-concurrency 200 --limit-max-requests 10000`. `--limit-max-requests` enables periodic worker recycle (memory-hygiene) without dropping warm-instance coverage (Cloud Run keeps ≥1 instance regardless).
- **Cold-start path:** `lifespan` → `init_pool()` + `importlib` preload of `app.agent.graph` (LangGraph compile once) + JWKS prefetch best-effort (failure swallowed; first authed request fetches). Heavy model clients (`httpx.AsyncClient` pools to Gemini/Groq) created per-request with connection reuse via shared `limits(max_connections=100, max_keepalive=20)` factory (no global socket leak across revisions).
- **Retrieval budget guard:** `command_timeout=10s` on asyncpg + `statement_timeout=3000ms` (`SET LOCAL`) on the hybrid query; `EXPLAIN` budget `<300ms @100K chunks` retained (`03 §4`).

### 2.6 Config Knobs (env, extends `03 §6` + `06 §2.6` + `07 §2.6` + `app/core/config.py`)

```python
env: str = "development"                    # ENV — "production" flips fail-closed guards
web_concurrency: int = 1                    # WEB_CONCURRENCY — 1 on Cloud Run; 2*CPU+1 for gunicorn hosts
pool_min_size: int = 2                      # POOL_MIN_SIZE (prod 2; dev 1)
pool_max_size: int = 20                     # POOL_MAX_SIZE (prod 20; dev 10)
pool_command_timeout: int = 10              # POOL_COMMAND_TIMEOUT (effective env name)
rate_limit_query_per_min: int = 100         # RATE_LIMIT_QUERY_PER_MIN
rate_limit_ingest_per_min: int = 20         # RATE_LIMIT_INGEST_PER_MIN (+ upload/reindex share bucket)
rate_limit_unauth_per_min: int = 10         # RATE_LIMIT_UNAUTH_PER_MIN (IP-keyed)
rate_limit_admin_per_min: int = 300         # RATE_LIMIT_ADMIN_PER_MIN (elevated, still capped)
llm_timeout_s: int = 25                     # unchanged (03 §6)
llm_fallback_timeout_s: int = 12            # LLM_FALLBACK_TIMEOUT_S — Groq attempt ceiling
embed_timeout_s: int = 8                    # EMBED_TIMEOUT_S — per-batch ceiling
retriever_timeout_ms: int = 3000            # RETRIEVER_TIMEOUT_MS — SET LOCAL statement_timeout
clerk_jwks_url: str = ""                    # CLERK_JWKS_URL — explicit JWKS pin (default derives {JWT_ISSUER}/.well-known/jwks.json)
# AUTH_SECRET* are dev-only now (HS256 dev-bypass, non-prod): auth_secret /
# auth_secret_current / auth_secret_previous kept for local mint only; prod
# requires the Clerk JWT_ISSUER (https + non-localhost) instead.
```

---

## 3. System Interface & Contracts

Base `{API_URL}`. Auth `Authorization: Bearer <Clerk JWT (template kromarag)>` (`viewer+` unless noted). Errors `02 §4` shape `{"detail":{"code","message"}}` + `X-Trace-Id` on **every** response including `429/503`. Leak-safe `404` patterns from `06`/`07` unchanged.

### 3.1 Security Middleware Contract (normative order)

Request path: `TrustedHost → CORS → RateLimit → Auth → Route`. Response path appends `SecurityHeaders + X-Trace-Id + X-RateLimit-*`.

1. **Trusted hosts:** `ALLOWED_HOSTS=["<cloud-run-host>", "kromarag.vercel.app", "localhost"]` (prod rejects `Host` mismatch with `400`). `redirect_slashes` disabled for `/healthz` parity.
2. **CORS:** `allow_origins = CORS_ORIGINS.split(",")` (stripped, exact, `https://` in prod — `http://localhost:*` dev only), `allow_credentials=True`, `allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"]` (never `*` in prod), `allow_headers=["Authorization","Content-Type","Accept","X-Requested-With"]`, `expose_headers=["X-Trace-Id","X-RateLimit-Limit","X-RateLimit-Remaining","Retry-After"]`, `max_age=600`.
3. **Security headers (every response, including errors):**

   ```
   Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
   Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
   X-Content-Type-Options: nosniff
   X-Frame-Options: DENY
   Referrer-Policy: strict-origin-when-cross-origin
   Cross-Origin-Opener-Policy: same-origin
   Cross-Origin-Resource-Policy: same-origin
   Permissions-Policy: camera=(), microphone=(), geolocation=()
   X-DNS-Prefetch-Control: off
   ```

   `Server` header suppressed (`server_header=False`). `X-Powered-By` never emitted.

### 3.2 Rate-Limit Contract (tiered, per-`sub` + per-IP)

| Bucket | Key | Limit | Window | Scope |
|---|---|---|---|---|
| `query` | `sub` (`viewer`/`editor`) | 100 req | 60s sliding | `POST /api/v1/query` |
| `query:admin` | `sub` (`admin`) | 300 req | 60s sliding | `POST /api/v1/query` |
| `ingest` | `sub` (`editor+`) | 20 req | 60s sliding | `POST /ingest`, `POST /documents/upload`, `POST /{id}/reindex` |
| `read` | `sub` | 300 req | 60s sliding | `GET /documents*`, `GET /conversations*` |
| `unauth` | client IP | 10 req | 60s sliding | any `401`-path + `/login`-adjacent API abuse |

- Algorithm: sliding-window counter (in-memory Phase 1, Redis-compatible interface deferred — signature `check_limit(key, limit, window_s) -> (allowed, remaining, reset_s)` so the backend swap is non-breaking). Multi-instance note: effective global limit = `per-instance limit × instances`; cap Cloud Run `max-instances ≤ 3` or lower limits to compensate until Redis lands.
- Breach response: `429 {"detail":{"code":"RATE_LIMITED","message":"rate limit exceeded, retry in Ns"}}` + `Retry-After: <s>` + `X-RateLimit-Limit/Remaining/Reset`. The `Retry-After` value equals `reset_s` (never `0`). Rate-limited requests MUST still emit `X-Trace-Id` and a structured `WARNING` log (no PII).
- Auth runs **before** per-`sub` bucketing: missing/invalid JWT consumes `unauth` (IP) bucket and returns `401` until that bucket exhausts, then `429` (prevents credential-stuffing amplification from learning `401` vs `429` oracle — both carry identical `X-Trace-Id` discipline).
- `GET /health*` + `/ready*` are **exempt** from rate limits (probe traffic must never `429`).

### 3.3 JWT & Kid-Rollover Contract (extends `02 §5`, non-breaking)

1. **Issuance (Clerk `kromarag-dev` / `kromarag-prod` apps):** Clerk issues session JWTs with the custom `kromarag` template — `email={{user.primary_email_address}}`, `roles={{user.public_metadata.roles}}`, `aud=kromarag-web`, lifetime 60s. Roles live in `publicMetadata.roles` (Dashboard → Users → Metadata; default `["viewer"]`); allowed redirect origins cover prod + staging + `http://localhost:3000`; Google OAuth is enabled in the Clerk dashboard (no `GOOGLE_CLIENT_ID/SECRET` on Vercel). The browser resolves a fresh token per request (`getToken({ template: "kromarag" })`); the edge (`proxy.ts` `protect()`) gates pages while `/api/*` stays public at the edge (FastAPI enforces auth itself).
2. **Verification (API `security.py`):**
   - Parse `Bearer`; fetch `{JWT_ISSUER}/.well-known/jwks.json` (explicit `CLERK_JWKS_URL` / `JWT_JWKS_URL` wins; cache 600s), match `kid`; on `kid` miss, refresh once then try the cached **prev-set** before rejecting (covers propagation lag). HS256 dev-bypass verifies only when NOT prod; prod rejects kid-less tokens outright.
   - Normalize roles via the Clerk claim map (`roles` top-level or `public_metadata.roles`, `ALLOWED_ROLES`, default `["viewer"]`, missing `sub` (`user_xxx`) → `401`). Rollover MUST NOT alter role semantics.
   - Emit `WARNING` log on prev-set use (`jwks_previous_key_used`, no key material) so operators can observe rotation drain.
3. **Rotation runbook (kid-rollover — no operator action):** Clerk rotates signing keys automatically; the backend's JWKS cache (600s) + prev-set fallback already implements zero-downtime overlap. Operator steps: none (verify `jwks_previous_key_used` drains after Clerk publishes the new `kid`). Rollback = prior Cloud Run revision + prior Vercel deployment (both immutable; no DB migration involved).
4. **Boot guard:** `ENV=production` requires `JWT_ISSUER=https://<clerk-app>` (https + non-localhost) and `JWT_AUDIENCE=kromarag-web` → else `RuntimeError` (`jwt_issuer_invalid` / `jwt_audience_invalid`, fail-closed, never serve). `AUTH_SECRET_*` are dev-only and no longer gated in prod.
5. **Verify hygiene:** in `ENV=production` any configured HS256 secret that is weak (`<32 chars`) or a documented default/placeholder (`change-me*`, `placeholder*`) is dropped from the verify set (public values are forgeable). Prod identity is Clerk RS256; the filter is retained harmlessly for non-prod dev-bypass semantics.

### 3.4 Prod Endpoint Deltas (additive, non-breaking)

| Method & Path | Auth | Behavior |
|---|---|---|
| `GET /health` (kept) + `GET /healthz` (alias) | none, rate-limit exempt | Liveness: `200 {"status":"ok","service":"kromarag-api","version":"0.1.0"}` in `<50ms`, no DB, no provider calls. |
| `GET /ready` (kept) + `GET /readyz` (alias) | none, rate-limit exempt | Readiness §5.1: `200 {"ready":true,"db":"ok","pool":{...},"models":{"gemini":"ok\|unprobed","groq":"ok\|unprobed"},"version":"0.1.0"}` / `503 {"ready":false,"db":"down",...}`. |
| `GET /metrics` | `admin` JWT **AND** scraper allowlist (`09` §3.2) | Dual-gated ops plane: network miss → oracle-safe `404` (even for admins); with empty `METRICS_SCRAPE_ALLOWLIST` in prod the gate is closed for all external callers (fail-closed healthy). Deploy-kit smoke derives authed expectations from the anon probe (anon `404` → authed `404`; anon `401` → viewer `403` + admin `200`). |
| All `/api/v1/*` | per-route (`02`/`06`/`07`) | Adds `429 RATE_LIMITED` + `503 DB_UNAVAILABLE` to the documented error-code set; existing codes unchanged. |

---

## 4. Business Logic & Edge Cases

### 4.1 Timeout Budgets (normative decomposition of `LLM_TIMEOUT_S=25`)

```
POST /query total SSE window ............ 25s (LLM_TIMEOUT_S, unchanged default)
├─ embed query (batch-1, LRU hit ~0ms) .. ≤ 8s  (EMBED_TIMEOUT_S; miss → single attempt, no embed retry storm)
├─ hybrid SQL (SET LOCAL 3000ms) ........ ≤ 3s  (RETRIEVER_TIMEOUT_MS; exceed → 503 DB path, no fallback to LLM-only answer)
├─ primary Gemini attempt ............... ≤ 15s (httpx connect=3s/read=15s; abort → fallback, not 504)
└─ fallback Groq attempt (if triggered) . ≤ 12s (LLM_FALLBACK_TIMEOUT_S; runs INSIDE stream — source already sent)
Background (never blocks stream) ........ LangSmith batch, usage log, history pair write (each best-effort with ERROR alert on failure)
```

- `httpx` discipline: explicit `Timeout(connect=3.0, read=<slice>, write=5.0, pool=3.0)` per call (no implicit infinite read). `BackgroundTasks` (ingest embed batch-32) use `EMBED_TIMEOUT_S × retries(3× exponential)` inherited from `03 §3` — prod adds jitter `±20%` to avoid thundering-herd on provider recovery.
- SSE disconnect (`request.is_disconnected()`) during fallback: break token loop immediately, still attempt history pair write if answer finalized (`06 §4.2` exactly-once via `trace_id` unique index).

### 4.2 Fallback Mechanics (Gemini 3.6 Flash → Groq `openai/gpt-oss-120b`)

1. **Trigger set (retryable only):** `httpx.TimeoutException`/`ConnectError` → `LLM_TIMEOUT`; HTTP `429` → `LLM_RATE_LIMIT`; HTTP `404` or body contains `NOT_FOUND`/`IS NOT FOUND` (retired-model path) → retryable `LLM_ERROR`; HTTP `5xx` → retryable `LLM_ERROR`. HTTP `4xx` other than `429/404-shape` → non-retryable (raise, no fallback — prevents masking auth/quota-config errors as valid answers).
2. **Exactly one fallback attempt:** `generate_with_fallback()` order `[llm_primary, llm_fallback]` (defaults `gemini, groq`), `first_err` preserved as `fallback_error`. No second loop, no fallback-to-fallback. `fallback_used=true` surfaces in `done.usage`, LangSmith span `generator.gemini|groq`, and structured log (`level=ERROR` with `trace_id`, `code`, prompt-token counts — never prompt text).
3. **Stream framing under fallback:** `source` is emitted BEFORE the primary attempt (retrieval already succeeded), so fallback never re-sends `source`. Sequence stays `source(1) → token(0..N, possibly from Groq) → error{code} (one, informational) → done(1, usage.model=<actual>)`. Clients MUST tolerate `error` mid-stream followed by continued `token`s (existing `02 §3` contract already permits this).
4. **Total failure (both providers):** `source:[] + error{code:LLM_TIMEOUT|LLM_ERROR} + friendly tokens ("I can't reach my language models right now…") + done` with HTTP 200 SSE (transport ok, generation degraded) and `503`-equivalent `error.code` for alerting; pair persisted with friendly text per `06 §4.2` so replay matches.
5. **Fallback-rate guard:** sustained `fallback rate > 15% over 10min` fires `WARNING` alert (provider incident), `> 40%` fires `CRITICAL` + auto-page (primary likely down — operator pins `LLM_PRIMARY=groq` via env flip, no redeploy of code logic needed since `order` reads settings).

### 4.3 Upload / Reindex Under Load

- Multipart `file > DOC_UPLOAD_MAX_BYTES (20 MB)` → `413 PAYLOAD_TOO_LARGE` **before** parse (unchanged `07 §4.7`); rate-limit `ingest` bucket applies BEFORE size check (abuse traffic rejected cheapest-first).
- Concurrent `POST /reindex` while `pending/indexing` → `409 REINDEX_IN_PROGRESS` (unchanged); prod adds `Retry-After: 10` on the `409` so pollers back off instead of hot-looping `GET status`.
- Embed provider down mid-reindex → job `failed` + truncated `error`, prior chunks preserved per `07 §4.2` rule 3; `readyz` stays 200 (job failure is not instance-unhealthy).

### 4.4 Validation, Failure & Safety Matrix

| Case | Behavior |
|------|----------|
| Missing/expired Clerk JWT, bad `iss/aud`, missing `sub` (`user_xxx`), kid-less HS256 in prod | `401 UNAUTHENTICATED` (unchanged `02 §5`); consumes `unauth` IP bucket. |
| `viewer` on `editor+` route | `403 FORBIDDEN` (role gate, before existence checks — the ONLY `403` for routes; `06`/`07` `404`-oracle rules unchanged for `/{id}` existence). |
| Guessed UUID / foreign doc/thread | `404 NOT_FOUND` uniform body (unchanged). |
| Rate breach | `429 RATE_LIMITED` + `Retry-After` + `X-RateLimit-*` + `X-Trace-Id`; `WARNING` log. Probes exempt. |
| Pool exhaust / Neon unreachable | `503 DB_UNAVAILABLE` + `Retry-After: 5`; `readyz` → `503 {"ready":false,"db":"down"}`; `healthz` stays `200`. |
| Gemini retryable failure | Fallback to Groq (§4.2); `ERROR` log; `done.fallback_used=true`. |
| Both LLMs down | Friendly SSE `error + done` (§4.2 rule 4); no `500` on transport; `CRITICAL` alert. |
| `JWT_ISSUER` not Clerk-https / localhost in prod, or `JWT_AUDIENCE ≠ kromarag-web` | Boot `RuntimeError` (fail-closed: `jwt_issuer_invalid` / `jwt_audience_invalid`); Cloud Run revision never becomes traffic-serving. Kid-less HS256 in prod → `401` (§3.3). |
| `CORS_ORIGINS` contains `*` with `ENV=production` | Boot `RuntimeError` (fail-closed). |
| Migration failure at entrypoint | Boot abort; revision stays unready; prior revision keeps traffic (zero-downtime preserved). |
| Oversize file / bad mime / validation | `413` / `422` per `07 §4.7` (unchanged), now also rate-counted. |
| URL ingest `file://`, non-http scheme, private/loopback/IMDS host, redirect to private | `422 URL_FETCH_BLOCKED` per SEC-06 (§4.5); no outbound fetch, `WARNING` log with host only (no full URL content). |

### 4.5 URL Fetch SSRF Guard (SEC-06 normative)

1. **Scheme:** `http`/`https` only. Anything else (`file://`, `ftp://`, `gopher://`, no-scheme) → `ValueError("URL_FETCH_BLOCKED: scheme not allowed")`.
2. **DNS + IP check per hop:** resolve host via `socket.getaddrinfo`; if ALL resolved IPs are private/loopback/link-local/multicast/reserved/unspecified (`ipaddress` module) → block. Covers `localhost`, `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254/16` (IMDS), `::1`, `fc00::/7`, `fe80::/10`.
3. **Redirects:** max 3 hops, each hop re-validated (DNS-rebinding safe). `httpx` `follow_redirects=False` + manual loop so every `Location` is checked before fetch.
4. **Timeouts/caps:** `Timeout(connect=3.0, read=15.0, write=5.0, pool=3.0)`, `URL_MAX_BYTES=50000` slice. No credentials forwarded (`Authorization`/`Cookie` stripped on cross-host redirect).
5. **Error mapping:** blocked → `ValueError(URL_FETCH_BLOCKED)` → endpoint maps to `422`; network/timeout → `ValueError(URL_FETCH_FAILED)` → `422` with safe message (no internal IP disclosure).

---

## 5. Observability & Health Checks

### 5.1 Health Endpoints (`/healthz`, `/readyz` + legacy aliases)

```python
# GET /health, GET /healthz — liveness. No auth, no rate limit, no DB, no provider I/O.
200 {"status": "ok", "service": "kromarag-api", "version": "0.1.0"}
# p95 < 50ms. Cloud Run livenessProbe: path /healthz, period 30s, timeout 5s, failureThreshold 3.

# GET /ready, GET /readyz — readiness. No auth, no rate limit.
# 200 — pool SELECT 1 ok:
200 {"ready": true, "db": "ok",
     "pool": {"min": 2, "max": 20, "in_use": 3, "idle": 5},
     "models": {"gemini": "ok", "groq": "unprobed"},
     "version": "0.1.0"}
# 503 — pool down or SELECT 1 fails (orchestrator pulls instance):
503 {"ready": false, "db": "down", "pool": {...}, "models": {...}, "version": "0.1.0"}
```

- **DB ping:** `SELECT 1` via `acquire(timeout=5)` (existing `db_ping()`); failure → `503` in `<300ms` (never hang the probe).
- **Model availability probes:** best-effort, cached 60s, never fail `readyz` alone — surface as `"degraded"` value: `"models": {"gemini": "degraded", "groq": "ok"}` when fallback rate is elevated. Rationale: an LLM outage must trigger fallback + alerts, not mass instance eviction (which would convert a provider incident into a self-inflicted capacity incident).
- **Version contract:** `version` echoes `FastAPI(version=…)` + image tag (additive `image` field permitted: `{"version":"0.1.0","image":"git-abc1234"}`). Breaking contract change → bump + spec-first (`02 §6` retained).

### 5.2 Structured JSON Logging (production shape)

```jsonc
// One line per request to stdout (Cloud Run ingests). NEVER raw sub/email/JWT/content/API keys.
{"ts": "2026-09-16T12:00:00.123Z", "level": "INFO", "trace_id": "uuid",
 "route": "POST /api/v1/query", "sub_hash": "sha256:abc…", "roles": ["viewer"],
 "latency_ms": 812, "model": "gemini-3.6-flash", "fallback_used": false,
 "top_k": 6, "history_tokens": 210, "status": 200}
// Fallback / failure examples:
{"level": "ERROR", "trace_id": "…", "route": "POST /api/v1/query",
 "code": "LLM_TIMEOUT", "model": "groq", "fallback_used": true, "latency_ms": 9400}
{"level": "ERROR", "trace_id": "…", "route": "GET /readyz", "code": "DB_UNAVAILABLE"}
{"level": "WARNING", "trace_id": "…", "route": "POST /api/v1/query", "code": "RATE_LIMITED"}
```

Rules: `trace_id` = per-request UUID, echoed as `X-Trace-Id` response header AND SSE `done.trace_id` (existing contract). `sub_hash = sha256(sub + salt_PEPPER?)` truncated 16 chars (never raw `sub`). Content excerpts `≤ 500` chars, only on `ERROR` with `redacted=true` marker. `DATABASE_URL` redaction enforced by scrubber (any value containing `://.*:.*@` replaced with `[redacted-dsn]` before emit).

### 5.3 Production Tracing (LangSmith) & Metrics

- Env prod: `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_PROJECT=KromaRAG`, `LANGSMITH_API_KEY` (Secret Manager), `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com`. Tracing client initialized once at startup; per-request runs attached in background task (stream never awaits flush; flush on `lifespan` shutdown with 5s cap).
- Span tree per `POST /query` (extends `04 §1`): `kromarag.query{trace_id}` → `router` → `history.load{msg_count,truncated}` → `retriever.sql{top_k,score_min,latency_ms}` → `embed{model,cached}` → `generator.gemini|groq{fallback_used,usage}` → `guardrail{flags}` → `evaluator{faithfulness,relevance,leakage}`. Metadata `{trace_id, sub_hash, roles, top_k, model, fallback_used, chunk_ids, scores, history_tokens}`.
- Extra spans: `ingest.parse`, `ingest.embed{chunks,model}`, `doc.reindex{from_version,to_version}`, `doc.purge{chunks_deleted}`, `migrate{files,ms}`, `readyz.probe{db_ms}`.
- Cost table pinned in `app/core/costs.py` (indicative per 1M, review quarterly): `gemini-3.6-flash $1.50/$7.50`, `openai/gpt-oss-120b $0.15/$0.60`, `gemini-embedding-001 $0.02/1M tokens`, legacy `gemini-2.0-flash $0.10/$0.40` + `groq llama-3.3-70b $0.59/$0.79` retained for history. Alerts: p95 `cost/query > $0.02` or `fallback rate > 15%` (10min window) → `WARNING`; sustained 30min → page.
- Dashboards (Cloud Run + LangSmith + Neon): request rate/latency p50/p95/p99 by route; `429` rate by bucket; `503` rate by `code`; fallback %; pool `in_use/wait queue`; `readyz` flap count; migration duration; Vercel Web Vitals (LCP/CLS) + API edge latency.

---

## 6. Environment Variables & Secrets Matrix (production strictly-enforced checklist)

`ENV=production` boot guard: every `Required: yes` row MUST be set, non-empty, and differ from its documented dev default — else `RuntimeError` fail-closed. `Previous` secrets MAY be empty outside a rotation window.

| Variable | Required prod | Source | Default (dev) → Prod rule | Notes |
|---|---|---|---|---|
| `ENV` | yes | env | `development` → `production` | Flips all fail-closed guards on. |
| `DATABASE_URL` | yes | Secret Manager | `…@localhost…` → pooled Neon `…-pooler.…?sslmode=require` | Asyncpg form `postgresql+asyncpg://`. Never logged. Must contain `-pooler` + `sslmode=require` in prod or boot fails. |
| `DATABASE_URL_SYNC` | yes (jobs) | Secret Manager | unset → direct Neon `postgresql://` non-pooled | `migrate.py` / `ANALYZE` / `reembed.py` only. |
| `PGVECTOR_DIM` | yes | env | `768` (pinned) | Mismatch vs `vector_dims` sample → boot `WARNING` + `readyz` degraded. |
| `JWT_ISSUER` | yes | env | `http://localhost:3000` (dev bypass) → `https://<app>.clerk.accounts.dev` (Clerk prod/dev app) | Must be `https://` + non-localhost in prod (boot fail otherwise). |
| `JWT_AUDIENCE` | yes | env | `kromarag-web` (same prod) | Template carries it; mismatch → `401` storm; verify in deploy smoke. |
| `CLERK_JWKS_URL` | no | env | empty → derive `{JWT_ISSUER}/.well-known/jwks.json` | Optional explicit JWKS pin. |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | yes (web) | Vercel env | `pk_test_…` (dev/preview) → `pk_live_…` (prod) | Per-env Clerk app key; baked at build — one web build per env. |
| `CLERK_SECRET_KEY` | yes (web server + smoke runner) | Vercel env (server-only) / smoke runner env | `sk_test_…` → `sk_live_…` | Never in Cloud Run env (backend needs no Clerk secret for JWKS). Smoke runner only for `sign_in_tokens` tickets. |
| `CORS_ORIGINS` | yes | env | `http://localhost:3000` → `https://kromarag.vercel.app` | Exact origins, comma-separated. `*` in prod → boot fail. |
| `NEXT_PUBLIC_API_URL` | yes (web) | Vercel env | `http://localhost:8000` → `https://<cloud-run-host>` | Build-time baked — one web build per env. |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | yes (either) | Secret Manager | empty → prod fail | At least one must verify against Generative Language API at deploy smoke. |
| `GEMINI_MODEL` | yes | env | `gemini-3.6-flash` | Pin; change = new deploy + smoke eval. |
| `GROQ_API_KEY` | yes | Secret Manager | empty → prod fail (fallback is load-bearing) | Deploy smoke MUST force one fallback call. |
| `GROQ_MODEL` | yes | env | `openai/gpt-oss-120b` | Pin; `404-shape` → fallback-chain alert, not silent model swap. |
| `EMBEDDING_MODEL` | yes | env | `gemini-embedding-001` | Change requires reindex spec + migration (never silent). |
| `LANGCHAIN_TRACING_V2` | yes | env | `false` → `true` | `false` in prod → boot `WARNING` (degraded observability). |
| `LANGSMITH_API_KEY` | yes | Secret Manager | empty → prod `WARNING` + stdout-only fallback | Required for prod tracing SLO; missing >24h → page. |
| `LANGCHAIN_PROJECT` | yes | env | `KromaRAG` | Preview envs use `KromaRAG-staging`. |
| `RATE_LIMIT_QUERY_PER_MIN` | no | env | `100` | Lower only with product sign-off. |
| `RATE_LIMIT_INGEST_PER_MIN` | no | env | `20` | Shared by ingest/upload/reindex buckets. |
| `RATE_LIMIT_UNAUTH_PER_MIN` | no | env | `10` | IP-keyed. |
| `RATE_LIMIT_ADMIN_PER_MIN` | no | env | `300` | Still capped (no uncapped admin). |
| `LLM_TIMEOUT_S` / `LLM_FALLBACK_TIMEOUT_S` / `EMBED_TIMEOUT_S` / `RETRIEVER_TIMEOUT_MS` | no | env | `25 / 12 / 8 / 3000` | Raise only with latency-budget review. |
| `POOL_MIN_SIZE` / `POOL_MAX_SIZE` / `POOL_COMMAND_TIMEOUT` | no | env | `2 / 20 / 10` | `MAX` × `max-instances` must fit Neon `max_connections`. |
| `HISTORY_MAX_TURNS/CHARS/TOKENS` | no | env | `10 / 6000 / 1500` (`06 §2.6`) | Unchanged. |
| `DOC_UPLOAD_MAX_BYTES` / `URL_MAX_BYTES` | no | env | `20971520 / 50000` (`07 §2.6`) | Unchanged. |
| `API_HOST` / `API_PORT` | no | env | `0.0.0.0 / 8000` | Container listens `8000`; Cloud Run maps `$PORT` → entrypoint MUST honor `$PORT` when set. |
| `WEB_CONCURRENCY` | no | env | `1` | `1` on Cloud Run; gunicorn hosts only may raise. |

Secret-generation runbook (normative): `openssl rand -base64 48` for the local dev-bypass `AUTH_SECRET` only (same value in `apps/web/.env.local` + `apps/api/.env`, never Vercel/Cloud Run); Clerk keys rotate in the Clerk dashboard; Neon + provider keys rotate in vendor console then Secret Manager revision; Cloud Run **new revision** (never in-place env patch without revision note); Vercel env change → redeploy web; verify with §7 smoke (health, authed Clerk query, forced fallback, kid-rollover drain check) before marking deploy green.

---

## 7. Acceptance Criteria (Gherkin)

```gherkin
Feature: Production Hardening & Deployment

  Background:
    Given prod image tags ":git-<sha>" for api and web built from clean tree
      And migrations 001, 002, 003 applied via entrypoint against pooled Neon clone
      And ENV=production with Secrets Matrix §6 satisfied (fail-closed guards armed)
      And users "alice" (roles ["viewer"]), "ed" (roles ["editor"]), "admin" (roles ["admin"]) with valid JWTs

  Scenario: Rate-limit breach returns 429 with Retry-After and headers
    When "alice" POSTs /api/v1/query 101 times within 60s with distinct trace_ids
    Then at least one response is 429 {"code":"RATE_LIMITED"}
      And the 429 carries headers X-Trace-Id, Retry-After (>0), X-RateLimit-Limit, X-RateLimit-Remaining: 0
      And a structured WARNING log with code RATE_LIMITED and matching trace_id exists
      And GET /healthz during the burst still returns 200 (probes exempt)
    When "ed" POSTs /api/v1/documents/upload 21 times within 60s
    Then at least one response is 429 with Retry-After

  Scenario: CORS and security headers enforced
    Given CORS_ORIGINS is exactly "https://kromarag.vercel.app"
    When a browser preflight OPTIONS /api/v1/query with Origin "https://evil.example"
    Then response carries no Access-Control-Allow-Origin for the evil origin
    When GET /healthz is fetched
    Then response includes Strict-Transport-Security (max-age>=63072000), X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy, and Content-Security-Policy containing "frame-ancestors 'none'"
      And response carries no Server or X-Powered-By header

  Scenario: LLM fallback executes Gemini 3.6 Flash -> Groq openai/gpt-oss-120b
    Given Gemini endpoint forced to timeout (chaos: 15s blackhole) and GROQ_API_KEY valid
    When "alice" POSTs /api/v1/query {"query":"What is KromaRAG?"} (Accept: text/event-stream)
    Then SSE yields exactly one "source" then zero or more "token" then one "error" with code LLM_TIMEOUT then one "done"
      And "done".usage.model equals "openai/gpt-oss-120b" with fallback_used true
      And an ERROR structured log with trace_id and fallback_used true exists
      And GET /api/v1/conversations/{done.conv_id}/messages shows exactly one persisted (user, assistant) pair (no duplicate)
    Given both Gemini and Groq forced down
    When "alice" POSTs /api/v1/query {"query":"hello world"}
    Then SSE yields source:[] then error then friendly tokens then done (transport 200, generation degraded)

  Scenario: Timeout budgets respected per hop
    Given EMBED_TIMEOUT_S=8 and RETRIEVER_TIMEOUT_MS=3000 and LLM_TIMEOUT_S=25 and LLM_FALLBACK_TIMEOUT_S=12
    When retriever SQL is artificially slowed to 6s
    Then POST /api/v1/query terminates the SQL hop at ~3s with 503 {code:DB_UNAVAILABLE} or degraded SSE error (never a hung stream >25s total)
      And X-Trace-Id is present and latency_ms in logs is <= 26000

  Scenario: Zero-downtime database migrations 001, 002, 003
    Given a staging Neon branch seeded with 001-only schema plus legacy status rows ('ready') and sample docs/chunks
    When the prod entrypoint runs migrate (001 -> 002 -> 003) while old-revision traffic replays reads
    Then all three files apply idempotently; re-running migrate appends zero errors
      And legacy 'ready' rows read back as 'indexed'; 002 backfills updated_at and constraints hold; 003 adds version/chunk_config/embedding columns with CHECKs
      And orphan check (chunks with no parent doc) equals 0 and vector_dims sample equals 768
      And concurrent second migrator blocks on advisory lock then exits cleanly (no interleaved DDL)
      And rollback needs no DROP: a forward-fix file would be the only permitted recovery

  Scenario: Health check failures gate traffic correctly
    Given the API pool pointed at an unreachable DSN (chaos: firewall Neon)
    Then GET /healthz still returns 200 {"status":"ok"} in <50ms
      And GET /readyz returns 503 {"ready":false,"db":"down"} with pool stats and version
      And POST /api/v1/query returns 503 {"code":"DB_UNAVAILABLE"} with Retry-After: 5
    When connectivity is restored
    Then GET /readyz returns 200 {"ready":true,"db":"ok"} without container restart (pool re-acquires)

  Scenario: Key rollover without downtime (Clerk kid-rollover)
    Given JWKS current-set kid K1 and cached prev-set kid K0 with an active K0-signed user JWT
    When K0-signed "alice" POSTs /api/v1/query
    Then response is 200 SSE (verified via prev-set) with WARNING log jwks_previous_key_used
    When Clerk publishes K1-only JWKS and sessions refresh past the 60s template TTL
    Then K1 traffic stays 200, stale-K0 replays correctly become 401, and no operator action was needed

  Scenario: Boot fails closed on insecure prod config
    Given ENV=production and JWT_ISSUER="http://localhost:3000"
    When the container boots
    Then it exits non-zero before binding traffic with ERROR log jwt_issuer_invalid
    Given ENV=production and CORS_ORIGINS="*"
    When the container boots
    Then it exits non-zero with ERROR log cors_wildcard_rejected

  Scenario: Cold-start and worker posture within budget
    Given Cloud Run min-instances=1 and single-worker Uvicorn per instance
    When a new revision receives its first POST /api/v1/query within 60s of traffic shift
    Then first-token p95 including cold penalty is within warm p95 + 3s
      And response carries valid citations [doc:Title:ord] and done.history_tokens (budgets from 01/03/06 preserved)
```

### Exit Gates (Phase mapping, `05` cross-cutting)

- `uv run pytest -q` green — 96 inherited tests + new `tests/test_prod_hardening.py` (rate-limit matrix incl. exempt probes, header presence on `200/429/503`, fallback single-attempt + `fallback_used`, timeout-slice abort, `readyz` 503 vs `healthz` 200 split, Clerk kid-rollover verify order, boot fail-closed on Clerk issuer/`*` CORS).
- `npm run build --workspace apps/web` green with `headers()` + `output:standalone` (dummy Clerk keys, no live calls); `docker build -f apps/api/Dockerfile apps/api -t kromarag-api:spec08` + `docker build -f apps/web/Dockerfile apps/web` green; `trivy image --severity HIGH,CRITICAL` = 0; images run locally with `ENV=production` + pooled-DSN placeholder and fail-closed guards demonstrably trip on non-Clerk issuer.
- `npm run spec:check` green (extended to 06/07/08: aliases `/healthz`, codes `RATE_LIMITED`/`DB_UNAVAILABLE`, Clerk `protect()` + `kromarag` template, `pgBouncer`, migration files `001/002/003`).
- Staging deploy drill green: Vercel preview → staging Cloud Run → Neon branch — login→query (forced fallback once)→upload→reindex→archive→purge→rotation-drain, with LangSmith traces + JSON-log `trace_id` join verified end-to-end; Lighthouse `≥ 85`; p95 latency/fallback/pool dashboard exports attached to release notes.
- Manual: `curl -i` header audit; `hey -z 60s -q 5` rate-burst replay; kill-Gemini chaos; kill-Neon chaos; `docker logs` secret-scrub grep (`://.*@` → zero hits).

---

## Appendix — Normative Deltas Index (for reviewers)

- **Topology:** Cloud Run + Vercel frozen normative; Render retired as target (Dockerfile fallback covers Docker hosts). `concurrency=80`, `timeout=300s`, `min-instances=1`, single Uvicorn worker/instance; Gunicorn documented for non-Cloud-Run hosts only.
- **API:** additive only — `/healthz` + `/readyz` aliases; new error codes `429 RATE_LIMITED` + `503 DB_UNAVAILABLE` (+ `Retry-After`, `X-RateLimit-*`, `X-Trace-Id` everywhere). No change to SSE framing, RBAC predicates, `indexed`-only retrieval, `404`-oracle, history/fallback persistence semantics (`06`/`07` retained verbatim).
- **Security:** exact CORS (no `*` in prod), 8-header set, trusted-hosts, tiered rate limits (100 query / 20 ingest / 10 unauth / 300 admin), dual-secret `CURRENT/PREVIOUS` + JWKS `kid` overlap with 24–48h runbook, fail-closed boot on defaults.
- **Resilience:** timeout slices `8s embed / 3s SQL / 15s primary / 12s fallback` inside `25s` total; exactly-once fallback on retryable `LLMError` only; pool `2/20/10s` + `statement_cache_size=0` for pgBouncer txn mode; `readyz` 503 pulls instance, `healthz` stays 200.
- **DB ops:** pooled runtime DSN (`-pooler` + `sslmode=require`) vs direct job DSN; advisory-locked `001→002→003` entrypoint; additive-only DDL; orphan-zero + dim-768 post-verify; `ANALYZE` after bulk.
- **Web:** Vercel primary; `standalone` Dockerfile fallback; immutable `/_next/static`, `no-store` on APIs/SSE, ISR-30s on list reads; `NEXT_PUBLIC_*` per-env builds; preview→staging wiring (never prod DB).
- **Observability:** JSON log schema + DSN/PII scrubbers; LangSmith sampling `1.0 err / 0.1 ok`; cost/fallback alerts; `readyz`-flap + pool-wait dashboards.
- **Out of scope:** Postgres RLS, pgqueue/Redis job backend, multi-region active-active, WAF vendor config, OCR, embedding-model migration, per-tenant quotas/billing, automatic LLM prompt-version rollback.

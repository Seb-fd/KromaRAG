# 09 — Telemetry & Operations

> Production observability, distributed tracing, metrics collection, and load/stress operating standards for KromaRAG on **Cloud Run (API) + Vercel (Web) + Neon PostgreSQL + pgBouncer**. Extends `04` (LangSmith tracing, token costs, eval gates) and `08` (hardening, timeout budgets, JSON logs, `healthz`/`readyz`) with vendor-neutral **W3C Trace Context propagation**, a protected **Prometheus `/metrics`** endpoint, a frozen **RED/USE catalogue**, and normative **k6 load + chaos** scenarios for `/query` (SSE) and `/documents/upload` under peak rate limits. **Spec-first, no implementation code** — requirements, propagation rules, exposition contracts, metric names, thresholds, and runbooks only.

- **Status:** Draft (Build mode, approved 2026-09-16)
- **Refs:** `01 §2+§5` (SSE/async pipeline, p95 budgets), `02 §3–§4` (SSE framing, `X-Trace-Id`, error codes), `03 §4–§6` (hybrid SQL, agent nodes, knobs), `04 §1–§4` (LangSmith hierarchy, cost table, evals, logs/alerts), `05` (phase/exit discipline), `06 §3.1–§4.6` (leak-safe `404`, atomic writes, `trace_id` idempotency), `07 §2.2–§4.4` (lifecycle states, retriever `indexed` gate), `08 §2–§6` (topology, rate limits, timeout slices, fallback, health, secrets matrix)
- **Code truth:** `app/main.py:35-57`, `app/core/middleware.py:45-119`, `app/core/rate_limit.py:54-92`, `app/core/config.py:68-80`, `app/core/costs.py:5-16`, `app/core/logging.py:1-10`, `app/services/tracing.py:48-84`, `app/services/db.py:44-78`, `app/api/v1/query.py:202-337`, `app/api/v1/health.py:19-48`, `apps/api/pyproject.toml:6-23`
- **Decisions (approved 2026-09-16):** (1) **LangSmith primary, OTel minimal** — keep `tracing.span()` / LangSmith hierarchy as the trace system of record; add only W3C `traceparent`/`tracestate` ingress-join + egress-inject (no full OTel SDK migration Phase 1); (2) **Dual protection on `/metrics`** — `admin` JWT **AND** internal network filter (Cloud Run IAM / VPC ingress / scraper allowlist); either gate failing denies with oracle-safe codes; (3) **k6 normative** for load/stress (Locust deferred; ad-hoc `hey`/`curl` allowed only for smoke, never as SLO evidence).

---

## 1. Requirements & User Stories

### 1.1 Feature Objective

Make every production request explainable in under a minute (`trace_id` → logs → LangSmith trace → Prometheus series) without changing any frozen product contract: same SSE framing (`02 §3`), same RBAC predicates, same `indexed`-only retrieval, same leak-safe `404`, same timeout/fallback semantics (`08 §4`). Close the three gaps `04`+`08` leave open: (a) no cross-service trace linkage (FastAPI ↔ LangGraph ↔ Neon ↔ LLM providers share only an app UUID); (b) no scrapeable RED/USE metrics (only JSON logs + LangSmith); (c) no normative load/chaos bar for SSE streaming and multipart upload at rate-limit ceiling.

### 1.2 Functional Requirements

| ID | Requirement |
|----|-------------|
| TRACE-01 | API **accepts W3C `traceparent` (+ optional `tracestate`)** on every inbound request, **joins** it to the app `trace_id`, and **echoes** both `X-Trace-Id` and `traceparent` on every response incl. `429/503`, SSE stream headers, and SSE `done` payload (§3.1). |
| TRACE-02 | **LangSmith remains the trace system of record**: span tree `kromarag.query → router → history.load → retriever.sql → embed → generator.gemini\|groq → guardrail → evaluator` unchanged (`04 §1`, `08 §5.3`); OTel layer only propagates context and annotates spans with `trace_id`/`span_id` links — never replaces `tracing.span()` Phase 1. |
| TRACE-03 | Propagated context flows into **LangGraph agent runs and Neon DB queries**: `AgentState.trace_id` set from the joined context; `asyncpg` hybrid query annotated via `SET LOCAL application_name = 'kromarag:<trace_id>'` (or `/* trace_id=<id> */` SQL comment); outbound LLM `httpx` calls carry injected `traceparent` header. |
| MET-01 | `GET /metrics` exposes **Prometheus text exposition format `0.0.4`** (§3.2) with the frozen **RED/USE catalogue** (§3.3): HTTP RED, token consumption, LLM fallback rate, vector-search latency (p95 computable), DB pool utilization. Scrape-safe: monotonic counters, bounded label cardinality, no PII/JWT/content in labels. |
| MET-02 | `/metrics` enforces **dual protection** (§3.2): (1) `require_role("admin")` JWT gate **AND** (2) internal network filter (Cloud Run IAM invoker / VPC ingress `internal` / scraper IP allowlist). Missing network trust → `404` (hide existence); failing JWT → `401/403`. Rate-limit **exempt** (like probes) but still emits `X-Trace-Id`. |
| MET-03 | Every metric sample that derives from a request carries an **exemplar with `trace_id`** where the client library supports it (histogram exemplars; counter `Info` otherwise via log join). Cost series reuse the pinned table in `app/core/costs.py` (`04 §2`, `08 §5.3`). |
| LOAD-01 | Normative **k6 scenarios** in `loadtests/k6/` (§3.4): `query_sse.js` (streaming `/query` at `100/min/sub` ceiling), `upload_burst.js` (`/documents/upload` + `/ingest` at `20/min/sub` ceiling), `soak.js` (30-min mixed read/write). Thresholds in §3.4 are release-blockers. |
| LOAD-02 | **Resilience criteria under degradation** (§4.3): DB pool exhausted → `503 {code:DB_UNAVAILABLE}` + `Retry-After: 5`, `readyz` flips `503`, p99 degradation bounded; primary LLM (Gemini) degraded → exactly-once fallback to Groq with `fallback_used=true`, SSE framing intact; both LLMs down → friendly SSE triple with transport `200`. |
| LOAD-03 | Chaos hooks are **config/flag-driven, never code branches in prod**: latency injection (`CHAOS_SQL_DELAY_MS`), provider blackhole (`CHAOS_GEMINI_DOWN=1`), pool starvation (`CHAOS_POOL_MAX=1`) — test/staging only, fail-closed to `0/off` when `ENV=production`. |

### 1.3 User Stories

- **US-1 Join the trace:** As an on-call, I copy `X-Trace-Id` from a user report and find the JSON log line, the LangSmith `kromarag.query` trace, and the Prometheus exemplar for the slow `retriever.sql` hop in one jump.
- **US-2 Scrape safely:** As an operator, Prometheus scrapes `/metrics` every 15s with an admin service JWT from inside the VPC while external callers get `404` and viewers get `403` — with zero label-cardinality incidents.
- **US-3 Ship with confidence:** As a release captain, the k6 `query_sse` + `upload_burst` suites pass at rate-limit ceiling before any prod traffic shift, and the chaos runs prove fallback + pool-exhaust behavior matches §4.3.
- **US-4 Survive provider outage:** As a viewer, when Gemini times out during a streamed answer I still get Groq-backed tokens with a single mid-stream `error` notice and a correct `done.fallback_used=true`.
- **US-5 No oracle regressions:** As a security engineer, `/metrics`, thread, and document existence probes all return uniform `404`s to unauthorized callers — telemetry adds no enumeration channel.

### 1.4 Non-Functional Requirements

| Category | Target |
|----------|--------|
| Performance overhead | Tracing + metrics middleware adds p50 `≤ 2ms`, p95 `≤ 5ms` per request at `concurrency=80`; `/metrics` scrape p95 `< 100ms` with 50+ series; LangSmith flush never blocks SSE (background, 5s shutdown cap — `08 §5.3` retained). |
| Cardinality | Total active series on `/metrics` `≤ 500` in prod; label values bounded (§3.3 rules); `sub`/`email`/`doc_id`/`chunk_id`/`query` **never** label values (use `sub_hash` in logs only). Violation is a release-blocker. |
| Security | `traceparent`/`tracestate` are untrusted input (validated, never logged raw beyond `trace_id` hex); `/metrics` dual-gate enforced before any series render; raw DSN/JWT/API keys scrubbed (`://.*@` → `[redacted-dsn]`); excerpts `≤ 500` chars (`04 §1` retained). |
| Scaling | Stateless `/metrics` (per-instance counters + Cloud Run aggregated at Prometheus federation); k6 suites run from CI (small) + staging soak (large); no shared k6 state. |
| Reliability | Missing/invalid `traceparent` never fails a request (degrade to fresh `trace_id`); metrics render failure degrades to `503 {code:METRICS_UNAVAILABLE}` without breaking product routes; chaos flags inert in prod (`ENV=production` forces off). |
| Observability | 100% error traces sampled, 10% success (`OTEL_SAMPLING_*`); p95 `cost/query > $0.02` or `fallback rate > 15% / 10min` → `WARNING` (`04 §2` retained); dashboards: latency p50/p95/p99 by route, fallback %, pool wait, `readyz` flap, k6 thresholds. |
| Configurability | All §2.5 knobs env-overridable with documented defaults; `ENV=production` flips fail-closed guards (chaos off, `/metrics` dual-gate required, wildcard CORS already rejected per `08 §6`). |

---

## 2. Data Models / Config (no new tables)

No schema migration in this spec (`001/002/003` frozen). Telemetry state is process-local (counters/histograms) + existing `kroma_usage`-style log records. The only persistent-adjacent contract is the log/metric field vocabulary below.

### 2.1 Trace-context record (per request, in-memory)

```python
class TraceContext(BaseModel):  # concept only — implementation follows in code PR
    trace_id: str        # app UUID, == W3C trace-id hex when ingress traceparent valid, else fresh uuid4
    span_id: str         # 16-hex-char span id minted per request (W3C span-id format)
    traceparent_in: str | None   # validated ingress value or None
    traceparent_out: str         # "00-{trace_id_hex}-{span_id}-01" echoed downstream
    sampled: bool        # error → True always; success → OTEL_SAMPLING_OK lottery
```

`trace_id` storage format stays UUIDv4-with-dashes in `X-Trace-Id`, SSE `done.trace_id`, `kroma_messages.trace_id`, and JSON logs. The W3C hex form is the dash-stripped lowercase `trace_id.hex`. Both forms MUST join 1:1 (dash-strip compare).

### 2.2 Metric sample discipline

- Counters monotonic within process lifetime (`_total` suffix, `-sided` `+Inf` histograms for latencies).
- Latency histograms in **seconds** with buckets `LE = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25]` (covers 50ms `healthz` → 25s LLM ceiling). p95 computed server-side in Prometheus (`histogram_quantile(0.95, …)`), never pre-aggregated in-app.
- Token/cost counters in **tokens** and **USD-6dp** (reuse `cost_usd()`); never per-user series.
- All series carry `service="kromarag-api"`, `version="0.1.0"` (image tag permitted as additive `image="git-<sha>"` label on `*_info` only, never on high-frequency series).

### 2.3 Log-field extension (additive to `08 §5.2`, non-breaking)

```jsonc
// Existing 08 §5.2 line + three additive keys (traceparent, span_id, pool_wait_ms):
{"ts": "…", "level": "INFO", "trace_id": "uuid", "span_id": "16hex",
 "traceparent": "00-<32hex>-<16hex>-01", "route": "POST /api/v1/query",
 "sub_hash": "sha256:abc…", "roles": ["viewer"],
 "latency_ms": 812, "pool_wait_ms": 4, "model": "gemini-3.6-flash",
 "fallback_used": false, "top_k": 6, "history_tokens": 210, "status": 200}
```

### 2.4 TypeScript surface (additive, `apps/web/lib`)

```typescript
export type TelemetryHeaders = {
  traceId: string;       // X-Trace-Id echo (existing api-client.ts usage)
  traceparent?: string;  // W3C echo, forwarded on follow-up polls for log join
};
```

Web forwards a stored `traceparent` on `GET status` / `GET messages` polls following a `POST /query` so the poll joins the same trace in logs (best-effort; missing value is fine).

### 2.5 Config Knobs (env, extends `03 §6` + `06 §2.6` + `07 §2.6` + `08 §2.6` + `app/core/config.py`)

```python
otel_enabled: bool = True              # OTEL_ENABLED — W3C join/inject layer on/off
otel_sampling_ok: float = 0.1          # OTEL_SAMPLING_OK — success trace sample rate
otel_sampling_error: float = 1.0       # OTEL_SAMPLING_ERROR — error trace sample rate (keep 1.0)
metrics_enabled: bool = True           # METRICS_ENABLED — serve GET /metrics (dual-gated)
metrics_scrape_allowlist: str = ""     # METRICS_SCRAPE_ALLOWLIST — comma-separated CIDR/IP for network filter
chaos_sql_delay_ms: int = 0            # CHAOS_SQL_DELAY_MS — test/staging only, forced 0 in prod
chaos_gemini_down: bool = False        # CHAOS_GEMINI_DOWN — force primary failure (staging chaos)
chaos_pool_max: int = 0                # CHAOS_POOL_MAX — override pool max in chaos runs (0 = follow POOL_MAX_SIZE)
k6_query_vus: int = 20                 # K6_QUERY_VUS — default VU level for query_sse.js
k6_upload_vus: int = 5                 # K6_UPLOAD_VUS — default VU level for upload_burst.js
```

`ENV=production` boot guard additions (fail-closed): `CHAOS_*` non-default → `RuntimeError`; `METRICS_ENABLED=true` without admin-gate code path → `RuntimeError` (defense: code always gates; guard asserts the route is registered with `require_role("admin")`).

---

## 3. System Interface & Contracts

Base `{API_URL}`. Auth per-route per `02`/`06`/`07`; errors `02 §4` shape `{"detail":{"code","message"}}` + `X-Trace-Id` on **every** response. This spec adds `traceparent` echo and `429/503`-safe propagation (§3.1), the `/metrics` route (§3.2), the frozen series catalogue (§3.3), and k6 file/threshold contracts (§3.4).

### 3.1 W3C Trace Context Propagation (minimal-OTel layer, normative)

**Ingress (all routes incl. `/health*`/`/ready*`/`/metrics`):**

1. Read `traceparent` header. Valid iff it matches `^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$` (case-insensitive, lowercased before use) with `version == "00"` and `trace-id != 0*32`, `span-id != 0*16`. Anything else (missing, malformed, `ff` version, all-zero ids) → **ignore and mint fresh** (never `400`).
2. `tracestate` accepted opaquely (≤512 chars, else truncated); forwarded downstream verbatim when present and valid; never used for sampling decisions Phase 1.
3. Join rule: when ingress valid, app `trace_id = str(UUID(hex=trace-id-part))` (dash form); else `trace_id = str(uuid4())`. Mint per-request `span_id = secrets.token_hex(8)` (16 hex chars). Compute `traceparent_out = f"00-{trace_id.hex}-{span_id}-{'01' if sampled else '00'}"`.
4. Sampling: `sampled = True` when the request ends `ERROR`/`5xx`/fallback (`OTEL_SAMPLING_ERROR=1.0`); else lottery `random() < OTEL_SAMPLING_OK (0.1)`. The ingress `sampled` flag is preserved into logs as `traceparent_in_sampled` but does NOT override server sampling Phase 1 (documented divergence, avoids caller-forced retention).
5. Precedence with existing middleware (`app/core/middleware.py:45-58`): the `SecurityHeadersMiddleware` trace-id attach point becomes the join point — if `request.state.trace_id` already set from `x-trace-id` echo, the W3C join **wins** for canonical `trace_id` and `x-trace-id` is re-emitted with the joined value (one canonical id per request, no forks).

**Egress + in-process fan-out:**

6. Every HTTP response (incl. `401/403/404/422/429/503`, `GET /healthz`, `GET /metrics` denies) carries **both** `X-Trace-Id: <uuid-dash>` and `traceparent: <traceparent_out>`. SSE stream open headers carry both; `done.data` keeps `trace_id` (existing) and gains additive `traceparent` field (`done.data.traceparent`, non-breaking).
7. `AgentState.trace_id` is set from the joined context at `_initial_state()` (`app/api/v1/query.py:165-178` extension point); all LangSmith spans for the run carry `metadata.trace_id` = joined id (existing `tracing.span("kromarag.query", {"trace_id": …})` call site unchanged in shape).
8. Neon DB hop: before the hybrid query, execute `SET LOCAL application_name = 'kromarag:<trace_id_hex8>'` (8-char prefix keeps `application_name` short and joinable in `pg_stat_activity`) **and** append a `/* trace_id=<uuid> */` SQL comment to the `SELECT` text. Either join key suffices; both are best-effort (failure swallowed, fakes-compatible).
9. Outbound LLM `httpx` calls (Gemini embed/generate, Groq fallback) inject `traceparent: <traceparent_out>` + `tracestate` (when present). Provider header-allowlist additions are additive; provider rejection of unknown headers is tolerated (strip-and-retry once without `tracestate`).

**Examples (non-breaking, additive headers bold):**

```http
POST /api/v1/query  Authorization: Bearer <viewer JWT>
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
→ 200 SSE  X-Trace-Id: 4bf92f35-77b3-4da6-a3ce-929d0e0e4736
→ **traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-<server-span>-01**
  event: done
  data: {"conv_id":"…","trace_id":"4bf92f35-77b3-4da6-a3ce-929d0e0e4736",
         **"traceparent":"00-4bf92f3577b34da6a3ce929d0e0e4736-<server-span>-01",**
         "usage":{…},"fallback_used":false,…}

GET /metrics  Authorization: Bearer <admin JWT>
→ 200 Content-Type: text/plain; version=0.0.4  X-Trace-Id: <fresh-uuid>  **traceparent: 00-…**
```

### 3.2 `/metrics` Endpoint Contract (Prometheus, dual-protected)

| Aspect | Normative rule |
|---|---|
| Route | `GET /metrics`. No trailing-slash variant. Not under `/api/v1` (ops plane, like `/healthz`). |
| Format | Prometheus text exposition `0.0.4`: `Content-Type: text/plain; version=0.0.4; charset=utf-8`. Lines `# HELP` + `# TYPE` per series family, then samples with optional `#{trace_id="…"}` exemplars on histograms. |
| Auth gate (1/2) | `Depends(require_role("admin"))` — `viewer`/`editor` valid JWTs → `403 {"code":"FORBIDDEN"}`; missing/bad JWT → `401 {"code":"UNAUTHENTICATED"}` (identical shape to `02 §4`). |
| Network gate (2/2) | Evaluated **before** JWT (cheapest deny first): allow iff request arrives via trusted ingress — Cloud Run IAM invoker identity present **OR** `X-Forwarded-For`/direct IP ∈ `METRICS_SCRAPE_ALLOWLIST` **OR** VPC internal header (`X-Cloud-Run-Internal: true` equivalent per platform). Miss → `404 {"code":"NOT_FOUND","message":"not found"}` — byte-identical to a random nonexistent route (hide existence; `06`/`07` oracle pattern). |
| Gate order | `Network filter → Auth → Render`. Both must pass. `404` (network) takes precedence over `401/403` (JWT) so external scanners learn nothing about valid credentials. |
| Rate limits | **Exempt** from all `08 §3.2` buckets (scrape traffic must never `429`), same as `/health*`/`/ready*`. Still emits `X-Trace-Id` + `traceparent` + `X-RateLimit-*` informational headers (`Limit: admin-scrape`, `Remaining` omitted → send `X-RateLimit-Limit: 0` to signal exemption). |
| Security headers | Full `08 §3.1` set applies (HSTS/CSP/nosniff/deny-frame/…). `Cache-Control: no-store` (never cache credentials-adjacent series). |
| Failure mode | Render exception → `503 {"code":"METRICS_UNAVAILABLE","message":"metrics temporarily unavailable"}` + `Retry-After: 5`. Product routes unaffected. |
| Cardinality guard | Render path counts active series; if `> 500`, emit the low-cardinality subset (drop `route`-split histograms first, keep `*_total` counters + `*_info`) and set `kromarag_metrics_cardinality_shed_total += 1`. |

```text
# Example scrape (abridged — full catalogue in §3.3):
# HELP http_server_duration_seconds Request latency by route and code.
# TYPE http_server_duration_seconds histogram
http_server_duration_seconds_bucket{service="kromarag-api",route="POST /api/v1/query",code="200",le="0.5"} 41
http_server_duration_seconds_bucket{service="kromarag-api",route="POST /api/v1/query",code="200",le="+Inf"} 58 # {trace_id="4bf92f35…"} 0.412
# HELP llm_fallback_total Primary→fallback transitions by reason.
# TYPE llm_fallback_total counter
llm_fallback_total{service="kromarag-api",from_model="gemini-3.6-flash",to_model="openai/gpt-oss-120b",code="LLM_TIMEOUT"} 3
```

### 3.3 RED/USE Catalogue (frozen names, types, labels)

Conventions: every series carries `service="kromarag-api"`. `route` values are the **route template** (`POST /api/v1/query`, `POST /api/v1/documents/upload`, `GET /api/v1/documents`, … — never raw paths with ids). `code` is the numeric HTTP status as string (`"200"`, `"429"`, `"503"`). `model` values are pinned (`gemini-3.6-flash`, `openai/gpt-oss-120b`, `gemini-embedding-001`; legacy `gemini-2.0-flash`, `llama-3.3-70b-versatile` permitted on history series). Forbidden label values: raw `sub`/`email`/`doc_id`/`chunk_id`/`conv_id`/`query`/JWT.

| Series | Type | Labels | Purpose / SLO join |
|---|---|---|---|
| `http_server_duration_seconds` | histogram (`LE` §2.2) + exemplar `trace_id` | `service, route, code` | **R**ate/**E**rrors/**D**uration per route; p95 via `histogram_quantile(0.95, sum by (route,le)(rate(…[5m])))`. Budgets: first-token `<2.5s`, retrieval `<400ms` (`01 §5`). |
| `http_server_requests_total` | counter | `service, route, code` | Request rate + error ratio (`code=~"5.."`). Alert: `5xx > 1% / 5min`. |
| `http_rate_limited_total` | counter | `service, bucket` (`query\|ingest\|read\|unauth`) | `429` abuse signal per `08 §3.2` bucket. |
| `llm_tokens_total` | counter | `service, model, kind` (`prompt\|completion`) | **Token consumption** per model. Cost derived: `tokens × costs.py` rate. Alert: p95 `cost/query > $0.02` (`04 §2`). |
| `llm_fallback_total` | counter | `service, from_model, to_model, code` (`LLM_TIMEOUT\|LLM_RATE_LIMIT\|LLM_ERROR`) | **LLM fallback rate** (Gemini → Groq). Alert: `> 15% / 10min` warn, `> 40%` critical (`08 §4.2`). |
| `llm_request_duration_seconds` | histogram + exemplar | `service, model, outcome` (`ok\|fallback\|error`) | Primary vs fallback latency split; fallback slice `≤ 12s` (`08 §4.1`). |
| `vector_search_duration_seconds` | histogram + exemplar | `service, outcome` (`ok\|timeout\|error`) | **Vector search latency** (hybrid SQL hop); p95 `< 400ms` @100K chunks (`03 §4`). `timeout` counts `RETRIEVER_TIMEOUT_MS` aborts. |
| `embed_duration_seconds` | histogram | `service, model, cached` (`true\|false`) | Embed hop; slice `≤ 8s` (`08 §4.1`); `cached=true` tracks LRU-500 hits (`03 §3`). |
| `db_pool_in_use` | gauge | `service` | **DB pool utilization** (instant). Alert: `in_use/max > 0.8` sustained 5min. |
| `db_pool_idle` | gauge | `service` | Idle connections (from `pool_stats()`). |
| `db_pool_wait_seconds` | histogram | `service, outcome` (`acquired\|timeout`) | Pool acquire wait; `timeout` joins `503 DB_UNAVAILABLE`. |
| `db_errors_total` | counter | `service, code` (`DB_UNAVAILABLE\|DB_TIMEOUT`) | Pool-exhaust / `SELECT 1` failure signal. |
| `sse_stream_duration_seconds` | histogram | `service, route, outcome` (`done\|disconnect\|error`) | SSE stream lifetime (open → `done`); `disconnect` counts `is_disconnected()` breaks (`06 §4.6`). |
| `sse_tokens_total` | counter | `service, model` | Streamed token frames per model (volume proxy). |
| `history_tokens_total` | counter | `service` | Injected history tokens (`06 §4.4` caps observable). |
| `doc_reindex_total` | counter | `service, outcome` (`ok\|failed\|conflict`) | Reindex jobs; `conflict` = `409 REINDEX_IN_PROGRESS` (`07 §4.7`). |
| `doc_purge_total` | counter | `service` | Hard purges (audit; joins orphan-zero verify `07 §4.3`). |
| `kromarag_info` | info/gauge `1` | `service, version, image` | Build identity (`version="0.1.0"`, `image="git-<sha>"` when known). |
| `kromarag_metrics_cardinality_shed_total` | counter | `service` | Times the render shed load (§3.2). Must stay `0` in steady state. |

PromQL joins (non-normative examples, shipped as dashboard JSON in code PR):

```promql
# Vector search p95 (5m window):
histogram_quantile(0.95, sum by (le) (rate(vector_search_duration_seconds_bucket[5m])))
# Fallback rate (10m window):
sum(rate(llm_fallback_total[10m])) / clamp_min(sum(rate(llm_request_duration_seconds_count[10m])), 1)
# Pool saturation:
max_over_time(db_pool_in_use[5m]) / 20  # POOL_MAX_SIZE prod default
```

### 3.4 k6 Load & Soak Contracts (normative tool + thresholds)

Location: `loadtests/k6/{query_sse.js, upload_burst.js, soak.js}` + `loadtests/k6/lib/{sse.js, auth.js}` helpers (file layout frozen; helper internals follow in code PR). Base URL + JWTs via env (`K6_BASE_URL`, `K6_VIEWER_JWT`, `K6_EDITOR_JWT`, `K6_ADMIN_JWT`); secrets never committed. k6 is the **only** SLO-evidence tool; `hey`/`curl` smoke is informational.

**`query_sse.js` — streaming `/query` at ceiling:**

- Stages (default, overridable via `K6_QUERY_VUS`): `ramp 0→20 VUs / 2min → hold 20 VUs / 8min → spike 40 VUs / 2min → recover 20 VUs / 3min`. Each VU loops `POST /api/v1/query {query from corpus, top_k: 6, stream: true}` with `Accept: text/event-stream`, parses frames, asserts `source(1) → token(0..N) → done(1)` + `done.trace_id == X-Trace-Id`.
- Load level targets the `100/min/sub` bucket: VU pool uses ≥5 distinct `sub` JWTs so no single bucket `429`s the suite by design; a dedicated 60s `429`-probe stage (single `sub` at `150/min`) asserts `429 + Retry-After>0 + X-Trace-Id` then backs off.
- Thresholds (release-blockers): `http_req_failed < 1%` (excluding intentional 429-probe), `sse_first_token_p95 < 2500ms`, `sse_done_p95 < 26000ms` (25s budget + 1s harness slack), `checks{source_once:1,done_once:1} rate > 99%`.

**`upload_burst.js` — `/documents/upload` + `/ingest` at ceiling:**

- Stages: `ramp 0→5 VUs / 1min → hold 5 VUs / 5min` (≈`20/min/sub` per editor `sub`, 2 editor JWTs). Payloads: 50KB txt + 2MB md + 8MB pdf fixtures (repo-committed, PII-free). Each upload asserts `202 {doc_id, job_id}` then polls `GET /{id}/status` until terminal (`indexed`, timeout 120s).
- Thresholds: `http_req_failed < 1%`, `upload_accept_p95 < 500ms` (time to `202`, not to `indexed`), `status_poll_p95 < 100ms`, zero `413` on in-fixture files, zero `422` on allowlisted mimes.

**`soak.js` — 30-min mixed:**

- Mix: 80% `GET /documents` + `GET /conversations*` (read bucket `300/min`), 15% `POST /query` (non-stream every 3rd), 5% `PATCH` title rotation on fixture docs. Constant `10 VUs / 30min`.
- Thresholds: no `5xx` growth (`http_req_failed` flat), `readyz` flap count `== 0`, memory/pool stable (`db_pool_in_use` p95 `< 0.8 × max`), LangSmith error-sample `100%` captured.

**Common k6 rules:** thresholds evaluated with `--summary-export`; suite fails the pipeline on breach (non-zero exit). Every k6 HTTP call sends a unique `traceparent` (k6 `uuidv4` → `00-…-…-01`) so load-test traffic joins production-style traces and is filterable (`service + k6_run_id` log field). Load runs target **staging** by default; prod runs require explicit `K6_TARGET=prod` + change-window approval + `min-instances ≥ 1` verified.

---

## 4. Business Logic & Edge Cases

### 4.1 Sampling, Retention & Cost Discipline

1. Sampling decision is made at response time (outcome known): any request ending with `fallback_used=true`, `5xx`, `429`-probe-failure, or `code ∈ {LLM_*, DB_UNAVAILABLE, RATE_LIMITED-burst}` forces `sampled=true`; else `random() < OTEL_SAMPLING_OK`.
2. LangSmith background flush unchanged (`08 §5.3`): stream never awaits flush; `lifespan` shutdown flushes with 5s cap. OTel inject calls are synchronous-but-trivial (string format); zero network I/O on the request path Phase 1 (no OTLP exporter yet — deferred).
3. Cost accounting reuses `cost_usd()` (`app/core/costs.py:14-16`); `/metrics` exposes tokens, dashboards multiply by pinned rates. Rate changes require a spec note + `costs.py` bump (quarterly review per `04 §2`).

### 4.2 Cardinality & Scrub Enforcement (normative order per request)

1. Build label set from route template + numeric code + pinned model only. Any dynamic value (ids, titles, queries, excerpts) → **drop the series increment** and bump `kromarag_metrics_cardinality_shed_total` (fail-safe toward under-counting, never toward explosion).
2. Scrub before emit: DSN regex `://[^@\s]+@` → `[redacted-dsn]`; JWT-shaped `eyJ…` → `[redacted-jwt]`; excerpts truncated `≤ 500` chars with `… [truncated]` marker. `sub` hashed (`sha256[:16]`) for logs, **absent** from metric labels.
3. `tracestate` echoed verbatim but capped: ingress `> 512` chars → forward first `512` + log `WARNING trace_state_truncated` (no PII — length only).

### 4.3 Degradation & Chaos Matrix (extends `08 §4.1–§4.2`, k6-verified)

| Case | Behavior (SLO-relevant) |
|------|--------------------------|
| Pool exhausted (`acquire timeout=5s`, `command_timeout=10s`) | Request fast-fails `503 {"code":"DB_UNAVAILABLE"}` + `Retry-After: 5` + both trace headers; `db_pool_wait_seconds{outcome="timeout"} += 1`, `db_errors_total{code="DB_UNAVAILABLE"} += 1`; `readyz` → `503 {"ready":false,"db":"down"}` so orchestrator pulls the instance (`08 §4.4` retained). k6 soak asserts p99 of 503-path `< 6s` (no hung streams). |
| Retriever SQL slowed (chaos `CHAOS_SQL_DELAY_MS=6000`) | `SET LOCAL statement_timeout=3000ms` aborts the hop at ~3s; query terminates via the 503/DB path, never hangs to 25s total; `vector_search_duration_seconds{outcome="timeout"} += 1`; `X-Trace-Id` + exemplar join the slow trace. |
| Gemini retryable failure (`timeout/429/404-shape/5xx`) | Exactly one Groq attempt (`openai/gpt-oss-120b`, `≤ 12s`); SSE keeps `source(1)` already sent → `token*(groq)` → `error{code:LLM_FALLBACK}` (informational) → `done{fallback_used:true, model:groq}`; `llm_fallback_total{…,code} += 1`, `ERROR` log with `trace_id` (`query.py:255-260` shape retained). |
| Both LLMs down | Transport stays `200 SSE`: `source:[] + error{code} + friendly tokens + done` (`08 §4.2` rule 4); pair persisted per `06 §4.2`; `llm_request_duration_seconds{outcome="error"} += 1`; `CRITICAL` alert. |
| Rate breach during k6 peak | `429 {"code":"RATE_LIMITED"}` + `Retry-After` + `X-RateLimit-*` + both trace headers; `http_rate_limited_total{bucket} += 1`; probes + `/metrics` scrape unaffected (exempt). |
| Invalid `traceparent` (fuzzed) | Ignored; fresh `trace_id` minted; request served normally; `WARNING traceparent_invalid` sampled at 1% (length/format only, never the raw value). |
| `/metrics` render overload | Shed to low-cardinality subset + `kromarag_metrics_cardinality_shed_total += 1`; scrape returns `200` degraded (never `500` for cardinality). |
| Chaos flags in prod | Boot `RuntimeError` (`chaos_enabled_in_production`) before binding; staging runs gate on explicit `CHAOS_*` env + `ENV!=production` assertion in the k6 setup hook. |

### 4.4 Upload / Reindex Under Load (extends `07 §4.2–§4.3`)

- `ingest` bucket (`20/min/sub`) applies **before** size/mime checks (abuse rejected cheapest-first — `08 §4.3` retained). `409 REINDEX_IN_PROGRESS` carries `Retry-After: 10` so k6 pollers back off instead of hot-looping `GET status`.
- Reindex swap stays single-transaction (`07 §4.2` rule 3): load-test queries during `indexing` see exactly one version (never a v1+v2 mix); `doc_reindex_total{outcome}` records the result.
- Purge while `pending/indexing` → `409 ARCHIVE_CONFLICT` (unchanged); orphan invariant (`chunks with no parent == 0`) verified post-soak and logged.

### 4.5 Validation, Fallbacks, Error Handling

| Case | Behavior |
|------|----------|
| Missing/expired JWT, bad `iss/aud`, missing `sub` | `401 UNAUTHENTICATED` (unchanged `02 §5`); consumes `unauth` IP bucket; trace headers still emitted. |
| `viewer` on `editor+` route | `403 FORBIDDEN` (role gate — the ONLY `403` for routes; `06`/`07` `404`-oracle rules unchanged). |
| Guessed UUID / foreign doc/thread | `404 NOT_FOUND` uniform body (unchanged). |
| External caller on `/metrics` | `404 NOT_FOUND` uniform (network gate, §3.2) — identical body to random nonexistent route. |
| Non-admin JWT on `/metrics` from trusted network | `403 FORBIDDEN` (auth gate, §3.2). |
| `traceparent` malformed | Ignored per §3.1 (never `4xx`). |
| Metrics lib missing at import | `/metrics` → `503 METRICS_UNAVAILABLE`; product routes unaffected; boot succeeds with `WARNING metrics_degraded`. |

---

## 5. Acceptance Criteria (Gherkin)

```gherkin
Feature: Telemetry & Operations

  Background:
    Given prod image ":git-<sha>" with 001/002/003 migrated and ENV=production guards armed
      And users "alice" (roles ["viewer"]), "ed" (roles ["editor"]), "svc" (roles ["admin"]) with valid JWTs
      And PGVECTOR_DIM 768, hybrid weights 0.7/0.3, timeout slices 8s/3s/15s/12s inside 25s total

  Scenario: W3C traceparent joins the app trace_id end to end
    When "alice" POSTs /api/v1/query {"query":"What is KromaRAG?"} with traceparent "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    Then SSE open headers include X-Trace-Id "4bf92f35-77b3-4da6-a3ce-929d0e0e4736" and traceparent starting "00-4bf92f3577b34da6a3ce929d0e0e4736-"
      And "done".trace_id equals X-Trace-Id and "done".traceparent equals the echoed traceparent
      And the JSON log line with that trace_id carries span_id + traceparent
      And the LangSmith run kromarag.query carries metadata.trace_id equal to the trace_id
    When "alice" POSTs /api/v1/query with traceparent "bogus"
    Then response is 200 SSE with a fresh X-Trace-Id (no 400) and a well-formed traceparent echo

  Scenario: SSE error payloads and HTTP errors carry trace linkage
    Given Gemini forced to timeout and GROQ_API_KEY valid
    When "alice" POSTs /api/v1/query {"query":"fallback?"} with a valid traceparent
    Then SSE yields source(1) then error{code:LLM_FALLBACK} then done{fallback_used:true,model:"openai/gpt-oss-120b"}
      And done.trace_id == X-Trace-Id and done.traceparent is present
      And llm_fallback_total{from_model="gemini-3.6-flash",to_model="openai/gpt-oss-120b",code="LLM_TIMEOUT"} increments by 1
      And llm_tokens_total{model="openai/gpt-oss-120b"} increments for prompt+completion

  Scenario: /metrics dual protection hides existence and enforces admin scope
    When an external caller GETs /metrics with no JWT from an untrusted IP
    Then response is 404 with body identical to GET /no-such-route plus X-Trace-Id and traceparent
    When "alice" (viewer JWT) GETs /metrics from a trusted scraper IP
    Then response is 403 {"code":"FORBIDDEN"}
    When "svc" (admin JWT) GETs /metrics from a trusted scraper IP
    Then response is 200 Content-Type "text/plain; version=0.0.4" with http_server_duration_seconds, llm_tokens_total, llm_fallback_total, vector_search_duration_seconds_bucket, db_pool_in_use
      And every histogram family with exemplars carries a trace_id exemplar on at least one bucket
      And no sample contains an email, sub, doc_id, chunk_id, or query-text label value

  Scenario: k6 query_sse passes SLO thresholds at ceiling
    Given staging Cloud Run with min-instances=1 and 5 viewer JWTs and K6_BASE_URL=staging
    When k6 run loadtests/k6/query_sse.js completes (ramp→hold→spike→recover)
    Then checks source_once and done_once pass at >99%, http_req_failed <1% (excluding intentional 429-probe)
      And sse_first_token_p95 <2500ms and sse_done_p95 <26000ms
      And the intentional single-sub 150/min probe stage observed at least one 429 with Retry-After>0 and X-Trace-Id
      And GET /healthz stayed 200 throughout (probes exempt)

  Scenario: k6 upload_burst passes accept and poll budgets
    Given 2 editor JWTs and fixture files (50KB txt, 2MB md, 8MB pdf)
    When k6 run loadtests/k6/upload_burst.js completes
    Then upload_accept_p95 <500ms and status_poll_p95 <100ms and http_req_failed <1%
      And every upload reached terminal status indexed with version 1 via GET status polling
      And zero 413/422 on in-fixture files

  Scenario: Pool exhaustion degrades gracefully and recovers
    Given chaos CHAOS_POOL_MAX=1 plus k6 soak pressure (10 VUs mixed)
    When the pool saturates
    Then overflow POST /api/v1/query calls return 503 {"code":"DB_UNAVAILABLE"} with Retry-After: 5 and trace headers in <6s p99
      And GET /readyz returns 503 {"ready":false,"db":"down"} while GET /healthz stays 200
      And db_pool_wait_seconds{outcome="timeout"} and db_errors_total{code="DB_UNAVAILABLE"} increment
    When chaos is cleared
    Then GET /readyz returns 200 without container restart and soak error rate returns to baseline

  Scenario: Cardinality guard never leaks PII into labels
    When 1000 queries with distinct user subs, doc titles, and query texts complete
    Then active series on /metrics stays <=500
      And kromarag_metrics_cardinality_shed_total stays 0
      And a scrape-body grep for any test email/sub/doc_id returns zero hits
```

### Exit Gates (Phase mapping, `05` cross-cutting)

- `uv run pytest -q` green — inherited suite + new `tests/test_telemetry_ops.py` (traceparent join/mint matrix, `done.traceparent` echo, `/metrics` 404-vs-401-vs-403-vs-200 matrix, exemplar presence, cardinality-shed, pool-exhaust `503` + `readyz` split, single-attempt fallback counter increments).
- `npm run build --workspace apps/web` green (traceparent forward on status/messages polls, additive only); `docker build -f apps/api/Dockerfile apps/api` green with `prometheus-client` (or OTel exporter) pinned in `pyproject.toml`; `trivy image --severity HIGH,CRITICAL` = 0.
- `npm run spec:check` green across **9 specs** (this file additive; `02` SSE framing, `06`/`07` `404`-oracle, `08` timeout/fallback/health semantics untouched — deltas indexed below).
- Staging evidence green: k6 `query_sse.js` + `upload_burst.js` threshold output attached to release notes; 30-min `soak.js` with zero `readyz` flaps; Prometheus scrape (15s interval, 24h) with p95 panels (query first-token, vector search, upload accept) + fallback-rate + pool-saturation dashboards screenshotted.
- Manual: `curl -i` header audit (`X-Trace-Id` + `traceparent` on `200/401/403/404/429/503`); scrape-body PII grep (`@`, `eyJ`, `doc_`) zero hits; `docker logs` DSN-scrub grep (`://.*@` → zero hits); chaos-flag-in-prod boot rejection demonstrated (`chaos_enabled_in_production`).

---

## Appendix — Normative Deltas Index (for reviewers)

- **Tracing:** LangSmith stays system of record (`04 §1`, `08 §5.3` retained verbatim); this spec adds only W3C `traceparent`/`tracestate` ingress-join (§3.1 rules 1–5), `traceparent` echo on every response + `done.traceparent` (additive), `SET LOCAL application_name` + SQL-comment join for Neon, `traceparent` inject on LLM `httpx`. Invalid `traceparent` never fails a request.
- **Metrics:** new ops-plane route `GET /metrics` (Prometheus `0.0.4`, §3.2) with **dual protection** (network `404` → admin `401/403`), rate-limit exempt, `no-store`, cardinality-shed at 500 series. Frozen RED/USE catalogue §3.3 (18 families) with `service` on all series, template-only `route`, numeric `code`, pinned `model`; exemplars carry `trace_id`.
- **Load/chaos:** k6 normative (`loadtests/k6/query_sse.js|upload_burst.js|soak.js` + thresholds §3.4); chaos flags (`CHAOS_SQL_DELAY_MS`, `CHAOS_GEMINI_DOWN`, `CHAOS_POOL_MAX`) staging-only, fail-closed `off` in prod. Resilience matrix §4.3 extends `08 §4.1–§4.2` without altering timeout slices or single-fallback discipline.
- **Logs:** `08 §5.2` line extended additively (`span_id`, `traceparent`, `pool_wait_ms`); scrub + `≤500`-char rules unchanged.
- **Config:** §2.5 knobs additive (`OTEL_*`, `METRICS_*`, `CHAOS_*`, `K6_*`); prod boot gains `chaos_enabled_in_production` + `/metrics`-gate assertions.
- **Out of scope:** full OTel SDK migration / OTLP exporter, per-tenant quotas/billing, Postgres RLS, pgqueue/Redis job backend, multi-region active-active, WAF vendor config, OCR, embedding-model migration, Locust suites, automatic prompt-version rollback.

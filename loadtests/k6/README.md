# k6 Load & Soak — SLO evidence (specs/09 §3.4, normative)

k6 is the **only** SLO-evidence tool. `hey`/`curl` smoke is informational.

## Suites

| File | Target | Default shape | Thresholds (release-blockers) |
|---|---|---|---|
| `query_sse.js` | `POST /api/v1/query` streaming at `100/min/sub` ceiling | ramp `0→20` VUs / 2m → hold 20 / 8m → spike 40 / 2m → recover 20 / 3m | `http_req_failed<1%` (excl. intentional 429-probe), `sse_first_token_p95<2500ms`, `sse_done_p95<26000ms`, framing `>99%` |
| `upload_burst.js` | `POST /documents/upload` + `POST /ingest` at `20/min/sub` ceiling | ramp `0→5` VUs / 1m → hold 5 / 5m | `http_req_failed<1%`, `upload_accept_p95<500ms` (to `202`), `status_poll_p95<100ms`, zero `413/422` on fixtures |
| `soak.js` | mixed read/write 30-min | `10` VUs / 30m (80% reads, 15% query, 5% PATCH) | no `5xx` growth, `readyz` flaps `== 0`, pool `in_use p95 < 0.8×max` |
| `lib/sse.js`, `lib/auth.js` | helpers (SSE parse, JWT pool) | — | — |

Fixtures: `fixtures/` (`small.txt`, `medium.md`, 8 MB `handbook.pdf` generated in CI via `make_pdf.py`, not committed — `upload_burst.js` falls back to JSON `/ingest` when absent). All fixtures PII-free.

## Run (staging-first)

```powershell
$env:K6_BASE_URL = "https://staging-api.example"
$env:K6_VIEWER_JWT = "<csv-5+-distinct-subs>"   # query_sse needs ≥5 subs
$env:K6_EDITOR_JWT = "<csv-2-subs>"             # upload_burst needs 2 editors
$env:K6_ADMIN_JWT = "<admin-jwt>"
k6 run --summary-export=summary.json loadtests/k6/query_sse.js
k6 run loadtests/k6/upload_burst.js
k6 run loadtests/k6/soak.js
```

- Every k6 call sends a unique `traceparent` (joinable to logs/LangSmith/`/metrics` exemplars; filter by `k6_run_id`).
- Staging default. Prod runs require `K6_TARGET=prod` + change-window approval + `min-instances ≥ 1` verified.
- `K6_QUERY_VUS` / `K6_UPLOAD_VUS` override VU levels (defaults `20` / `5`, mirrors `app/core/config.py`).
- Chaos flags (`CHAOS_SQL_DELAY_MS`, `CHAOS_GEMINI_DOWN`, `CHAOS_POOL_MAX`) are staging-only; prod boot fails if set (`chaos_enabled_in_production`).

## Threshold output

Attach `--summary-export` JSON + Prometheus screenshots (p95 first-token / vector search / upload accept, fallback %, pool saturation) to release notes per `09` exit gates.

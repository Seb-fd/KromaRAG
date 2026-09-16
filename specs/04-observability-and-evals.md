# 04 — KromaRAG Observability & Evals

> LangSmith tracing, cost/token metrics, hallucination/relevance gates. Required for prod.

## 1. LangSmith Tracing

- Env: `LANGCHAIN_TRACING_V2=true`, `LANGSMITH_API_KEY`, `LANGCHAIN_PROJECT=KromaRAG`.
- Trace hierarchy per `POST /query`: `kromarag.query(trace_id)` → children `router`, `retriever.sql`, `embed`, `generator.gemini|groq`, `guardrail`.
- Metadata on every run: `{trace_id, user_sub_hash, roles, top_k, model, fallback_used, chunk_ids, scores}`. Never log raw JWT, email plaintext, or full doc content (log excerpts ≤500 chars).
- Implementation: `langsmith` + `langchain-core` `tracing_v2`; `trace_id` = UUID in `X-Trace-Id` header + SSE `done.usage`. Background batching so streaming never blocks.

## 2. Cost & Token Metrics

- Capture from provider response: `prompt_tokens`, `completion_tokens`, `model`. Emit in SSE `done.usage`.
- Cost table (config, update quarterly — pinned in `apps/api/app/core/costs.py`): live `gemini-3.6-flash $1.50/$7.50 per 1M`, `openai/gpt-oss-120b $0.15/$0.60`, `gemini-embedding-001 $0.02 per 1M tokens` (indicative); legacy rows retained for history: `gemini-2.0-flash $0.10/$0.40 per 1M`, `groq llama-3.3-70b $0.59/$0.79 per 1M`, `text-embedding-004 $0.02 per 1M tokens` (retired).
- Log per request to `kroma_usage(trace_id, model, prompt_tok, compl_tok, cost_usd, latency_ms)` (Phase 2 table) + stdout JSON for Cloud Run logs. Alert if p95 cost/query > $0.02 or fallback rate > 15%.

## 3. Evaluation Criteria & Datasets

- Dataset `kroma-evals/v1` (20-50 QAs, versioned CSV in `apps/api/evals/`): `{query, roles, expected_doc_ids, ideal_answer, forbidden_claims}`.
- Metrics (LangSmith evaluators):
  - **Faithfulness** (hallucination): claims entailed by retrieved chunks ≥ 0.85. Fail if any `forbidden_claims` present.
  - **Context recall:** expected docs in top_k ≥ 0.8.
  - **Answer relevance:** LLM-judge ≥ 4/5.
  - **RBAC leakage:** 0 chunks from unauthorized docs (critical, must be 100%).
- CI gate (Phase 3+): `uv run pytest evals/` blocks merge if faithfulness < 0.85 or leakage > 0.

## 4. Logs & Alerts

- Structured JSON logs (`trace_id`, `route`, `latency_ms`, `model`). `ERROR` on fallback, 5xx, RBAC deny bursts.
- Dashboards: LangSmith project view + Cloud Run latency + Neon DB slow-query (>500ms).

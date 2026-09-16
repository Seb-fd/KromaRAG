# 03 — KromaRAG RAG & Agent Workflow

> Ingestion, chunking, hybrid retrieval SQL, LangGraph nodes, tools. Implements `01` data flow.

## 1. Ingestion Pipeline

```
upload/URL → mime-validate → extract text → clean → semantic chunk → Gemini embed (batch 32)
→ INSERT kroma_documents + kroma_chunks + kroma_document_roles → GIN/ivfflat ready
```

- Supported Phase 1: `text/plain, text/markdown, application/pdf` (pypdf), URL fetch (readability, 50KB cap).
- Cleaning: normalize whitespace, strip boilerplate, language detect (skip non-en/es Phase 1 with warning).
- Failure: `status=failed`, error in `kroma_documents` log column; retry idempotent on `doc_id`.

## 2. Chunking Strategy

- **Default:** recursive semantic split, target **600 tokens**, overlap **90 tokens** (~15%), `tiktoken cl100k` counting.
- Boundaries: `["\n## ","\n### ","\n\n",". ","; "]`; never split code fences; `ord` preserves order.
- Metadata per chunk: `{ord, token_count, heading_path}`. Max 2000 chunks/doc Phase 1.
- Module: `apps/api/app/rag/chunker.py` → `chunk_text(text) -> list[ChunkDraft]`.

## 3. Embeddings

- Model: `text-embedding-004`, dim `768` (`PGVECTOR_DIM`). Batch embed, retry 3× exponential.
- Query embedding cached in-memory (LRU 500, TTL 10 min) keyed by normalized query.

## 4. Hybrid Retrieval SQL (RBAC-enforced)

Weights: `0.7 * cosine + 0.3 * ts_rank`. All in one query; RBAC in `WHERE`, never post-filter.

```sql
-- $1: query_embedding VECTOR(768), $2: plainto_tsquery string, $3: user_sub, $4: user_roles TEXT[], $5: top_k
SELECT c.id AS chunk_id, c.doc_id, d.title, c.content, c.ord,
  (0.7 * (1 - (c.embedding <=> $1)) + 0.3 * coalesce(ts_rank(c.content_tsv, $2), 0)) AS score
FROM kroma_chunks c JOIN kroma_documents d ON d.id = c.doc_id
WHERE (d.owner_sub = $3 OR EXISTS (
  SELECT 1 FROM kroma_document_roles r WHERE r.doc_id = d.id AND r.role = ANY($4)
))
AND ($2 IS NULL OR c.content_tsv @@ $2 OR TRUE)
ORDER BY c.embedding <=> $1
LIMIT ($5 * 3)
-- Python rerank: final top_k by blended score; threshold score >= 0.25 else "no evidence".
```

- `ivfflat` probes=10. `EXPLAIN` budget <300ms @100K chunks (Neon).
- Reranker Phase 1: blended-score sort; Phase 3: cross-encoder (`bge-reranker`) optional.

## 5. LangGraph Agent

Graph: `router → retriever → reranker → generator → guardrail`. State: `AgentState{query, principal, chunks, draft, citations, usage, trace_id}`.

```python
# apps/api/app/agent/graph.py (LangGraph)
router: if greeting/smalltalk → generator(direct) else retriever
retriever: embed query → hybrid SQL (§4) → chunks
reranker: sort/filter, attach citations
generator: Gemini 3.6-flash (system: cite [doc:title:ord]); on timeout/429/404-NOT_FOUND → Groq llama-3.3-70b
guardrail: if no chunks → "I don't have evidence in your accessible docs."; PII redact; return SSE
```

- Tool-calling: `hybrid_search(query, top_k)` (the SQL above), `get_doc_metadata(doc_id)` (RBAC-checked). LLM may call max 3 tool steps.
- Prompts versioned in `agent/prompts/`; system prompt must instruct: answer only from `chunks`, cite every 2-3 sentences, refuse out-of-scope.

## 6. Config Knobs (env)

`TOP_K_DEFAULT=6`, `HYBRID_VECTOR_W=0.7`, `HYBRID_TEXT_W=0.3`, `CHUNK_TOKENS=600`, `CHUNK_OVERLAP=90`, `SCORE_MIN=0.25`, `LLM_TIMEOUT_S=25`.

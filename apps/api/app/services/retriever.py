"""Hybrid search per specs/03 §4 + specs/07 §4.4: 0.7*cosine + 0.3*ts_rank, RBAC in WHERE (never post-filter).

Admin bypass mirrors the documents endpoints (VISIBILITY_FRAG): owner, admin
role, or exact role grant — the panel and the retriever agree on "accessible".

Only status='indexed' rows are retrieval-eligible (pending/indexing/failed/archived excluded).

$1 query_embedding VECTOR(768) | $2 plainto_tsquery string | $3 user_sub
$4 user_roles TEXT[] | $5 fetch_limit (= top_k*3)
Python rerank → final top_k, threshold SCORE_MIN (0.25) else "no evidence" ([]).
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

HYBRID_SQL = """SELECT c.id AS chunk_id, c.doc_id, d.title, c.content, c.ord,
  (0.7 * (1 - (c.embedding <=> $1::vector)) + 0.3 * coalesce(ts_rank(c.content_tsv, plainto_tsquery('english', $2)), 0)) AS score
FROM kroma_chunks c JOIN kroma_documents d ON d.id = c.doc_id
WHERE (d.owner_sub = $3 OR 'admin' = ANY($4) OR EXISTS (
  SELECT 1 FROM kroma_document_roles r WHERE r.doc_id = d.id AND r.role = ANY($4)
))
AND d.status = 'indexed'
ORDER BY c.embedding <=> $1::vector
LIMIT $5"""


@dataclass
class RetrievedChunk:
    chunk_id: UUID | str
    doc_id: UUID | str
    title: str
    content: str
    ord: int
    score: float


def build_hybrid_params(query_embedding: list[float], query_text: str, user_sub: str, roles: list[str], top_k: int):
    """Return (sql, args) for asyncpg fetch. Single source of SQL truth (unit-tested)."""
    if len(query_embedding) != 768:
        raise ValueError(f"query_embedding must be dim 768, got {len(query_embedding)}")
    if not user_sub:
        raise ValueError("user_sub is required (RBAC)")
    fetch = max(1, top_k) * 3
    vec_literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    return HYBRID_SQL, [vec_literal, query_text, user_sub, list(roles), fetch]


def rerank_blended(rows: list[dict], top_k: int, score_min: float = 0.25) -> list[RetrievedChunk]:
    """Blended-score sort (Phase 1 reranker). Rows carry precomputed `score` from SQL."""
    ranked = sorted(rows, key=lambda r: r["score"], reverse=True)
    out: list[RetrievedChunk] = []
    for r in ranked:
        if r["score"] < score_min:
            continue
        out.append(RetrievedChunk(
            chunk_id=r["chunk_id"], doc_id=r["doc_id"], title=r.get("title", ""),
            content=r.get("content", ""), ord=int(r.get("ord", 0)), score=float(r["score"]),
        ))
        if len(out) >= top_k:
            break
    return out


async def hybrid_search(query: str, principal, top_k: int = 6, db=None, embed_fn=None,
                        score_min: float = 0.25, trace_id: str | None = None) -> list[RetrievedChunk]:
    """Tool entrypoint for LangGraph retriever node (specs/03 §5): embed → hybrid SQL → rerank."""
    import asyncio as _asyncio
    import time as _time

    from app.services.embeddings import embed_query as _default_eq

    if db is None:
        raise ValueError("db pool/connection is required")
    q = (query or "").strip()
    if not q:
        return []
    top_k = max(1, min(int(top_k or 6), 20))
    # Chaos hook (spec 09 §4.3, staging-only): artificial SQL delay.
    try:
        from app.core.config import settings as _settings

        delay_ms = int(getattr(_settings, "chaos_sql_delay_ms", 0) or 0)
        if delay_ms > 0 and not _settings.is_prod:
            await _asyncio.sleep(min(delay_ms, 30000) / 1000.0)
    except Exception:
        pass
    vec = await (embed_fn(q) if embed_fn is not None else _default_eq(q))
    sql, args = build_hybrid_params(vec, q, principal.sub, list(getattr(principal, "roles", [])), top_k)
    # Trace join (spec 09 §3.1 rule 8): application_name + SQL comment. Best-effort.
    if trace_id:
        try:
            from app.core.tracecontext import db_trace_comment

            sql = sql + f" {db_trace_comment(trace_id)}"
        except Exception:
            pass
    t0 = _time.monotonic()
    outcome = "ok"
    try:
        try:
            await db.execute("SET LOCAL ivfflat.probes = 10")
        except Exception:
            pass  # fakes / non-ivfflat hosts
        if trace_id:
            try:
                from app.core.tracecontext import db_application_name as _appname

                await db.execute(f"SET LOCAL application_name = '{_appname(trace_id)}'")
            except Exception:
                pass
        try:
            from app.core.config import settings as _s2

            await db.execute(f"SET LOCAL statement_timeout = '{int(_s2.retriever_timeout_ms)}ms'")
        except Exception:
            pass
        rows = await db.fetch(sql, *args)
    except Exception as e:
        low = str(e).lower()
        outcome = "timeout" if ("timeout" in low or "canceling statement" in low) else "error"
        try:
            from app.services import metrics as _m

            _m.observe_vector_search(_time.monotonic() - t0, outcome, trace_id=trace_id)
        except Exception:
            pass
        raise
    dicts = [dict(r) for r in rows]
    out = rerank_blended(dicts, top_k, score_min)
    try:
        from app.services import metrics as _m

        _m.observe_vector_search(_time.monotonic() - t0, outcome, trace_id=trace_id)
    except Exception:
        pass
    return out


async def get_doc_metadata(doc_id: str, principal, db=None) -> dict | None:
    """RBAC-checked metadata for tool-calling (specs/03 §5, specs/07 §3.1)."""
    if db is None:
        raise ValueError("db pool/connection is required")
    row = await db.fetchrow(
        "SELECT d.id, d.title, d.mime, d.status, d.owner_sub, d.version FROM kroma_documents d "
        "WHERE d.id=$1 AND (d.owner_sub=$2 OR 'admin' = ANY($3) OR EXISTS ("
        "SELECT 1 FROM kroma_document_roles r WHERE r.doc_id=d.id AND r.role = ANY($3)))",
        doc_id, principal.sub, list(getattr(principal, "roles", [])),
    )
    return dict(row) if row else None

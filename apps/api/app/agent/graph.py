"""LangGraph agent per specs/03 §5: router → retriever → reranker → generator → guardrail (+ evaluator).

Node logic is framework-free async functions so unit tests run without langgraph
installed; build_graph() compiles a real StateGraph when the package is present,
else a FallbackGraph with the same ainvoke() contract. Max 3 tool steps per run.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from app.agent import evaluator as evaluator_mod
from app.agent import guardrails as guardrails_mod
from app.agent.llm import LLMError, generate_with_fallback
from app.agent.prompts import DIRECT_PROMPT_V1, NO_EVIDENCE, RAG_USER_TEMPLATE_V2, SYSTEM_PROMPT_V2
from app.services import tracing as tracing_mod
from app.services.retriever import get_doc_metadata, hybrid_search

log = logging.getLogger(__name__)

GREETING_RE = re.compile(
    r"^(hi|hello|hey|hola|thanks|thank you|bye|good (morning|afternoon|evening)|who are you|what can you do)\b[.!?]?\s*$",
    re.IGNORECASE,
)
MAX_TOOL_STEPS = 3


class AgentState(TypedDict, total=False):
    query: str
    principal_sub: str
    principal_roles: list[str]
    top_k: int
    trace_id: str
    conv_id: str
    route: str  # direct | retrieval
    chunks: list
    citations: list
    draft: str
    answer: str
    usage: dict
    model: str
    fallback_used: bool
    fallback_error: str | None
    eval: dict
    flags: list
    error: dict | None
    tool_steps: int
    history: list  # [{role: user|assistant, content: str}] verbatim oldest→newest (specs/06 §2.5)
    history_tokens: int
    _sse_token_frames: int  # internal SSE metric only, never persisted


@dataclass
class AgentDeps:
    """Injectable collaborators (DB, LLM fakes). Production: db=pool, fns=None (real APIs)."""

    db: Any = None
    embed_fn: Any = None
    gemini_fn: Any = None
    groq_fn: Any = None
    http_client: Any = None
    score_min: float = 0.25
    max_tool_steps: int = MAX_TOOL_STEPS
    forbidden_claims: tuple = ()


def route_intent(query: str) -> str:
    """Greeting/smalltalk → direct (no retrieval); everything else → retrieval."""
    q = (query or "").strip()
    if not q:
        return "retrieval"
    if len(q.split()) <= 12 and GREETING_RE.match(q):
        return "direct"
    return "retrieval"


def _principal(state: AgentState):
    from app.schemas import Principal

    return Principal(sub=state["principal_sub"], roles=list(state.get("principal_roles", [])))


def _meta(state: AgentState, **extra) -> dict:
    return {
        "trace_id": state.get("trace_id", ""), "user_sub": state.get("principal_sub", ""),
        "roles": state.get("principal_roles", []), "top_k": state.get("top_k", 6),
        "query_excerpt": (state.get("query") or "")[:500], **extra,
    }


async def node_router(state: AgentState, deps: AgentDeps) -> AgentState:
    with tracing_mod.span("router", _meta(state)):
        state["route"] = route_intent(state.get("query", ""))
        state["tool_steps"] = 0
    return state


async def node_retriever(state: AgentState, deps: AgentDeps) -> AgentState:
    """Tool step 1: hybrid_search; steps 2-3: get_doc_metadata enrichment (max 3 total).

    Retrieval isolation (specs/06 §4.4): embeds current query ONLY; never reads
    state["history"] for embedding or filtering.
    """
    from app.core.config import settings

    p = _principal(state)
    top_k = int(state.get("top_k") or settings.top_k_default)
    if deps.db is None:
        state["chunks"] = []
        state["flags"] = [*state.get("flags", []), "no-db"]
        state["tool_steps"] = 0
        return state
    with tracing_mod.span("retriever.sql", _meta(state, model="hybrid")):
        try:
            chunks = await hybrid_search(
                state["query"], p, top_k=top_k, db=deps.db,
                embed_fn=deps.embed_fn, score_min=deps.score_min,
                trace_id=state.get("trace_id") or None,
            )
        except TypeError:
            # Backward compat: older fakes without the trace_id kwarg.
            chunks = await hybrid_search(
                state["query"], p, top_k=top_k, db=deps.db,
                embed_fn=deps.embed_fn, score_min=deps.score_min,
            )
    steps = 1
    # Enrich top-2 docs with metadata (bounded tool steps)
    seen: set[str] = set()
    for c in list(chunks)[:2]:
        if steps >= deps.max_tool_steps:
            break
        doc_id = str(c["doc_id"] if isinstance(c, dict) else getattr(c, "doc_id", ""))
        if doc_id in seen:
            continue
        seen.add(doc_id)
        with tracing_mod.span("retriever.metadata", _meta(state, doc_id=doc_id)):
            await get_doc_metadata(doc_id, p, db=deps.db)
        steps += 1
    state["chunks"] = chunks
    state["tool_steps"] = steps
    return state


async def node_reranker(state: AgentState, deps: AgentDeps) -> AgentState:
    """Blended-score order (retriever already reranks) + citation attachment."""
    chunks = sorted(state.get("chunks", []), key=lambda c: float(
        c["score"] if isinstance(c, dict) else getattr(c, "score", 0)), reverse=True)
    state["chunks"] = chunks
    state["citations"] = [
        {"chunk_id": str(c["chunk_id"] if isinstance(c, dict) else getattr(c, "chunk_id", "")),
         "doc_id": str(c["doc_id"] if isinstance(c, dict) else getattr(c, "doc_id", "")),
         "title": c["title"] if isinstance(c, dict) else getattr(c, "title", ""),
         "ord": int(c["ord"] if isinstance(c, dict) else getattr(c, "ord", 0)),
         "score": round(float(c["score"] if isinstance(c, dict) else getattr(c, "score", 0)), 4)}
        for c in chunks
    ]
    return state


def sanitize_title(title: str) -> str:
    """Sanitize a doc title so it round-trips through [doc:Title:ord] parsing.

    Strips characters that break the citation regex ([ ] : newlines) and
    truncates to keep prompts compact. Mirrors frontend lib/citations.ts.
    """
    t = re.sub(r"[\[\]:\n\r]+", "-", str(title or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:120] or "untitled"


def _rag_prompt(query: str, chunks: list, history: list | None = None) -> str:
    items: list[tuple[str, int, str]] = []
    for c in chunks:
        raw_title = c.title if hasattr(c, "title") else c.get("title", "")
        raw_ord = c.ord if hasattr(c, "ord") else c.get("ord", 0)
        try:
            ord_val = int(str(raw_ord).split(".")[0])
        except (ValueError, TypeError):
            ord_val = 0
        title = sanitize_title(raw_title)
        content = c.content if hasattr(c, "content") else c.get("content", "")
        items.append((title, ord_val, str(content or "")[:1200]))
    ctx = "\n".join(f"[doc:{t}:{o}] {content}" for t, o, content in items)
    allowlist = "\n".join(f"- [doc:{t}:{o}]" for t, o, _ in items) or "- (no citations: no accessible chunks)"
    base = RAG_USER_TEMPLATE_V2.format(query=query, allowlist=allowlist, context=ctx)
    # History is prompt-only context (specs/06 §4.4); never affects retrieval.
    hist = history or []
    if not hist:
        return base
    try:
        from app.services.conversations import format_history_block
    except Exception:
        return base
    block = format_history_block(hist)
    if not block:
        return base
    return f"{base}\n\n{block}\n\nCurrent user query: {query}"


async def node_generator(state: AgentState, deps: AgentDeps) -> AgentState:
    from app.core.config import settings

    if state.get("route") == "direct":
        prompt, system, model_tag = state["query"], DIRECT_PROMPT_V1, "generator.direct"
    else:
        # Retrieval isolation: history flows to the generator only (specs/06 §4.4).
        prompt, system, model_tag = _rag_prompt(
            state["query"], state.get("chunks", []), state.get("history") or []
        ), SYSTEM_PROMPT_V2, None
    try:
        # Outbound trace linkage (spec 09 §3.1 rule 9): derive a traceparent
        # from the joined trace_id for provider calls (best-effort, no I/O).
        traceparent = None
        try:
            import secrets as _secrets

            from app.core.tracecontext import build_traceparent

            tid = state.get("trace_id") or ""
            if tid:
                traceparent = build_traceparent(tid, _secrets.token_hex(8), True)
        except Exception:
            traceparent = None
        with tracing_mod.span(model_tag or "generator.gemini", _meta(state)):
            res = await generate_with_fallback(
                prompt, system, timeout_s=settings.llm_timeout_s,
                gemini_fn=deps.gemini_fn, groq_fn=deps.groq_fn, client=deps.http_client,
                traceparent=traceparent,
            )
    except LLMError as e:
        # Raw provider error retained backend-side (state + logs); SSE layer sanitizes.
        log.warning("node_generator LLM failure trace_id=%s code=%s raw=%s",
                    state.get("trace_id", ""), e.code, str(e)[:500])
        state["error"] = {"code": e.code, "message": str(e)}
        state["draft"] = ""
        state["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "model": ""}
        state["model"] = ""
        state["fallback_used"] = False
        return state
    state["draft"] = res.text
    state["usage"] = {"prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens, "model": res.model}
    state["model"] = res.model
    state["fallback_used"] = res.fallback_used
    state["fallback_error"] = res.fallback_error
    return state


async def node_guardrail(state: AgentState, deps: AgentDeps) -> AgentState:
    with tracing_mod.span("guardrail", _meta(state)):
        if state.get("error"):
            state["answer"] = NO_EVIDENCE
            state["flags"] = [*state.get("flags", []), "llm-error"]
            return state
        draft = state.get("draft", "")
        if state.get("route") == "direct":
            safe: str
            flags: list[str] = []
            safe = guardrails_mod.redact_pii(draft)
            state["answer"] = safe
            state["flags"] = [*state.get("flags", []), *flags]
            return state
        safe, flags = guardrails_mod.apply_guardrails(draft, state.get("chunks", []))
        state["answer"] = safe
        state["flags"] = [*state.get("flags", []), *flags]
    return state


async def node_evaluator(state: AgentState, deps: AgentDeps) -> AgentState:
    chunks = state.get("chunks", [])
    allowed = {str(c["doc_id"] if isinstance(c, dict) else getattr(c, "doc_id", "")) for c in chunks} or None
    state["eval"] = evaluator_mod.evaluate(
        state.get("answer", ""), chunks, state.get("query", ""),
        allowed_doc_ids=allowed, forbidden_claims=deps.forbidden_claims,
    )
    return state


async def arun(state: AgentState, deps: AgentDeps | None = None) -> AgentState:
    """Full pipeline: router → [retriever → reranker] → generator → guardrail → evaluator."""
    deps = deps or AgentDeps()
    t0 = time.monotonic()
    state.setdefault("history", [])
    state.setdefault("history_tokens", 0)
    with tracing_mod.span("kromarag.query", _meta(state)):
        state = await node_router(state, deps)
        if state["route"] == "retrieval":
            state = await node_retriever(state, deps)
            state = await node_reranker(state, deps)
        else:
            state.setdefault("chunks", [])
            state.setdefault("citations", [])
        state = await node_generator(state, deps)
        state = await node_guardrail(state, deps)
        state = await node_evaluator(state, deps)
        usage = state.get("usage") or {}
        tracing_mod.log_usage(
            state.get("trace_id", ""), state.get("model", ""), int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)), int((time.monotonic() - t0) * 1000),
            bool(state.get("fallback_used")),
        )
    return state


async def stream_answer(answer: str, chars: int = 48):
    """Simulated token streaming: slice final answer into deltas (provider streaming = future opt)."""
    for i in range(0, len(answer), chars):
        yield answer[i : i + chars]


class FallbackGraph:
    """arun-backed graph used when langgraph is not installed. Same ainvoke() contract."""

    def __init__(self, deps: AgentDeps | None = None):
        self.deps = deps or AgentDeps()

    async def ainvoke(self, state: AgentState) -> AgentState:
        return await arun(cast(AgentState, dict(state)), self.deps)


def build_graph(deps: AgentDeps | None = None):
    """Compile a real LangGraph StateGraph when available; else FallbackGraph.

    Nodes: router → retriever → reranker → generator → guardrail → evaluator.
    """
    deps = deps or AgentDeps()
    try:
        from langgraph.graph import END, StateGraph
    except Exception:
        return FallbackGraph(deps)

    async def _router(s: AgentState) -> AgentState:
        return await node_router(s, deps)

    async def _retriever(s: AgentState) -> AgentState:
        return await node_retriever(s, deps)

    async def _reranker(s: AgentState) -> AgentState:
        return await node_reranker(s, deps)

    async def _generator(s: AgentState) -> AgentState:
        return await node_generator(s, deps)

    async def _guardrail(s: AgentState) -> AgentState:
        return await node_guardrail(s, deps)

    async def _evaluator(s: AgentState) -> AgentState:
        return await node_evaluator(s, deps)

    g: Any = StateGraph(AgentState)  # Any: langgraph add_node overloads vary by version; wiring is runtime-tested
    g.add_node("router", _router)
    g.add_node("retriever", _retriever)
    g.add_node("reranker", _reranker)
    g.add_node("generator", _generator)
    g.add_node("guardrail", _guardrail)
    g.add_node("evaluator", _evaluator)
    g.set_entry_point("router")
    g.add_conditional_edges("router", lambda s: "generator" if s.get("route") == "direct" else "retriever")
    g.add_edge("retriever", "reranker")
    g.add_edge("reranker", "generator")
    g.add_edge("generator", "guardrail")
    g.add_edge("guardrail", "evaluator")
    g.add_edge("evaluator", END)
    return g.compile()

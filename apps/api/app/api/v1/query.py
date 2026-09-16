"""Query SSE endpoint — LangGraph agent streaming (specs/02 §3, specs/03 §5, specs/06 §3-§4).

Contract: `source` exactly once first → `token` 0..N → `done` exactly once.
`error` on LLM failure (followed by `done`). Heartbeat `: ping` every 15s.
Non-stream (`stream=false`) returns the same payload as JSON.

Specs/06 additions: thread ownership validation (leak-safe 404), implicit
thread creation, sliding-window history injection (retrieval-isolated), and
atomic (user, assistant) persistence after terminal `done`.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.agent import graph as agent_graph
from app.agent.graph import AgentDeps
from app.agent.prompts import FRIENDLY_LLM_DOWN
from app.core.security import require_role
from app.schemas import Principal, QueryRequest

router = APIRouter(tags=["query"])

log = logging.getLogger(__name__)

# Sanitized client-facing notice when primary failed but fallback succeeded.
# Raw primary error stays in backend logs only.
FALLBACK_NOTICE = "Primary provider unavailable, used fallback."

THREAD_NOT_FOUND = {"code": "NOT_FOUND", "message": "conversation not found"}


def resolve_deps() -> AgentDeps:
    """Production collaborators. Monkeypatched in tests (FakeDB / fake LLM fns)."""
    from app.core.config import settings
    from app.services.db import get_pool

    return AgentDeps(db=get_pool(), score_min=settings.score_min)


def _db_conn(deps: AgentDeps):
    """Return a directly-usable connection, or None when no usable DB.

    Production deps.db is an asyncpg Pool (has .acquire); tests may inject a
    fake connection (has .fetchrow) or a bare object() (offline → None).
    Callers needing a transaction should use `_run_with_conn`.
    """
    db = getattr(deps, "db", None)
    if db is None:
        return None
    if hasattr(db, "fetchrow") and hasattr(db, "execute"):
        return db
    return None


async def _run_with_conn(deps: AgentDeps, fn):
    """Run fn(conn) with a connection from deps.db. Returns None when offline.

    Supports Pool (.acquire) and bare connections (.fetchrow). Never raises —
    history/persistence must never break the query path.
    """
    db = getattr(deps, "db", None)
    if db is None:
        return None
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                return await fn(conn)
        if hasattr(db, "fetchrow") and hasattr(db, "execute"):
            return await fn(db)
    except Exception as e:  # noqa: BLE001 — persistence/history is best-effort
        log.warning("history db op skipped: %s", type(e).__name__)
    return None


async def resolve_thread(deps: AgentDeps, principal: Principal, conv_id: str | None) -> str:
    """Validate or implicitly create a thread (specs/06 §4.1).

    Returns the stable conv_id. Raises 404 when an explicit conv_id is
    foreign or missing. Offline/fake DBs without a usable connection fall
    through with an ephemeral id (existing offline tests).
    """
    if not conv_id:
        new_id = str(uuid.uuid4())
        from app.services import conversations as conv_svc

        async def _ensure(conn):
            await conv_svc.ensure_thread(conn, new_id, principal.sub)

        await _run_with_conn(deps, _ensure)
        return new_id
    from app.services import conversations as conv_svc

    found: list = []

    async def _check(conn):
        row = await conv_svc.check_owner(conn, conv_id, principal.sub)
        found.append(bool(row))

    # No usable DB → allow through (offline unit path, e.g. db=object()).
    db = getattr(deps, "db", None)
    usable = db is not None and (hasattr(db, "acquire") or hasattr(db, "fetchrow"))
    if not usable:
        return conv_id
    await _run_with_conn(deps, _check)
    if not found or not found[0]:
        raise HTTPException(404, THREAD_NOT_FOUND)
    return conv_id


async def load_truncated_history(deps: AgentDeps, conv_id: str) -> tuple[list, int]:
    """Load verbatim last-N history + token estimate (specs/06 §4.4)."""
    from app.core.config import settings
    from app.services import conversations as conv_svc
    from app.services import tracing as tracing_mod

    holder: dict = {"rows": []}

    async def _load(conn):
        holder["rows"] = await conv_svc.load_history_rows(
            conn, conv_id, settings.history_max_turns * 2
        )

    await _run_with_conn(deps, _load)
    rows = holder.get("rows") or []
    history, tokens = conv_svc.truncate_history(
        rows,
        max_turns=settings.history_max_turns,
        max_chars=settings.history_max_chars,
        max_tokens=settings.history_max_tokens,
    )
    try:
        with tracing_mod.span("history.load", {"trace_id": "", "msg_count": len(history),
                                               "truncated": len(rows) > len(history)}):
            pass
    except Exception:
        pass
    return history, tokens


async def persist_turn(deps: AgentDeps, principal: Principal, conv_id: str,
                       user_content: str, state: dict) -> None:
    """Best-effort atomic pair write (specs/06 §4.2). Never raises."""
    from app.services import conversations as conv_svc

    usage = state.get("usage") or {}
    route = state.get("route") or "retrieval"
    answer = state.get("answer", "")

    async def _write(conn):
        await conv_svc.persist_pair(
            conn, conv_id, principal.sub, user_content, answer,
            str(state.get("trace_id", "")), usage, route,
        )

    await _run_with_conn(deps, _write)


def _initial_state(body: QueryRequest, principal: Principal, trace_id: str, conv_id: str,
                   history: list | None = None, history_tokens: int = 0) -> dict:
    from app.core.config import settings

    return {
        "query": body.query.strip(),
        "principal_sub": principal.sub,
        "principal_roles": list(principal.roles),
        "top_k": max(1, min(int(body.top_k or settings.top_k_default), 20)),
        "trace_id": trace_id,
        "conv_id": conv_id,
        "history": list(history or []),
        "history_tokens": int(history_tokens or 0),
    }


def _source_data(state: dict) -> str:
    return json.dumps({"chunks": [
        {"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "title": c["title"],
         "ord": c["ord"], "score": c["score"]}
        for c in state.get("citations", [])
    ]})


def _done_data(state: dict, conv_id: str, trace_id: str, traceparent: str | None = None) -> str:
    usage = state.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "model": ""}
    payload = {
        "conv_id": conv_id, "trace_id": trace_id,
        "usage": {"prompt_tokens": int(usage.get("prompt_tokens", 0)),
                  "completion_tokens": int(usage.get("completion_tokens", 0)),
                  "model": usage.get("model", "") or state.get("model", "")},
        "model": state.get("model", ""), "fallback_used": bool(state.get("fallback_used", False)),
        "eval": state.get("eval", {}),
        "history_tokens": int(state.get("history_tokens", 0)),
    }
    if traceparent:
        payload["traceparent"] = traceparent
    else:
        # Additive trace linkage for direct/unit invocations without HTTP context.
        try:
            from app.core.tracecontext import build_traceparent
            import secrets as _secrets

            payload["traceparent"] = build_traceparent(trace_id, _secrets.token_hex(8), True)
        except Exception:
            pass
    return json.dumps(payload)


def _parse_fallback_code(fallback_error: str | None) -> str:
    """Extract LLM_* code from `CODE: detail` strings (spec 09 fallback series)."""
    try:
        raw = (fallback_error or "").strip()
        if not raw:
            return "LLM_ERROR"
        head = raw.split(":", 1)[0].strip()
        if head in ("LLM_TIMEOUT", "LLM_RATE_LIMIT", "LLM_ERROR", "LLM_FALLBACK"):
            return head if head != "LLM_FALLBACK" else "LLM_ERROR"
        up = raw.upper()
        if "TIMEOUT" in up:
            return "LLM_TIMEOUT"
        if "429" in up or "RATE_LIMIT" in up:
            return "LLM_RATE_LIMIT"
        return "LLM_ERROR"
    except Exception:
        return "LLM_ERROR"


def _record_query_metrics(state: dict, trace_id: str, sse_outcome: str = "done",
                          sse_duration_s: float = 0.0, route: str = "POST /api/v1/query") -> None:
    """Best-effort RED/USE increments for a completed query (spec 09 §3.3)."""
    try:
        from app.core.config import settings as _settings
        from app.services import metrics as _m

        usage = state.get("usage") or {}
        model = str(state.get("model", "") or usage.get("model", "") or "")
        if state.get("fallback_used"):
            code = _parse_fallback_code(state.get("fallback_error"))
            _m.inc_fallback(_settings.gemini_model, model or _settings.groq_model, code)
        try:
            n_tok = state.get("_sse_token_frames")
            if n_tok and model:
                _m.inc_sse_tokens(model, int(n_tok))
        except Exception:
            pass
        _m.observe_sse(route, sse_outcome, max(0.0, float(sse_duration_s or 0.0)))
    except Exception:
        pass


async def run_query_events(body: QueryRequest, principal: Principal, deps: AgentDeps,
                           trace_id: str, conv_id: str, traceparent: str | None = None):
    """Async generator of {"event","data"} frames. Unit-tested without HTTP (see test_query.py)."""
    from app.services import tracing as tracing_mod

    t0 = time.monotonic()
    sse_outcome = "done"
    token_frames = 0
    history, history_tokens = await load_truncated_history(deps, conv_id)
    state = _initial_state(body, principal, trace_id, conv_id, history, history_tokens)
    source_sent = False
    completed = False
    try:
        with tracing_mod.span("kromarag.query", {"trace_id": trace_id, "user_sub": principal.sub,
                                                 "roles": list(principal.roles), "top_k": state["top_k"],
                                                 "query_excerpt": state["query"][:500]}):
            state = await agent_graph.node_router(state, deps)
            if state.get("route") == "retrieval":
                state = await agent_graph.node_retriever(state, deps)
                state = await agent_graph.node_reranker(state, deps)
            else:
                state.setdefault("chunks", [])
                state.setdefault("citations", [])
            # Preserve history across node transitions (nodes return the same dict,
            # but fakes in tests may construct new state).
            state.setdefault("history", history)
            state.setdefault("history_tokens", history_tokens)
            yield {"event": "source", "data": _source_data(state)}
            source_sent = True
            state = await agent_graph.node_generator(state, deps)
            state.setdefault("history", history)
            state.setdefault("history_tokens", history_tokens)
            state = await agent_graph.node_guardrail(state, deps)
            state.setdefault("history", history)
            state.setdefault("history_tokens", history_tokens)
            state = await agent_graph.node_evaluator(state, deps)
            state.setdefault("history", history)
            state.setdefault("history_tokens", history_tokens)
            if state.get("error") and state["error"].get("code"):
                # Total failure: both primary and fallback failed.
                # Log raw backend-side, expose only friendly text client-side,
                # then stream the friendly message as tokens so dumb SSE readers show it.
                raw_code = str(state["error"].get("code", "LLM_ERROR"))
                raw_msg = str(state["error"].get("message", ""))
                log.warning("llm total failure trace_id=%s code=%s raw=%.500s",
                            trace_id, raw_code, raw_msg)
                yield {"event": "error", "data": json.dumps(
                    {"code": raw_code, "message": FRIENDLY_LLM_DOWN})}
                state["answer"] = FRIENDLY_LLM_DOWN
                async for delta in agent_graph.stream_answer(state["answer"]):
                    token_frames += 1
                    yield {"event": "token", "data": json.dumps({"delta": delta})}
                if state.get("fallback_error"):
                    log.info("llm fallback detail trace_id=%s raw=%.500s",
                             trace_id, str(state.get("fallback_error")))
            else:
                if state.get("fallback_error"):
                    # Fallback succeeded: sanitize raw primary error, keep answer tokens intact.
                    log.info("llm fallback used trace_id=%s raw=%.500s",
                             trace_id, str(state.get("fallback_error")))
                    yield {"event": "error", "data": json.dumps(
                        {"code": "LLM_FALLBACK", "message": FALLBACK_NOTICE})}
                async for delta in agent_graph.stream_answer(state.get("answer", "")):
                    token_frames += 1
                    yield {"event": "token", "data": json.dumps({"delta": delta})}
            completed = True
    except Exception as e:  # noqa: BLE001 — SSE contract: errors as events, then done
        # Raw exception stays backend-side only.
        sse_outcome = "error"
        log.exception("query stream failed trace_id=%s: %s", trace_id, e)
        if not source_sent:
            yield {"event": "source", "data": json.dumps({"chunks": []})}
        yield {"event": "error", "data": json.dumps({"code": "LLM_ERROR", "message": FRIENDLY_LLM_DOWN})}
        state["answer"] = FRIENDLY_LLM_DOWN
        async for delta in agent_graph.stream_answer(state["answer"]):
            token_frames += 1
            yield {"event": "token", "data": json.dumps({"delta": delta})}
        state.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0, "model": ""})
        state.setdefault("eval", {})
        state.setdefault("history", history)
        state.setdefault("history_tokens", history_tokens)
        completed = source_sent  # exception-before-source persists nothing (§4.2)
    finally:
        # Atomic pair persistence (best-effort; never breaks the stream).
        if completed:
            try:
                await persist_turn(deps, principal, conv_id, body.query.strip(), state)
            except Exception:
                pass
        usage = state.get("usage") or {}
        from app.services import tracing as _t

        try:
            _t.log_usage(trace_id, state.get("model", ""), int(usage.get("prompt_tokens", 0)),
                         int(usage.get("completion_tokens", 0)), int((time.monotonic() - t0) * 1000),
                         bool(state.get("fallback_used")),
                         int(state.get("history_tokens", 0)))
        except TypeError:
            _t.log_usage(trace_id, state.get("model", ""), int(usage.get("prompt_tokens", 0)),
                         int(usage.get("completion_tokens", 0)), int((time.monotonic() - t0) * 1000),
                         bool(state.get("fallback_used")))
        try:
            state["_sse_token_frames"] = token_frames
            _record_query_metrics(state, trace_id, sse_outcome,
                                  time.monotonic() - t0, "POST /api/v1/query")
        except Exception:
            pass
        yield {"event": "done", "data": _done_data(state, conv_id, trace_id, traceparent)}


@router.post("/query")
async def query(body: QueryRequest, request: Request, p: Principal = Depends(require_role("viewer"))):
    if not body.query.strip():
        raise HTTPException(422, {"code": "VALIDATION", "message": "query must not be blank"})
    # Canonical trace context from middleware (W3C join wins per specs/09 §3.1 rule 5).
    trace_id = getattr(request.state, "trace_id", None) or str(uuid.uuid4())
    traceparent = getattr(request.state, "traceparent", None)
    if not traceparent:
        try:
            from app.core.tracecontext import build_traceparent
            import secrets as _secrets

            traceparent = build_traceparent(trace_id, _secrets.token_hex(8), True)
        except Exception:
            traceparent = None
    deps = resolve_deps()
    conv_id = await resolve_thread(deps, p, str(body.conv_id) if body.conv_id else None)

    if not body.stream:
        history, history_tokens = await load_truncated_history(deps, conv_id)
        state = await agent_graph.arun(_initial_state(body, p, trace_id, conv_id, history, history_tokens), deps)
        state.setdefault("history", history)
        state.setdefault("history_tokens", history_tokens)
        usage = state.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "model": ""}
        if state.get("error") and state["error"].get("code"):
            # Sanitize raw LLM error for JSON clients; raw stays in logs via node_generator.
            log.warning("llm total failure (non-stream) trace_id=%s code=%s raw=%.500s",
                        trace_id, state["error"].get("code"), str(state["error"].get("message", "")))
            state["answer"] = FRIENDLY_LLM_DOWN
        try:
            await persist_turn(deps, p, conv_id, body.query.strip(), state)
        except Exception:
            pass
        try:
            _record_query_metrics(state, trace_id, "done", 0.0, "POST /api/v1/query")
        except Exception:
            pass
        payload = {"trace_id": trace_id, "conv_id": conv_id, "answer": state.get("answer", ""),
                   "chunks": state.get("citations", []), "usage": usage, "model": state.get("model", ""),
                   "fallback_used": bool(state.get("fallback_used", False)), "eval": state.get("eval", {}),
                   "history_tokens": int(state.get("history_tokens", 0))}
        if traceparent:
            payload["traceparent"] = traceparent
        headers = {"X-Trace-Id": trace_id}
        if traceparent:
            headers["traceparent"] = traceparent
        return JSONResponse(payload, headers=headers)

    async def gen():
        disconnected = False
        async for frame in run_query_events(body, p, deps, trace_id, conv_id, traceparent):
            if await request.is_disconnected():
                disconnected = True
                try:
                    from app.services import metrics as _m

                    _m.observe_sse("POST /api/v1/query", "disconnect", 0.0)
                except Exception:
                    pass
                break
            yield frame

    headers = {"X-Trace-Id": trace_id}
    if traceparent:
        headers["traceparent"] = traceparent
    return EventSourceResponse(gen(), headers=headers, ping=15)

"""Phase 3 tests: SSE contract (specs/02 §3), tool calling, fallback mechanics. Offline (fakes)."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent import graph as G
from app.agent.graph import AgentDeps
from app.agent.llm import GenerateResult, LLMError
from app.agent.prompts import FRIENDLY_LLM_DOWN
from app.api.v1 import query as query_mod
from app.core.config import settings
from app.core.security import get_current_principal
from app.main import app
from app.schemas import Principal
from app.services.retriever import RetrievedChunk

ME = Principal(sub="u1", email="u1@x.com", roles=["viewer"])
C1 = RetrievedChunk(chunk_id="c1", doc_id="d1", title="Doc",
                    content="KromaRAG blends vector cosine with full-text rank", ord=0, score=0.9)


async def _gemini_ok(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="KromaRAG blends vector cosine [doc:Doc:0]",
                          prompt_tokens=100, completion_tokens=20, model=settings.gemini_model)


async def _groq_ok(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="Groq fallback answer [doc:Doc:0]",
                          prompt_tokens=50, completion_tokens=10, model=settings.groq_model)


async def _fail_429(prompt, system=None, timeout_s=25, client=None):
    raise LLMError("LLM_RATE_LIMIT", "429")


async def _fail_timeout(prompt, system=None, timeout_s=25, client=None):
    raise LLMError("LLM_TIMEOUT", "timeout")


async def _hybrid_ok(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25):
    return [C1]


async def _meta_ok(doc_id, principal, db=None):
    return {"id": doc_id}


@pytest.fixture
def authed(monkeypatch):
    app.dependency_overrides[get_current_principal] = lambda: ME
    monkeypatch.setattr(G, "hybrid_search", _hybrid_ok)
    monkeypatch.setattr(G, "get_doc_metadata", _meta_ok)
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_gemini_ok, groq_fn=_groq_ok))
    yield
    app.dependency_overrides.clear()


def _frames(text: str):
    """Parse SSE body into [(event, data)] ignoring : ping comments."""
    out = []
    for blk in text.replace("\r\n", "\n").split("\n\n"):
        ev, data = None, None
        for line in blk.splitlines():
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if ev:
            out.append((ev, json.loads(data) if data else None))
    return out


@pytest.mark.asyncio
async def test_sse_contract_order(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?", "top_k": 6})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert r.headers["X-Trace-Id"]
    frames = _frames(r.text)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "source" and kinds.count("source") == 1  # source first, exactly once
    assert kinds[-1] == "done" and kinds.count("done") == 1  # done last, exactly once
    assert "token" in kinds
    src = frames[0][1]["chunks"][0]
    assert (src["chunk_id"], src["doc_id"], src["title"]) == ("c1", "d1", "Doc") and src["score"] == 0.9
    answer = "".join(d["delta"] for k, d in frames if k == "token")
    assert "[doc:Doc:0]" in answer
    done = frames[-1][1]
    assert done["trace_id"] == r.headers["X-Trace-Id"] and done["conv_id"]
    assert done["usage"] == {"prompt_tokens": 100, "completion_tokens": 20, "model": settings.gemini_model}


@pytest.mark.asyncio
async def test_stream_false_json(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?", "stream": False})
    assert r.status_code == 200 and r.headers["X-Trace-Id"]
    body = r.json()
    assert body["trace_id"] == r.headers["X-Trace-Id"]
    assert "[doc:Doc:0]" in body["answer"] and body["chunks"][0]["doc_id"] == "d1"
    assert body["fallback_used"] is False and body["eval"]["faithfulness"] >= 0.85


@pytest.mark.asyncio
async def test_fallback_error_event_then_done(authed, monkeypatch):
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_fail_429, groq_fn=_groq_ok))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"})
    frames = _frames(r.text)
    kinds = [k for k, _ in frames]
    assert "error" in kinds and kinds[-1] == "done" and kinds.count("done") == 1
    err = next(d for k, d in frames if k == "error")
    assert err["code"] == "LLM_FALLBACK"
    # Sanitized: raw primary error (429) must not leak; fallback answer still streams.
    assert "429" not in err["message"]
    assert "token" in kinds
    assert next(d for k, d in frames if k == "done")["fallback_used"] is True


@pytest.mark.asyncio
async def test_total_failure_error_no_tokens(authed, monkeypatch):
    """Total failure: sanitized error + friendly tokens, then done (no raw leak)."""
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_fail_timeout, groq_fn=_fail_timeout))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"})
    frames = _frames(r.text)
    kinds = [k for k, _ in frames]
    err = next(d for k, d in frames if k == "error")
    assert err["code"] == "LLM_TIMEOUT"
    assert err["message"] == FRIENDLY_LLM_DOWN
    assert "timeout" not in err["message"].lower()
    # Friendly message streams as tokens so the UI shows text.
    assert "token" in kinds
    answer = "".join(d["delta"] for k, d in frames if k == "token")
    assert answer == FRIENDLY_LLM_DOWN
    assert kinds.count("done") == 1
    assert kinds[0] == "source" and kinds.count("source") == 1


@pytest.mark.asyncio
async def test_total_failure_sanitizes_raw_groq_403(authed, monkeypatch):
    """Groq 403 Access Denied raw text must never reach the SSE stream."""
    async def _fail_403(prompt, system=None, timeout_s=25, client=None):
        raise LLMError("LLM_ERROR", "groq 403: {\"error\":{\"message\":\"Access denied.\"}}", retryable=False)

    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_fail_timeout, groq_fn=_fail_403))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"})
    frames = _frames(r.text)
    err = next(d for k, d in frames if k == "error")
    assert err["message"] == FRIENDLY_LLM_DOWN
    assert "403" not in err["message"] and "Access denied" not in err["message"]
    assert "Access denied" not in "".join(d["delta"] for k, d in frames if k == "token")
    assert "Access denied" not in r.text


@pytest.mark.asyncio
async def test_nonstream_total_failure_returns_friendly_answer(authed, monkeypatch):
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_fail_timeout, groq_fn=_fail_timeout))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?", "stream": False})
    assert r.status_code == 200
    assert r.json()["answer"] == FRIENDLY_LLM_DOWN


@pytest.mark.asyncio
async def test_run_query_events_direct_unit(monkeypatch):
    from app.schemas import QueryRequest

    monkeypatch.setattr(G, "hybrid_search", _hybrid_ok)
    body = QueryRequest(query="hello", top_k=6, stream=True)
    frames = [f async for f in query_mod.run_query_events(
        body, ME, AgentDeps(gemini_fn=_gemini_ok, groq_fn=_groq_ok), "t9", "v9")]
    kinds = [f["event"] for f in frames]
    assert kinds[0] == "source" and kinds[-1] == "done"
    assert json.loads(frames[0]["data"]) == {"chunks": []}  # direct route: no retrieval
    assert json.loads(frames[-1]["data"])["trace_id"] == "t9"


@pytest.mark.asyncio
async def test_blank_query_422(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "   "})
    assert r.status_code == 422

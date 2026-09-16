"""Phase 3 tests: LangGraph nodes, tool calling (max 3 steps), fallback, guardrails, evals. Offline."""
import sys
import types

import pytest

from app.agent import evaluator as eval_mod
from app.agent import graph as G
from app.agent.graph import AgentDeps, FallbackGraph, arun, build_graph, route_intent
from app.agent.guardrails import apply_guardrails, redact_pii
from app.agent.llm import GenerateResult, LLMError, generate_with_fallback, groq_generate
from app.agent.prompts import NO_EVIDENCE
from app.core.config import settings
from app.services.retriever import RetrievedChunk

C1 = RetrievedChunk(chunk_id="c1", doc_id="d1", title="Doc", content="KromaRAG blends vector cosine with full-text rank", ord=0, score=0.9)
C2 = RetrievedChunk(chunk_id="c2", doc_id="d2", title="Guide", content="RBAC filters every query by user roles", ord=1, score=0.6)


def _state(query="What is KromaRAG?", **kw):
    s = {"query": query, "principal_sub": "u1", "principal_roles": ["viewer"],
         "top_k": 6, "trace_id": "t1", "conv_id": "v1"}
    s.update(kw)
    return s


# ── router ──

@pytest.mark.parametrize("q", ["hello", "Hi!", "thanks", "who are you", "Good morning"])
def test_router_direct(q):
    assert route_intent(q) == "direct"


@pytest.mark.parametrize("q", ["What is KromaRAG?", "hello world explain hybrid search in detail please",
                               "How does RBAC filtering work?", "   "])
def test_router_retrieval(q):
    assert route_intent(q) == "retrieval"


# ── retriever / tools ──

@pytest.mark.asyncio
async def test_retriever_calls_tools_bounded(monkeypatch):
    calls = {"hybrid": 0, "meta": []}

    async def fake_hybrid(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25):
        calls["hybrid"] += 1
        assert principal.sub == "u1" and top_k == 6
        return [C1, C2]

    async def fake_meta(doc_id, principal, db=None):
        calls["meta"].append(doc_id)
        return {"id": doc_id}

    monkeypatch.setattr(G, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(G, "get_doc_metadata", fake_meta)
    st = await G.node_retriever(_state(), AgentDeps(db=object()))
    assert [c.chunk_id for c in st["chunks"]] == ["c1", "c2"]
    assert st["tool_steps"] == 3  # 1 hybrid + 2 metadata (max 3)
    assert sorted(calls["meta"]) == ["d1", "d2"]


@pytest.mark.asyncio
async def test_retriever_no_db():
    st = await G.node_retriever(_state(), AgentDeps(db=None))
    assert st["chunks"] == [] and "no-db" in st["flags"]


@pytest.mark.asyncio
async def test_reranker_attaches_citations():
    st = await G.node_reranker(_state(chunks=[C2, C1]), AgentDeps())
    assert [c["chunk_id"] for c in st["citations"]] == ["c1", "c2"]  # score order
    assert st["citations"][0]["score"] == 0.9


# ── generator / fallback ──

async def _ok_gemini(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="Grounded answer [doc:Doc:0]", prompt_tokens=10, completion_tokens=5, model=settings.gemini_model)


async def _fail_429(prompt, system=None, timeout_s=25, client=None):
    raise LLMError("LLM_RATE_LIMIT", "429")


async def _ok_groq(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="Fallback answer [doc:Guide:1]", prompt_tokens=8, completion_tokens=4, model=settings.groq_model)


@pytest.mark.asyncio
async def test_generate_fallback_on_rate_limit():
    res = await generate_with_fallback("q", "s", gemini_fn=_fail_429, groq_fn=_ok_groq)
    assert res.model == settings.groq_model and res.fallback_used is True
    assert "LLM_RATE_LIMIT" in (res.fallback_error or "")


@pytest.mark.asyncio
async def test_generate_non_retryable_raises():
    async def bad(prompt, system=None, timeout_s=25, client=None):
        raise LLMError("LLM_ERROR", "bad key", retryable=False)

    with pytest.raises(LLMError):
        await generate_with_fallback("q", "s", gemini_fn=bad, groq_fn=_ok_groq)


@pytest.mark.asyncio
async def test_node_generator_fallback_path():
    st = _state(route="retrieval", chunks=[C1], citations=[])
    out = await G.node_generator(st, AgentDeps(gemini_fn=_fail_429, groq_fn=_ok_groq))
    assert out["model"] == settings.groq_model and out["fallback_used"] is True


@pytest.mark.asyncio
async def test_node_generator_total_failure():
    async def down(*a, **k):
        raise LLMError("LLM_TIMEOUT", "timeout")

    st = await G.node_generator(_state(route="retrieval", chunks=[C1]), AgentDeps(gemini_fn=down, groq_fn=down))
    assert st["error"]["code"] == "LLM_TIMEOUT" and st["draft"] == ""


# ── groq_generate httpx-level regression (offline) ──

class _GroqResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self):
        return self._payload


class _GroqClient:
    """Fake httpx.AsyncClient: records model sent, returns canned response."""

    def __init__(self, resp: _GroqResp):
        self.resp = resp
        self.sent_model: str | None = None

    async def post(self, url, headers=None, json=None, timeout=None):
        self.sent_model = (json or {}).get("model")
        return self.resp

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_groq_generate_sends_configured_model_200(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "k-test")
    resp = _GroqResp(200, {"choices": [{"message": {"content": "hi"}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    client = _GroqClient(resp)
    res = await groq_generate("ping", None, 25, client)
    assert client.sent_model == settings.groq_model
    assert settings.groq_model == "openai/gpt-oss-120b"  # fallback default
    assert res.text == "hi" and res.model == settings.groq_model


@pytest.mark.asyncio
async def test_groq_generate_403_is_terminal_not_retryable(monkeypatch):
    """Cloudflare/WAF 403 (Access denied) must surface as retryable=False so the
    SSE layer can sanitize it into FRIENDLY_LLM_DOWN instead of looping."""
    monkeypatch.setattr(settings, "groq_api_key", "k-test")
    resp = _GroqResp(403, text='{"error":{"message":"Access denied. Please check your network settings."}}')
    with pytest.raises(LLMError) as exc:
        await groq_generate("ping", None, 25, _GroqClient(resp))
    assert exc.value.retryable is False
    assert "403" in str(exc.value)


# ── guardrails / evaluator ──

def test_guardrail_no_evidence():
    ans, flags = apply_guardrails("Some invented answer", [])
    assert ans == NO_EVIDENCE and "no-evidence" in flags


def test_guardrail_pii():
    assert redact_pii("mail me at admin@kromarag.com now") == "mail me at [redacted-email] now"
    assert "[doc:T:0]" in redact_pii("see [doc:T:0]")  # citations untouched


def test_eval_faithful_and_forbidden():
    good = eval_mod.evaluate("KromaRAG blends vector cosine [doc:Doc:0]", [C1], "What is KromaRAG?")
    assert good["faithfulness"] == 1.0 and good["leakage"] == 1.0
    bad = eval_mod.evaluate("The moon is cheese", [C1], "What?", forbidden_claims=("moon is cheese",))
    assert bad["faithfulness"] == 0.0
    leak = eval_mod.evaluate("RBAC filters queries [doc:Guide:1]", [C1, C2], "RBAC?", allowed_doc_ids={"d1"})
    assert leak["leakage"] == 0.5


# ── full pipeline ──

@pytest.mark.asyncio
async def test_arun_rag_integration(monkeypatch):
    async def fake_hybrid(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25):
        return [C1]

    async def fake_meta(doc_id, principal, db=None):
        return {"id": doc_id}

    async def fake_gemini(prompt, system=None, timeout_s=25, client=None):
        assert "KromaRAG" in prompt  # query embedded in RAG prompt
        return GenerateResult(text="KromaRAG blends vector cosine [doc:Doc:0]",
                              prompt_tokens=100, completion_tokens=20, model=settings.gemini_model)

    monkeypatch.setattr(G, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(G, "get_doc_metadata", fake_meta)
    st = await arun(_state(), AgentDeps(db=object(), gemini_fn=fake_gemini, groq_fn=_ok_groq))
    assert st["route"] == "retrieval"
    assert st["citations"][0]["doc_id"] == "d1"
    assert "[doc:Doc:0]" in st["answer"]
    assert st["usage"] == {"prompt_tokens": 100, "completion_tokens": 20, "model": settings.gemini_model}
    assert st["eval"]["faithfulness"] >= 0.85 and st["eval"]["leakage"] == 1.0
    assert st["fallback_used"] is False


@pytest.mark.asyncio
async def test_arun_direct_skips_retrieval(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("retriever must not run for greetings")

    monkeypatch.setattr(G, "hybrid_search", boom)
    st = await arun(_state(query="hello"), AgentDeps(gemini_fn=_ok_gemini, groq_fn=_ok_groq))
    assert st["route"] == "direct" and st["citations"] == []
    assert "Grounded answer" in st["answer"]


# ── graph compilation ──

@pytest.mark.asyncio
async def test_fallback_graph_ainvoke():
    g = FallbackGraph(AgentDeps(gemini_fn=_ok_gemini, groq_fn=_ok_groq))
    st = await g.ainvoke(_state(query="hello"))
    assert st["route"] == "direct"


def test_build_graph_uses_langgraph_when_present(monkeypatch):
    seen = {}

    class FakeSG:
        def __init__(self, state):
            seen["state"] = state

        def add_node(self, name, fn):
            seen.setdefault("nodes", []).append(name)

        def set_entry_point(self, name):
            seen["entry"] = name

        def add_conditional_edges(self, src, fn):
            seen["cond"] = src

        def add_edge(self, a, b):
            seen.setdefault("edges", []).append((a, b))

        def compile(self):
            seen["compiled"] = True
            return "COMPILED"

    mod = types.ModuleType("langgraph")
    sub = types.ModuleType("langgraph.graph")
    sub.StateGraph = FakeSG
    sub.END = "END"
    monkeypatch.setitem(sys.modules, "langgraph", mod)
    monkeypatch.setitem(sys.modules, "langgraph.graph", sub)
    assert build_graph() == "COMPILED"
    assert seen["nodes"] == ["router", "retriever", "reranker", "generator", "guardrail", "evaluator"]
    assert seen["entry"] == "router" and seen["cond"] == "router" and seen["compiled"]

"""Telemetry & operations tests (specs/09 §5): W3C traceparent propagation,
/metrics dual-security matrix (404/401/403/200), Prometheus exposition shape.
Offline (fakes); metrics store reset per test for isolation."""
from __future__ import annotations

import json
import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.agent import graph as G
from app.agent.graph import AgentDeps
from app.agent.llm import GenerateResult, LLMError
from app.api.v1 import query as query_mod
from app.core import tracecontext as tc
from app.core.config import settings
from app.core import security as sec
from app.core.security import get_current_principal
from app.main import app
from app.schemas import Principal
from app.services import metrics as m
from app.services.retriever import RetrievedChunk

ME = Principal(sub="u1", email="u1@x.com", roles=["viewer"])
C1 = RetrievedChunk(chunk_id="c1", doc_id="d1", title="Doc",
                    content="KromaRAG blends vector cosine with full-text rank", ord=0, score=0.9)

TP_VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TP_TRACE_DASH = "4bf92f35-77b3-4da6-a3ce-929d0e0e4736"


@pytest.fixture(autouse=True)
def _reset_metrics():
    m.reset_metrics()
    yield
    m.reset_metrics()


async def _gemini_ok(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="KromaRAG blends vector cosine [doc:Doc:0]",
                          prompt_tokens=100, completion_tokens=20, model=settings.gemini_model)


async def _groq_ok(prompt, system=None, timeout_s=25, client=None):
    return GenerateResult(text="Groq fallback answer [doc:Doc:0]",
                          prompt_tokens=50, completion_tokens=10, model=settings.groq_model)


async def _fail_429(prompt, system=None, timeout_s=25, client=None):
    raise LLMError("LLM_RATE_LIMIT", "429")


async def _hybrid_ok(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25, trace_id=None):
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


async def _no_jwks() -> list[dict]:
    return []


def _signed_token(secret: str, sub: str = "svc", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": f"{sub}@x.com",
        "roles": roles or ["admin"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "exp": now + 3600,
        "iat": now,
        "jti": "t9",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _frames(text: str):
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


# ── traceparent unit matrix ──

def test_parse_traceparent_valid():
    assert tc.parse_traceparent(TP_VALID) == (
        "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", "01")


def test_parse_traceparent_rejects():
    assert tc.parse_traceparent(None) is None
    assert tc.parse_traceparent("bogus") is None
    assert tc.parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None  # zero trace
    assert tc.parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01") is None
    assert tc.parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") is None
    assert tc.parse_traceparent("00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01") is not None  # case-insensitive


def test_join_valid_traceparent_wins_over_xtraceid():
    ctx = tc.join_trace_context(TP_VALID, None, "11111111-1111-1111-1111-111111111111")
    assert ctx["trace_id"] == TP_TRACE_DASH
    assert ctx["traceparent_out"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert len(ctx["span_id"]) == 16


def test_join_invalid_mints_fresh():
    ctx = tc.join_trace_context("bogus", None, None)
    assert ctx["trace_id"] and ctx["trace_id"] != TP_TRACE_DASH
    assert ctx["traceparent_out"].startswith("00-")
    assert ctx["traceparent_in"] is None


def test_join_reuses_valid_xtraceid():
    ctx = tc.join_trace_context(None, None, TP_TRACE_DASH)
    assert ctx["trace_id"] == TP_TRACE_DASH


def test_route_template_redacts_ids():
    assert tc.route_template("GET", "/api/v1/documents/123e4567-e89b-12d3-a456-426614174000") == \
        "GET /api/v1/documents/{id}"
    assert tc.route_template("GET", "/api/v1/documents/42") == "GET /api/v1/documents/{id}"
    assert tc.route_template("POST", "/api/v1/query") == "POST /api/v1/query"


# ── propagation over HTTP ──

@pytest.mark.asyncio
async def test_traceparent_joins_query_sse(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"},
                         headers={"traceparent": TP_VALID})
    assert r.status_code == 200
    assert r.headers["X-Trace-Id"] == TP_TRACE_DASH
    assert r.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    frames = _frames(r.text)
    done = next(d for k, d in frames if k == "done")
    assert done["trace_id"] == TP_TRACE_DASH == r.headers["X-Trace-Id"]
    assert done["traceparent"] == r.headers["traceparent"]


@pytest.mark.asyncio
async def test_invalid_traceparent_mints_fresh_no_400(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "hello there friend, tell me more?"},
                         headers={"traceparent": "bogus"})
    assert r.status_code == 200
    assert r.headers["X-Trace-Id"] and r.headers["X-Trace-Id"] != TP_TRACE_DASH
    assert r.headers["traceparent"].startswith("00-")
    frames = _frames(r.text)
    done = next(d for k, d in frames if k == "done")
    assert done["trace_id"] == r.headers["X-Trace-Id"]


@pytest.mark.asyncio
async def test_trace_headers_on_health_and_404(authed):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz", headers={"traceparent": TP_VALID})
        assert r.headers["X-Trace-Id"] == TP_TRACE_DASH
        assert "traceparent" in {k.lower() for k in r.headers.keys()}
        n = await c.get("/no-such-route-xyz", headers={"traceparent": TP_VALID})
        assert n.status_code == 404
        assert n.headers["X-Trace-Id"] == TP_TRACE_DASH


@pytest.mark.asyncio
async def test_traceparent_on_rate_limited(monkeypatch):
    import base64 as _b64

    def _b64url(o):
        return _b64.urlsafe_b64encode(json.dumps(o, separators=(",", ":")).encode()).decode().rstrip("=")

    tok = f"{_b64url({'alg': 'HS256'})}.{_b64url({'sub': 'rl-tp', 'roles': ['viewer']})}.sig"
    monkeypatch.setattr(settings, "rate_limit_query_per_min", 1)
    hdr = {"Authorization": f"Bearer {tok}", "traceparent": TP_VALID}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/query", json={"query": "one"}, headers=hdr)
        r = await c.post("/api/v1/query", json={"query": "two"}, headers=hdr)
    assert r.status_code == 429
    assert r.headers["X-Trace-Id"] == TP_TRACE_DASH
    assert r.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")


# ── /metrics dual-security matrix ──

def _trust(monkeypatch):
    monkeypatch.setattr(settings, "metrics_scrape_allowlist", "")


@pytest.mark.asyncio
async def test_metrics_untrusted_network_404(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    monkeypatch.setattr(settings, "metrics_scrape_allowlist", "10.0.0.0/8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/metrics")
    assert r.status_code == 404
    assert r.json() == {"detail": {"code": "NOT_FOUND", "message": "not found"}}
    assert r.headers["X-Trace-Id"] and "traceparent" in {k.lower() for k in r.headers.keys()}


@pytest.mark.asyncio
async def test_metrics_trusted_no_jwt_401(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    _trust(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/metrics", headers={"X-Cloud-Run-Internal": "true"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_metrics_viewer_403(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    _trust(monkeypatch)
    tok = _signed_token(settings.effective_current_secret, sub="alice", roles=["viewer"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/metrics", headers={"Authorization": f"Bearer {tok}",
                                             "X-Cloud-Run-Internal": "true"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_metrics_admin_200_prometheus_shape(monkeypatch, authed):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    _trust(monkeypatch)
    # Generate traffic so histograms carry exemplars.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/query", json={"query": "What is KromaRAG?"},
                     headers={"traceparent": TP_VALID})
        tok = _signed_token(settings.effective_current_secret, sub="svc", roles=["admin"])
        r = await c.get("/metrics", headers={"Authorization": f"Bearer {tok}",
                                             "X-Cloud-Run-Internal": "true"})
    assert r.status_code == 200
    ctype = r.headers["content-type"]
    assert "text/plain" in ctype and "version=0.0.4" in ctype
    assert r.headers["Cache-Control"] == "no-store"
    body = r.text
    for name in ["http_server_duration_seconds", "llm_tokens_total", "llm_fallback_total",
                 "vector_search_duration_seconds_bucket", "db_pool_in_use",
                 "kromarag_metrics_cardinality_shed_total", "kromarag_info"]:
        assert name in body, name
    assert "# HELP " in body and "# TYPE " in body
    assert 'le="+Inf"' in body
    assert '# {trace_id="' in body  # exemplar present after live traffic
    # No PII in labels.
    assert "u1@x.com" not in body and "eyJ" not in body


@pytest.mark.asyncio
async def test_metrics_exempt_from_rate_limit(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    monkeypatch.setattr(settings, "rate_limit_unauth_per_min", 1)
    _trust(monkeypatch)
    tok = _signed_token(settings.effective_current_secret, sub="svc", roles=["admin"])
    hdr = {"Authorization": f"Bearer {tok}", "X-Cloud-Run-Internal": "true"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.get("/metrics", headers=hdr)
        r2 = await c.get("/metrics", headers=hdr)
    assert r1.status_code == 200 and r2.status_code == 200  # never 429


@pytest.mark.asyncio
async def test_fallback_counter_increments(monkeypatch, authed):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    _trust(monkeypatch)
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=object(), gemini_fn=_fail_429, groq_fn=_groq_ok))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        q = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"})
        assert q.status_code == 200
        tok = _signed_token(settings.effective_current_secret, sub="svc", roles=["admin"])
        r = await c.get("/metrics", headers={"Authorization": f"Bearer {tok}",
                                             "X-Cloud-Run-Internal": "true"})
    body = r.text
    assert 'llm_fallback_total{' in body
    assert 'code="LLM_RATE_LIMIT"' in body
    # Token series incremented by the groq fallback usage (50 prompt / 10 completion).
    assert 'llm_tokens_total{service="kromarag-api",model="openai/gpt-oss-120b",kind="prompt"} 50' in body


def test_cardinality_bounded_after_many_routes():
    # Middleware redacts ids via route_template before observing (spec §3.3):
    # 1000 distinct doc ids collapse to a single template series.
    for i in range(1000):
        m.observe_http(tc.route_template("GET", f"/api/v1/documents/{i}"), "200", 0.01)
    assert m.active_series_count() < 50
    body = m.render_prometheus()
    assert "kromarag_metrics_cardinality_shed_total" in body
    assert 'route="GET /api/v1/documents/{id}"' in body
    assert "@" not in body and "eyJ" not in body

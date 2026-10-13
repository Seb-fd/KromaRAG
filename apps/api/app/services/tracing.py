"""LangSmith tracing per specs/04 §1 + W3C propagation per specs/09 §3.1.

Trace hierarchy: kromarag.query(trace_id) → children router, retriever.sql, embed,
generator.gemini|groq, guardrail. Metadata carries trace_id, user_sub_hash, roles,
top_k, model, fallback_used, chunk_ids, scores — never raw JWT/email/full content.

LangSmith remains the system of record (spec 09 decision: OTel minimal). This
module only joins/propagates W3C context and annotates spans with trace links.
No OTLP exporter Phase 1; outbound inject is header formatting only (no I/O).
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager, contextmanager

log = logging.getLogger(__name__)


def tracing_enabled() -> bool:
    from app.core.config import settings

    return bool(settings.langchain_tracing_v2 and settings.langsmith_api_key)


def sanitize_metadata(meta: dict) -> dict:
    """Hash identity, truncate excerpts. Call before any trace/log call."""
    out = dict(meta)
    if "user_sub" in out:
        out["user_sub_hash"] = hashlib.sha256(str(out.pop("user_sub")).encode()).hexdigest()[:16]
    out.pop("email", None)
    out.pop("jwt", None)
    for k in ("query_excerpt", "answer_excerpt"):
        if isinstance(out.get(k), str):
            out[k] = out[k][:500]
    if isinstance(out.get("chunks"), list):
        out["chunk_ids"] = [c.get("chunk_id") if isinstance(c, dict) else getattr(c, "chunk_id", None) for c in out.pop("chunks")]
    return out


def _ensure_env() -> None:
    from app.core.config import settings

    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true" if settings.langchain_tracing_v2 else "false")
    if settings.langsmith_api_key:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langchain_project)
    os.environ.setdefault("LANGCHAIN_ENDPOINT", settings.langchain_endpoint)


@contextmanager
def span(name: str, metadata: dict | None = None):
    """Sync span: LangSmith `trace` when available+enabled, else structured log. Never raises."""
    meta = sanitize_metadata(metadata or {})
    if tracing_enabled():
        _ensure_env()
        try:
            from langsmith import trace as ls_trace

            with ls_trace(name=name, run_type="chain", metadata=meta):
                yield
            return
        except Exception as e:  # noqa: BLE001 — tracing must never break serving
            log.warning("langsmith span %s failed: %s", name, e)
    log.info("trace span %s %s", name, {k: v for k, v in meta.items() if k != "answer_excerpt"})
    yield


@asynccontextmanager
async def aspan(name: str, metadata: dict | None = None):
    with span(name, metadata):
        yield


def log_usage(trace_id: str, model: str, prompt_tokens: int, completion_tokens: int,
              latency_ms: int, fallback_used: bool = False, history_tokens: int = 0) -> dict:
    """Per-request token/cost record for Cloud Run JSON logs (specs/04 §2, specs/06 §1.4)."""
    from app.core.costs import cost_usd

    rec = {
        "trace_id": trace_id, "model": model, "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens, "cost_usd": cost_usd(model, prompt_tokens, completion_tokens),
        "latency_ms": latency_ms, "fallback_used": fallback_used,
        "history_tokens": int(history_tokens or 0),
    }
    log.info("kroma_usage %s", rec)
    # Spec 09 §3.3: join token/latency series (best-effort, never breaks logging).
    try:
        from app.services import metrics as _m

        if model:
            _m.inc_tokens(model, "prompt", int(prompt_tokens or 0))
            _m.inc_tokens(model, "completion", int(completion_tokens or 0))
            _m.observe_llm(model, "fallback" if fallback_used else "ok",
                           max(0.0, float(latency_ms or 0) / 1000.0), trace_id=trace_id)
        if int(history_tokens or 0):
            _m.inc_history_tokens(int(history_tokens or 0))
    except Exception:
        pass
    return rec


def outbound_headers(trace_id: str, span_id: str | None = None,
                     sampled: bool = True, tracestate: str | None = None) -> dict:
    """Headers to inject on outbound HTTP calls (spec 09 §3.1 rule 9). No I/O."""
    try:
        import secrets as _secrets

        from app.core.tracecontext import build_traceparent, truncate_tracestate

        sid = span_id or _secrets.token_hex(8)
        headers = {"traceparent": build_traceparent(trace_id, sid, sampled)}
        ts = truncate_tracestate(tracestate)
        if ts:
            headers["tracestate"] = ts
        return headers
    except Exception:
        return {}

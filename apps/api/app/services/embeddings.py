"""Gemini embeddings per specs/03 §3: 768 dims, batch 32, retry 3x, LRU query cache.

Backend: `google-genai` SDK (`genai.Client(api_key=...)` + `client.models.embed_content`).
Default model `gemini-embedding-001` with `output_dimensionality=768` for pgvector
VECTOR(768) compat. `text-embedding-004` was retired (404 NOT_FOUND on v1beta as of
2026 — confirmed via ModelService.ListModels) so it is no longer the default.

A raw-httpx fallback is kept for injected test clients (FakeClient in unit tests)
and respects `settings.embedding_model` dynamically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, cast

import httpx

log = logging.getLogger(__name__)

# New default (verified live: ListModels contains it, 004 does not).
DEFAULT_MODEL = "gemini-embedding-001"
LEGACY_MODEL = "text-embedding-004"
DIM = 768
BATCH = 32
API_HOST = "https://generativelanguage.googleapis.com/v1beta/models"

# Backwards-compat alias: some callers may import MODEL.
MODEL = DEFAULT_MODEL

_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
CACHE_MAX = 500
CACHE_TTL = 600.0


def _api_key() -> str:
    from app.core.config import settings

    return settings.gemini_api_key or settings.google_api_key


def _model() -> str:
    """Resolve configured model id, tolerating a fully-qualified 'models/...' value."""
    from app.core.config import settings

    raw = (settings.embedding_model or DEFAULT_MODEL).strip()
    raw = raw.removeprefix("models/")
    if raw == LEGACY_MODEL:
        log.warning(
            "embedding model %s is retired (404 NOT_FOUND); set EMBEDDING_MODEL=%s",
            LEGACY_MODEL, DEFAULT_MODEL,
        )
    return raw


def _wants_truncation(model: str) -> bool:
    # gemini-embedding-* defaults to 3072 dims; request 768 for VECTOR(768) compat.
    return model.startswith("gemini-embedding")


def _norm(q: str) -> str:
    return " ".join(q.strip().lower().split())


def embed_query_cached(query: str) -> list[float] | None:
    """Return cached query embedding if fresh, else None."""
    k = _norm(query)
    hit = _cache.get(k)
    if not hit:
        return None
    ts, vec = hit
    if time.time() - ts > CACHE_TTL:
        _cache.pop(k, None)
        return None
    _cache.move_to_end(k)
    return vec


def _store_query(query: str, vec: list[float]) -> None:
    _cache[_norm(query)] = (time.time(), vec)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def _format_sdk_error(e: Exception, model: str) -> RuntimeError:
    msg = str(e)
    low = msg.lower()
    if "404" in msg or "not_found" in low or "is not found" in low:
        return RuntimeError(
            f"embedding model '{model}' not found (404 NOT_FOUND). "
            f"'{LEGACY_MODEL}' was retired; available embedding model is "
            f"'{DEFAULT_MODEL}' (verify via GET /v1beta/models). "
            f"Fix: set EMBEDDING_MODEL={DEFAULT_MODEL} in apps/api/.env. "
            f"Original error: {msg[:500]}"
        )
    if "400" in msg or "api key" in low or "api_key" in low or "invalid" in low:
        return RuntimeError(
            "Gemini embed failed: invalid API key or project setup. "
            "Use an AI Studio key (GOOGLE_API_KEY/GEMINI_API_KEY) with the "
            "Generative Language API enabled. If this key is a Vertex AI / OAuth "
            "token (e.g. non-AIza prefix), either swap to an AI Studio key or "
            "construct genai.Client(vertexai=True, project=..., location=...) instead "
            f"of api_key auth. Model='{model}'. Original error: {msg[:500]}"
        )
    return RuntimeError(f"Gemini embed failed for model '{model}': {msg[:500]}")


def _sdk_embed_batch_sync(texts: list[str], model: str, key: str) -> list[list[float]]:
    """Blocking SDK call (run via asyncio.to_thread). One API call for the batch."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    kwargs: dict = {}
    if _wants_truncation(model):
        kwargs["config"] = types.EmbedContentConfig(output_dimensionality=DIM)
    try:
        res = client.models.embed_content(model=model, contents=cast(Any, texts), **kwargs)
    except Exception as e:  # noqa: BLE001 — reformat below
        raise _format_sdk_error(e, model) from e
    try:
        embeddings = res.embeddings or []
        vecs = [list(em.values or []) for em in embeddings]
    except (AttributeError, TypeError) as e:
        raise RuntimeError(f"Gemini embed: unexpected response shape for model '{model}': {e}") from e
    for v in vecs:
        if len(v) != DIM:
            raise ValueError(
                f"expected dim {DIM} from model '{model}', got {len(v)} "
                f"(request output_dimensionality={DIM} for gemini-embedding-*)"
            )
    if len(vecs) != len(texts):
        raise ValueError(f"embed count mismatch: {len(vecs)} != {len(texts)}")
    return vecs


async def _sdk_embed_batches(texts: list[str], model: str, key: str) -> list[list[float]]:
    from app.core.config import settings

    batch_size = int(getattr(settings, "embed_batch", BATCH) or BATCH)
    out: list[list[float]] = []
    last: Exception | None = None
    for i in range(0, len(texts), batch_size):
        chunk = [t[:8000] for t in texts[i:i + batch_size]]
        for attempt in range(3):
            try:
                vecs = await asyncio.to_thread(_sdk_embed_batch_sync, chunk, model, key)
                out.extend(vecs)
                last = None
                break
            except ValueError:
                raise  # dim/count mismatch: do not retry
            except RuntimeError as e:
                # 404/model-not-found and invalid-key are non-retryable
                if "404" in str(e) or "not found" in str(e).lower() or "invalid API key" in str(e):
                    raise
                last = e
                await asyncio.sleep(2**attempt)
    if last is not None:
        raise last
    return out


async def _batch_request_legacy(
    texts: list[str], key: str, client: httpx.AsyncClient, model: str
) -> list[list[float]]:
    """Raw-httpx fallback (used when a test injects FakeClient). Respects `model`."""
    url = f"{API_HOST}/{model}:batchEmbedContents"
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t[:8000]}]},
                **({"outputDimensionality": DIM} if _wants_truncation(model) else {}),
            }
            for t in texts
        ]
    }
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = await client.post(url, params={"key": key}, json=body, timeout=30.0)
            status = getattr(r, "status_code", None)
            if status == 404:
                detail = getattr(r, "text", "")[:500] if hasattr(r, "text") else ""
                raise RuntimeError(
                    f"embedding model '{model}' not found (404 NOT_FOUND). "
                    f"'{LEGACY_MODEL}' was retired; set EMBEDDING_MODEL={DEFAULT_MODEL}. "
                    f"API detail: {detail}"
                )
            r.raise_for_status()
            data = r.json()
            vecs = [e["values"] for e in data["embeddings"]]
            for v in vecs:
                if len(v) != DIM:
                    raise ValueError(f"expected dim {DIM}, got {len(v)}")
            return vecs
        except (ValueError, RuntimeError):
            raise
        except Exception as e:  # noqa: BLE001 — retry then raise
            last = e
            await asyncio.sleep(2**attempt)
    assert last is not None
    raise last


async def embed_texts(texts: list[str], client: httpx.AsyncClient | None = None) -> list[list[float]]:
    """Embed 1..N texts in batches of 32. Requires GOOGLE/GEMINI_API_KEY."""
    if not texts:
        return []
    key = _api_key()
    if not key:
        raise RuntimeError("missing GOOGLE_API_KEY/GEMINI_API_KEY for embeddings")
    model = _model()
    if client is not None:
        # Injected httpx client (unit tests / custom transport): legacy REST path.
        from app.core.config import settings

        batch_size = int(getattr(settings, "embed_batch", BATCH) or BATCH)
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            out.extend(await _batch_request_legacy(texts[i:i + batch_size], key, client, model))
        return out
    return await _sdk_embed_batches(texts, model, key)


async def embed_query(query: str, client: httpx.AsyncClient | None = None) -> list[float]:
    import time as _time

    hit = embed_query_cached(query)
    if hit is not None:
        try:
            from app.services import metrics as _m

            _m.observe_embed(_model(), True, 0.0)
        except Exception:
            pass
        return hit
    t0 = _time.monotonic()
    try:
        vec = (await embed_texts([query], client))[0]
    except Exception:
        raise
    finally:
        try:
            from app.services import metrics as _m

            _m.observe_embed(_model(), False, _time.monotonic() - t0)
        except Exception:
            pass
    _store_query(query, vec)
    return vec

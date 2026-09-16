"""LLM clients per specs/03 §5: Gemini 3.6-flash primary, Groq llama fallback on timeout/429/404-NOT_FOUND/5xx.

All network fns accept injectable fakes (gemini_fn/groq_fn) so unit tests run offline.
Retired-model 404s are retryable so generate_with_fallback triggers the Groq fallback.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class GenerateResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    fallback_used: bool = False
    fallback_error: str | None = None


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _gemini_key() -> str:
    from app.core.config import settings

    return settings.gemini_api_key or settings.google_api_key


async def gemini_generate(prompt: str, system: str | None = None, timeout_s: int = 25,
                          client: httpx.AsyncClient | None = None,
                          traceparent: str | None = None, tracestate: str | None = None) -> GenerateResult:
    from app.core.config import settings

    key = _gemini_key()
    if not key:
        raise LLMError("LLM_ERROR", "missing GOOGLE_API_KEY/GEMINI_API_KEY", retryable=False)
    # Chaos hook (spec 09 §4.3, staging-only): force primary failure.
    try:
        if getattr(settings, "chaos_gemini_down", False) and not settings.is_prod:
            raise LLMError("LLM_TIMEOUT", "chaos: gemini blackhole (CHAOS_GEMINI_DOWN=1)")
    except LLMError:
        raise
    except Exception:
        pass
    body: dict = {"contents": [{"parts": [{"text": prompt[:12000]}]}]}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    headers: dict = {}
    if traceparent:
        headers["traceparent"] = traceparent
        if tracestate:
            headers["tracestate"] = tracestate
    own = client is None
    c = client or httpx.AsyncClient()
    try:
        try:
            r = await c.post(GEMINI_URL.format(model=settings.gemini_model), params={"key": key},
                             json=body, headers=headers or None, timeout=float(timeout_s))
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise LLMError("LLM_TIMEOUT", f"gemini timeout: {e}") from e
        if r.status_code == 429:
            raise LLMError("LLM_RATE_LIMIT", f"gemini rate limited (429): {r.text[:200]}")
        if r.status_code >= 500:
            raise LLMError("LLM_ERROR", f"gemini {r.status_code}: {r.text[:200]}")
        if r.status_code == 404 or "NOT_FOUND" in r.text.upper() or "IS NOT FOUND" in r.text.upper():
            # Retired/unknown model (e.g. models/gemini-2.0-flash 404) → fallback to Groq.
            raise LLMError("LLM_ERROR", f"gemini {r.status_code}: {r.text[:200]}", retryable=True)
        if r.status_code >= 400:
            raise LLMError("LLM_ERROR", f"gemini {r.status_code}: {r.text[:200]}", retryable=False)
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("LLM_ERROR", "gemini: unexpected response shape", retryable=False) from e
        um = data.get("usageMetadata", {})
        return GenerateResult(
            text=text,
            prompt_tokens=int(um.get("promptTokenCount") or estimate_tokens(prompt)),
            completion_tokens=int(um.get("candidatesTokenCount") or estimate_tokens(text)),
            model=settings.gemini_model,
        )
    finally:
        if own:
            await c.aclose()


async def groq_generate(prompt: str, system: str | None = None, timeout_s: int = 25,
                         client: httpx.AsyncClient | None = None,
                         traceparent: str | None = None, tracestate: str | None = None) -> GenerateResult:
    from app.core.config import settings

    if not settings.groq_api_key:
        raise LLMError("LLM_ERROR", "missing GROQ_API_KEY", retryable=False)
    own = client is None
    c = client or httpx.AsyncClient()
    try:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt[:12000]})
        headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
        if traceparent:
            headers["traceparent"] = traceparent
            if tracestate:
                headers["tracestate"] = tracestate
        try:
            r = await c.post(GROQ_URL, headers=headers,
                             json={"model": settings.groq_model, "messages": msgs}, timeout=float(timeout_s))
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise LLMError("LLM_TIMEOUT", f"groq timeout: {e}") from e
        if r.status_code == 429:
            raise LLMError("LLM_RATE_LIMIT", f"groq rate limited (429): {r.text[:200]}")
        if r.status_code >= 500:
            raise LLMError("LLM_ERROR", f"groq {r.status_code}: {r.text[:200]}")
        if r.status_code == 404 or "NOT_FOUND" in r.text.upper() or "IS NOT FOUND" in r.text.upper():
            raise LLMError("LLM_ERROR", f"groq {r.status_code}: {r.text[:200]}", retryable=True)
        if r.status_code >= 400:
            raise LLMError("LLM_ERROR", f"groq {r.status_code}: {r.text[:200]}", retryable=False)
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("LLM_ERROR", "groq: unexpected response shape", retryable=False) from e
        u = data.get("usage", {}) or {}
        return GenerateResult(
            text=text,
            prompt_tokens=int(u.get("prompt_tokens") or estimate_tokens(prompt)),
            completion_tokens=int(u.get("completion_tokens") or estimate_tokens(text)),
            model=settings.groq_model,
        )
    finally:
        if own:
            await c.aclose()


async def _call(fn, prompt: str, system: str | None, timeout_s: int, client,
                traceparent: str | None = None, tracestate: str | None = None):
    """Call provider fn with (prompt, system, timeout_s[, client]); tolerate 3-arg fakes."""
    if client is None:
        try:
            return await fn(prompt, system, timeout_s, traceparent=traceparent, tracestate=tracestate)
        except TypeError:
            return await fn(prompt, system, timeout_s)
    try:
        return await fn(prompt, system, timeout_s, client, traceparent=traceparent, tracestate=tracestate)
    except TypeError:
        pass
    try:
        return await fn(prompt, system, timeout_s, client)
    except TypeError:
        return await fn(prompt, system, timeout_s)


async def generate_with_fallback(prompt: str, system: str | None = None, timeout_s: int = 25,
                                 gemini_fn=None, groq_fn=None, client=None,
                                 traceparent: str | None = None,
                                 tracestate: str | None = None) -> GenerateResult:
    """Primary per settings.llm_primary (default gemini); fallback on retryable LLMError."""
    from app.core.config import settings

    order = [settings.llm_primary, settings.llm_fallback]
    fns = {"gemini": gemini_fn or gemini_generate, "groq": groq_fn or groq_generate}
    first_err: LLMError | None = None
    for i, name in enumerate(order):
        fn = fns.get(name)
        if fn is None:
            continue
        try:
            res = await _call(fn, prompt, system, timeout_s, client, traceparent, tracestate)
            if i > 0:
                res.fallback_used = True
                res.fallback_error = f"{first_err.code}: {first_err}" if first_err else None
            return res
        except LLMError as e:
            if not e.retryable or i == len(order) - 1:
                raise
            first_err = e
    raise LLMError("LLM_ERROR", "no LLM provider configured", retryable=False)

"""Token cost table per specs/04 §2. Prices indicative per 1M tokens; pin quarterly."""
from __future__ import annotations

# (input $/1M, output $/1M)
COSTS: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "text-embedding-004": (0.02, 0.0),
}


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = COSTS.get(model, (0.0, 0.0))
    return round(prompt_tokens / 1_000_000 * inp + completion_tokens / 1_000_000 * out, 6)

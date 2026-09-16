"""Heuristic hallucination/relevance evaluator per specs/04 §3 (offline, deterministic).

Production may swap in an LLM judge; CI gates use these scores:
faithfulness >= 0.85, RBAC leakage == 0. Relevance is 0..1 (×5 = judge scale).
"""
from __future__ import annotations

import re

from app.agent.prompts import NO_EVIDENCE

CITATION_RE = re.compile(r"\[doc:[^\]]+:\d+\]")
STOP = frozenset(
    "a an the and or of to in on for with is are was were be been do does did what "
    "which who whom how why when where i you we they it this that these those my your our".split()
)


def _words(t: str) -> list[str]:
    return [w.strip(".,;:!?()[]{}\"'").lower() for w in t.split() if w.strip(".,;:!?()[]{}\"'")]


def _content_words(t: str) -> set[str]:
    return {w for w in _words(t) if w and w not in STOP}


def evaluate(answer: str, chunks: list, query: str = "",
             allowed_doc_ids: set | None = None, forbidden_claims: tuple = ()) -> dict:
    """Return {faithfulness, relevance, leakage, details}."""
    for fc in forbidden_claims:
        if fc and fc.lower() in (answer or "").lower():
            return {"faithfulness": 0.0, "relevance": 0.0, "leakage": 1.0,
                    "details": {"forbidden_claim": fc}}
    if (answer or "").strip() == NO_EVIDENCE:
        return {"faithfulness": 1.0, "relevance": 1.0, "leakage": 1.0, "details": {"refusal": True}}
    if not answer or not answer.strip():
        return {"faithfulness": 0.0, "relevance": 0.0, "leakage": 1.0, "details": {"empty": True}}

    ctx_words: set[str] = set()
    for c in chunks:
        content = c.get("content") if isinstance(c, dict) else getattr(c, "content", "")
        ctx_words |= _content_words(content or "")

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if s.strip()]
    grounded = 0
    for s in sentences:
        if CITATION_RE.search(s):
            grounded += 1
            continue
        sw = _content_words(s)
        if sw and len(sw & ctx_words) / len(sw) >= 0.4:
            grounded += 1
    faithfulness = round(grounded / max(1, len(sentences)), 3)

    qw, aw = _content_words(query or ""), _content_words(answer)
    relevance = round(min(1.0, len(qw & aw) / max(1, len(qw)) * 1.5), 3) if qw else 1.0

    leakage = 1.0
    if allowed_doc_ids is not None and chunks:
        allowed_str = {str(d) for d in allowed_doc_ids}
        bad = sum(1 for c in chunks
                  if str(c.get("doc_id") if isinstance(c, dict) else getattr(c, "doc_id", None)) not in allowed_str)
        leakage = round(1.0 - bad / len(chunks), 3)

    return {"faithfulness": faithfulness, "relevance": relevance, "leakage": leakage,
            "details": {"sentences": len(sentences), "grounded": grounded}}

"""Guardrails node helpers per specs/03 §5: no-evidence refusal + PII redaction + citation normalization."""
from __future__ import annotations

import re

from app.agent.prompts import NO_EVIDENCE

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
MAX_CHARS = 4000


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = SSN_RE.sub("[redacted-ssn]", text)
    # Phone: only redact candidates with >= 8 digits (avoid citations like [doc:T:0])
    def _phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[redacted-phone]" if len(digits) >= 8 else m.group(0)

    return PHONE_RE.sub(_phone, text)


# ── citation normalization (canonical: [doc:Title:ord]) ──

# [doc:Title:12] optionally with timestamp decimals ([doc:T:00.50])
CANONICAL_WITH_DECIMAL_RE = re.compile(r"\[doc:([^\[\]:]+):(\d+)(?:\.\d+)?\]")
# Any other [doc:...] shape (e.g. [doc:Title:Ord] placeholder, missing ord)
MALFORMED_DOC_RE = re.compile(r"\[doc:[^\]]+\]")
CANONICAL_STRICT_RE = re.compile(r"\[doc:[^\[\]:]+:\d+\]")
# Legacy badge copy-paste: ⌗ Title:00.50 / ⌗ Title:0 (optional leading space/newline)
LEGACY_BADGE_RE = re.compile(r"(?:\s|^)⌗\s*([^:\n\[]+):(\d+(?:\.\d+)?)")
# Numeric-only citations like [1] (not Markdown links, not followed by '(')
NUMERIC_CITE_RE = re.compile(r"(?<![\w\]])\[(\d{1,3})\](?!\()")
FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?(?:```|$))")


def _sanitize_title(title: str) -> str:
    t = re.sub(r"[\[\]:\n\r]+", "-", str(title or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:120] or "untitled"


def _chunk_title_ord(c) -> tuple[str, int]:
    raw_title = c.title if hasattr(c, "title") else c.get("title", "") if isinstance(c, dict) else ""
    raw_ord = c.ord if hasattr(c, "ord") else c.get("ord", 0) if isinstance(c, dict) else 0
    try:
        ord_val = int(str(raw_ord).split(".")[0])
    except (ValueError, TypeError):
        ord_val = 0
    return _sanitize_title(raw_title), ord_val


def normalize_citations(answer: str, chunks: list) -> tuple[str, bool]:
    """Clean/convert legacy/malformed citation tags to canonical [doc:Title:ord].

    - Converts `⌗ Title:00.50` → `[doc:Title:ord]` when (Title, ord) is in chunks,
      else strips the `⌗` artifact (keeps bare Title text).
    - Normalizes `[doc:Title:00.50]` → `[doc:Title:0]` when valid.
    - Drops invented `[doc:...]` / `[1]` tags not present in chunks.
    - Never touches fenced code blocks.
    Returns (cleaned_text, did_change).
    """
    if not answer:
        return answer, False
    # Valid citations keyed by (sanitized_lower_title, ord) -> canonical title
    valid: dict[tuple[str, int], str] = {}
    titles_lower: set[str] = set()
    for c in chunks or []:
        t, o = _chunk_title_ord(c)
        valid[(t.lower(), o)] = t
        titles_lower.add(t.lower())

    changed = False

    def _normalize_part(part: str) -> str:
        nonlocal changed
        # 1. Legacy ⌗ badges (preserve leading whitespace, strip decimal timestamp)
        def _legacy(m: re.Match) -> str:
            nonlocal changed
            full = m.group(0)
            prefix = full[:1] if full[:1].isspace() else ""
            raw_title, raw_ord = m.group(1).strip(), m.group(2)
            title = _sanitize_title(raw_title)
            try:
                # e.g. :00.41 -> :0 (strip decimal timestamp portion)
                ord_val = int(str(raw_ord).split(".")[0])
            except (ValueError, TypeError):
                ord_val = 0
            key = (title.lower(), ord_val)
            if key in valid:
                changed = True
                return f"{prefix}[doc:{valid[key]}:{ord_val}]"
            # Unknown target: strip ⌗ artifact, keep readable title text
            changed = True
            return f"{prefix}{title}"

        part = LEGACY_BADGE_RE.sub(_legacy, part)

        # 2. Canonical-ish [doc:Title:ord] with optional decimals
        def _canonical(m: re.Match) -> str:
            nonlocal changed
            raw_title, raw_ord = m.group(1).strip(), m.group(2)
            title = _sanitize_title(raw_title)
            try:
                ord_val = int(str(raw_ord).split(".")[0])
            except (ValueError, TypeError):
                return m.group(0)
            key = (title.lower(), ord_val)
            full = m.group(0)
            if key in valid:
                canonical = f"[doc:{valid[key]}:{ord_val}]"
                if full != canonical:
                    changed = True
                return canonical
            # Invented title/ord: drop tag entirely
            changed = True
            return ""

        part = CANONICAL_WITH_DECIMAL_RE.sub(_canonical, part)

        # 3. Any remaining [doc:...] that is not strict-canonical (e.g. [doc:T:Ord]) → drop
        def _malformed(m: re.Match) -> str:
            nonlocal changed
            if CANONICAL_STRICT_RE.fullmatch(m.group(0)):
                # Strict shape but unknown target (missed above) → drop as invented
                key_title = m.group(0)
                # Re-parse to check validity
                inner = re.match(r"\[doc:([^\[\]:]+):(\d+)\]", key_title)
                if inner:
                    t = _sanitize_title(inner.group(1))
                    o = int(inner.group(2))
                    if (t.lower(), o) in valid:
                        return m.group(0)
                changed = True
                return ""
            changed = True
            return ""

        part = MALFORMED_DOC_RE.sub(_malformed, part)

        # 4. Numeric-only [1] citations (not links) → drop
        def _numeric(m: re.Match) -> str:
            nonlocal changed
            changed = True
            return ""

        part = NUMERIC_CITE_RE.sub(_numeric, part)
        return part

    fence_parts = FENCE_SPLIT_RE.split(answer)
    out: list[str] = []
    for fp in fence_parts:
        if not fp:
            continue
        if fp.startswith("```"):
            out.append(fp)
        else:
            out.append(_normalize_part(fp))
    cleaned = "".join(out)
    # Collapse excess blank lines left by dropped tags
    collapsed = re.sub(r"[ \t]+\n", "\n", cleaned)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    if collapsed != cleaned:
        changed = True
    return collapsed, changed


def apply_guardrails(answer: str, chunks: list) -> tuple[str, list[str]]:
    """Return (safe_answer, flags). Pure + unit-tested."""
    flags: list[str] = []
    if not chunks and answer.strip() != NO_EVIDENCE:
        return NO_EVIDENCE, ["no-evidence"]
    normalized, did_normalize = normalize_citations(answer, chunks)
    if did_normalize:
        flags.append("citations-normalized")
    safe = redact_pii(normalized)
    if safe != normalized:
        flags.append("pii-redacted")
    if len(safe) > MAX_CHARS:
        safe = safe[:MAX_CHARS] + "…"
        flags.append("truncated")
    return safe, flags

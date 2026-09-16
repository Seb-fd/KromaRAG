"""Semantic chunking per specs/03 §2: 600 tokens / 90 overlap, tiktoken cl100k.

Module contract: chunk_text(text) -> list[ChunkDraft]
- Recursive split on ["\\n## ", "\\n### ", "\\n\\n", ". ", "; "]
- Code fences (``` … ```) kept atomic unless block > target*1.5 (then split on newlines inside)
- Overlap carried as 90-token tail of previous chunk
- Metadata: ord, token_count, heading_path
"""
from __future__ import annotations

from dataclasses import dataclass

SEPARATORS = ["\n## ", "\n### ", "\n\n", ". ", "; "]

_enc = None


def _get_enc():
    global _enc
    if _enc is not None:
        return _enc
    try:
        import tiktoken

        _enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _enc = None
    return _enc


def count_tokens(text: str) -> int:
    enc = _get_enc()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback ~4 chars/token (only when tiktoken missing; tests install tiktoken)
    return max(1, len(text) // 4) if text else 0


def _encode(text: str) -> list[int]:
    enc = _get_enc()
    if enc is not None:
        return enc.encode(text)
    return [ord(c) for c in text.split()]


def _decode(tokens: list[int]) -> str:
    enc = _get_enc()
    if enc is not None:
        try:
            return enc.decode(tokens)
        except Exception:
            pass
    # Fallback only used without tiktoken; tokens are not real here
    return ""


@dataclass
class ChunkDraft:
    content: str
    ord: int
    token_count: int
    heading_path: str = ""


def _split_code_aware(text: str) -> list[tuple[str, bool]]:
    """Split into (segment, is_fence) preserving ``` delimiters in fence segments."""
    parts = text.split("```")
    out: list[tuple[str, bool]] = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            out.append(("```" + p + "```", True))
        elif p:
            out.append((p, False))
    return [s for s in out if s[0].strip()]


def _hard_split_long(text: str, target: int, is_fence: bool) -> list[str]:
    """Token-window split for a single oversized unit."""
    if is_fence and count_tokens(text) <= int(target * 1.5):
        return [text]
    if is_fence:
        # Split fence interior on newlines to keep markdown valid
        inner = text.strip()
        assert inner.startswith("```") and inner.endswith("```")
        first_nl = inner.find("\n")
        last_fence = inner.rfind("```")
        head = inner[: first_nl + 1] if first_nl != -1 else "```\n"
        body = inner[first_nl + 1 : last_fence] if first_nl != -1 else inner[3:-3]
        lines = body.split("\n")
        pieces, cur = [], ""
        for ln in lines:
            cand = cur + ("\n" if cur else "") + ln
            if count_tokens(head + cand + "\n```") > target and cur:
                pieces.append(head + cur + "\n```")
                cur = ln
            else:
                cur = cand
        if cur or not pieces:
            pieces.append(head + cur + "\n```")
        return pieces
    # Plain text: split on token windows at word boundaries
    words = text.split()
    pieces, cur = [], ""
    for w in words:
        cand = cur + (" " if cur else "") + w
        if count_tokens(cand) > target and cur:
            pieces.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        pieces.append(cur)
    return pieces or [text]


def _recursive_split(text: str, seps: list[str], target: int) -> list[tuple[str, bool]]:
    for seg, is_fence in _split_code_aware(text):
        # generator below handles each segment
        pass
    units: list[tuple[str, bool]] = []
    for seg, is_fence in _split_code_aware(text):
        if is_fence:
            if count_tokens(seg) > target:
                units.extend((p, True) for p in _hard_split_long(seg, target, True))
            else:
                units.append((seg, True))
            continue
        units.extend(_split_plain(seg, seps, target))
    return units


def _split_plain(text: str, seps: list[str], target: int) -> list[tuple[str, bool]]:
    if count_tokens(text) <= target or not seps:
        if count_tokens(text) <= target:
            return [(text, False)] if text.strip() else []
        return [(p, False) for p in _hard_split_long(text, target, False)]
    sep, rest = seps[0], seps[1:]
    if sep not in text:
        return _split_plain(text, rest, target)
    out: list[tuple[str, bool]] = []
    for part in text.split(sep):
        # re-attach separator except for first piece (keeps headings readable)
        piece = part if part.startswith(sep.strip()) else part
        if out:
            piece = sep.strip() + " " + piece if sep.strip() else piece
        piece = piece.strip()
        if not piece:
            continue
        if count_tokens(piece) > target:
            out.extend(_split_plain(piece, rest, target))
        else:
            out.append((piece, False))
    return out


def _heading_for(offset_text: str) -> str:
    """Last markdown heading path seen in text up to this point."""
    path: list[str] = []
    for line in offset_text.splitlines():
        s = line.strip()
        if s.startswith("### "):
            if len(path) >= 2:
                path[1] = s[4:].strip()
            else:
                path = (path[:1] + [s[4:].strip()])[:2]
        elif s.startswith("## "):
            path = [s[3:].strip()]
    return " / ".join(path)


def chunk_text(text: str, target: int = 600, overlap: int = 90, max_chunks: int = 2000) -> list[ChunkDraft]:
    """Chunk text into ~target-token pieces with overlap-token carry-over."""
    if not text or not text.strip():
        return []
    units = _recursive_split(text, SEPARATORS, target)
    chunks: list[ChunkDraft] = []
    cur_parts: list[str] = []
    cur_tokens = 0
    full_so_far = ""

    def flush() -> None:
        nonlocal cur_parts, cur_tokens, full_so_far
        if not cur_parts:
            return
        content = " ".join(cur_parts).strip() if not any("```" in p for p in cur_parts) else "\n\n".join(cur_parts).strip()
        full_so_far += ("\n\n" if full_so_far else "") + content
        n = count_tokens(content)
        chunks.append(ChunkDraft(content=content, ord=len(chunks), token_count=n, heading_path=_heading_for(full_so_far)))
        # Overlap: tail 90 tokens decoded back to text
        tail = _overlap_tail(content, overlap)
        cur_parts = [tail] if tail else []
        cur_tokens = count_tokens(tail) if tail else 0

    for unit, _is_fence in units:
        u_tokens = count_tokens(unit)
        if cur_tokens + u_tokens > target and cur_parts:
            flush()
        cur_parts.append(unit)
        cur_tokens += u_tokens
        if cur_tokens >= target:
            flush()
    flush()
    # Drop degenerate tail caused only by overlap echo (tiny last chunk duplicating tail)
    if len(chunks) >= 2 and count_tokens(chunks[-1].content) < overlap // 2:
        chunks.pop()
        for i, c in enumerate(chunks):
            c.ord = i
    if len(chunks) > max_chunks:
        raise ValueError(f"document exceeds {max_chunks} chunks ({len(chunks)})")
    return chunks


def _overlap_tail(content: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    toks = _encode(content)
    if len(toks) <= overlap:
        return ""
    tail_toks = toks[-overlap:]
    tail = _decode(tail_toks)
    if not tail:
        # tiktoken missing: word-based tail
        words = content.split()
        n = max(1, overlap // 2)
        tail = " ".join(words[-n:])
    return tail.strip()

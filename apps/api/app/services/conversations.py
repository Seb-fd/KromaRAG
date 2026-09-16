"""Conversations & history service (specs/06 §2.5, §4).

Pure helpers (auto_title, truncate_history, format_history) are framework-free
and unit-tested offline. Async DB helpers take an asyncpg connection/pool
acquired object exposing fetch/fetchrow/fetchval/execute — fakes in tests
implement the same surface.
"""
from __future__ import annotations

import math
import re
from typing import Any

DEFAULT_TITLE = "New conversation"

_SENT_SPLIT = re.compile(r"[.!?]\s+|\n")


def auto_title(query: str) -> str:
    """Deterministic server auto-title (specs/06 §4.3): first sentence → 60 chars.

    Sentence boundary is a delimiter followed by whitespace/newline (so
    decimals like 0.7 do not split). Trailing .!? on the first segment is
    stripped; overlong titles truncate to 60 chars + ….
    """
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return DEFAULT_TITLE
    first = _SENT_SPLIT.split(q, maxsplit=1)[0].strip() or q
    first = re.sub(r"\s+", " ", first).strip().rstrip(".!?")
    if not first:
        return DEFAULT_TITLE
    if len(first) > 60:
        return first[:60].rstrip() + "…"
    return first


def estimate_history_tokens(text: str) -> int:
    """Estimate prompt tokens for history caps. tiktoken cl100k when present."""
    t = text or ""
    if not t:
        return 0
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(t))
    except Exception:
        return math.ceil(len(t) / 4)


def truncate_history(
    messages: list[dict],
    max_turns: int = 10,
    max_chars: int = 6000,
    max_tokens: int = 1500,
) -> tuple[list[dict], int]:
    """Verbatim sliding window (specs/06 §4.4). Input oldest→newest.

    Steps: turn-count (last max_turns*2 rows) → char-cap (message boundary,
    oldest dropped first; single oversized message tail-truncated) →
    token-cap (oldest dropped first). Returns (kept, estimated_tokens).
    """
    msgs = [m for m in (messages or []) if m.get("role") in ("user", "assistant")]
    if max_turns is not None:
        msgs = msgs[max(0, len(msgs) - max_turns * 2):]
    # Char cap at message boundary (newest-first accumulation).
    if max_chars is not None:
        kept_rev: list[dict] = []
        total = 0
        for m in reversed(msgs):
            c = len(m.get("content") or "")
            if kept_rev and total + c > max_chars:
                continue  # drop this older message entirely
            if not kept_rev and c > max_chars:
                cut = dict(m)
                cut["content"] = (m.get("content") or "")[: max(0, max_chars - len("… [truncated]"))] + "… [truncated]"
                kept_rev.append(cut)
                total = max_chars
                break
            kept_rev.append(m)
            total += c
        msgs = list(reversed(kept_rev))
    # Token cap (oldest dropped first).
    def _tokens(ms: list[dict]) -> int:
        return estimate_history_tokens("\n".join(f"{m.get('role')}: {m.get('content')}" for m in ms))

    tokens = _tokens(msgs)
    if max_tokens is not None:
        while len(msgs) > 1 and tokens > max_tokens:
            msgs = msgs[1:]
            tokens = _tokens(msgs)
        if len(msgs) == 1 and tokens > max_tokens:
            # Single remaining message still over budget → tail-truncate content.
            # Binary-shrink chars until estimate fits (chars/4 fallback is linear).
            m = dict(msgs[0])
            content = m.get("content") or ""
            lo, hi = 0, len(content)
            suffix = "… [truncated]"
            while lo < hi:
                mid = (lo + hi + 1) // 2
                cand = content[:mid] + suffix
                if estimate_history_tokens(f"{m.get('role')}: {cand}") <= max_tokens:
                    lo = mid
                else:
                    hi = mid - 1
            m["content"] = content[:lo] + suffix
            msgs = [m]
            tokens = _tokens(msgs)
    return msgs, tokens


def format_history_block(history: list[dict]) -> str:
    """Prompt block for node_generator. Empty history → empty string (no header)."""
    if not history:
        return ""
    lines = ["## Conversation history (verbatim, oldest first, may be truncated):"]
    for m in history:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content') or ''}")
    lines.append(
        "Note: history is context only. Answer ONLY from the current accessible "
        "chunks; never cite history as [doc:...]. If history conflicts with chunks, chunks win."
    )
    return "\n".join(lines)


# ── Async DB helpers (asyncpg surface) ──

async def check_owner(conn: Any, conv_id: str, user_sub: str) -> Any | None:
    try:
        return await conn.fetchrow(
            "SELECT id FROM kroma_conversations WHERE id=$1 AND user_sub=$2",
            conv_id, user_sub,
        )
    except Exception:
        return None


async def ensure_thread(conn: Any, conv_id: str, user_sub: str) -> None:
    await conn.execute(
        "INSERT INTO kroma_conversations(id, user_sub, title) VALUES ($1,$2,$3)"
        " ON CONFLICT (id) DO NOTHING",
        conv_id, user_sub, DEFAULT_TITLE,
    )


async def load_history_rows(conn: Any, conv_id: str, limit: int) -> list[dict]:
    try:
        rows = await conn.fetch(
            "SELECT role, content FROM kroma_messages WHERE conv_id=$1"
            " ORDER BY created_at DESC, id DESC LIMIT $2",
            conv_id, limit,
        )
    except Exception:
        return []
    out = [{"role": r["role"], "content": r["content"]} for r in (rows or [])]
    out.reverse()  # DESC fetch → oldest→newest for truncation/prompt
    return out


async def persist_pair(
    conn: Any,
    conv_id: str,
    user_sub: str,
    user_content: str,
    assistant_content: str,
    trace_id: str,
    usage: dict | None = None,
    route: str = "retrieval",
) -> None:
    """Atomic (user, assistant) write + updated_at + first-turn auto-title.

    FK-safe: if the thread was hard-deleted mid-stream, skip silently.
    Idempotent: duplicate trace_id never creates a second assistant row.
    """
    usage = usage or {}
    try:
        pre_count = await conn.fetchval(
            "SELECT COUNT(*) FROM kroma_messages WHERE conv_id=$1", conv_id
        )
    except Exception:
        pre_count = 0
    # Use a transaction when the driver supports it (asyncpg); fakes skip it.
    tx = None
    try:
        if hasattr(conn, "transaction"):
            tx = conn.transaction()
            await tx.__aenter__()
    except Exception:
        tx = None
    try:
        try:
            await conn.execute(
                "INSERT INTO kroma_messages(conv_id, role, content) VALUES ($1,'user',$2)",
                conv_id, (user_content or "").strip(),
            )
        except Exception:
            # FK violation (thread deleted mid-stream) → skip whole pair.
            return
        try:
            await conn.execute(
                "INSERT INTO kroma_messages(conv_id, role, content, trace_id,"
                " prompt_tokens, completion_tokens, model, route)"
                " VALUES ($1,'assistant',$2,$3,$4,$5,$6,$7)"
                " ON CONFLICT (trace_id) WHERE trace_id IS NOT NULL DO NOTHING",
                conv_id, assistant_content or "",
                trace_id,
                int((usage or {}).get("prompt_tokens", 0)),
                int((usage or {}).get("completion_tokens", 0)),
                str((usage or {}).get("model", "") or ""),
                route or "retrieval",
            )
        except Exception:
            # Fallback for drivers without partial-index ON CONFLICT support:
            # check-then-insert (best-effort idempotency).
            try:
                existing = await conn.fetchrow(
                    "SELECT id FROM kroma_messages WHERE trace_id=$1", trace_id
                )
            except Exception:
                existing = None
            if not existing:
                try:
                    await conn.execute(
                        "INSERT INTO kroma_messages(conv_id, role, content, trace_id,"
                        " prompt_tokens, completion_tokens, model, route)"
                        " VALUES ($1,'assistant',$2,$3,$4,$5,$6,$7)",
                        conv_id, assistant_content or "", trace_id,
                        int((usage or {}).get("prompt_tokens", 0)),
                        int((usage or {}).get("completion_tokens", 0)),
                        str((usage or {}).get("model", "") or ""),
                        route or "retrieval",
                    )
                except Exception:
                    pass
        try:
            if (pre_count or 0) == 0:
                row = await conn.fetchrow(
                    "SELECT title FROM kroma_conversations WHERE id=$1 AND user_sub=$2",
                    conv_id, user_sub,
                )
                cur = (dict(row).get("title") if row else None) if row else None
                if not cur or cur == DEFAULT_TITLE:
                    await conn.execute(
                        "UPDATE kroma_conversations SET title=$3, updated_at=now()"
                        " WHERE id=$1 AND user_sub=$2",
                        conv_id, user_sub, auto_title(user_content),
                    )
                else:
                    await conn.execute(
                        "UPDATE kroma_conversations SET updated_at=now()"
                        " WHERE id=$1 AND user_sub=$2",
                        conv_id, user_sub,
                    )
            else:
                await conn.execute(
                    "UPDATE kroma_conversations SET updated_at=now()"
                    " WHERE id=$1 AND user_sub=$2",
                    conv_id, user_sub,
                )
        except Exception:
            pass
    finally:
        try:
            if tx is not None:
                await tx.__aexit__(None, None, None)
        except Exception:
            pass

"""Conversations & history CRUD — leak-safe thread RBAC (specs/06 §3).

All single-thread reads/writes scope by (id, user_sub); miss → uniform 404
NOT_FOUND (never 403) so existence is not leaked across tenants.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.security import require_role
from app.schemas import (
    ConversationCreate,
    ConversationRename,
    Principal,
)
from app.services.conversations import DEFAULT_TITLE

router = APIRouter(tags=["conversations"])

NOT_FOUND = {"code": "NOT_FOUND", "message": "conversation not found"}

LIST_SQL = (
    "SELECT c.id, c.title, c.created_at, c.updated_at,"
    " (SELECT COUNT(*) FROM kroma_messages m WHERE m.conv_id=c.id)::int AS message_count"
    " FROM kroma_conversations c WHERE c.user_sub=$1"
    " ORDER BY c.updated_at DESC LIMIT $2 OFFSET $3"
)
LIST_COUNT_SQL = "SELECT COUNT(*) FROM kroma_conversations WHERE user_sub=$1"
GET_SQL = (
    "SELECT c.id, c.title, c.created_at, c.updated_at,"
    " (SELECT COUNT(*) FROM kroma_messages m WHERE m.conv_id=c.id)::int AS message_count"
    " FROM kroma_conversations c WHERE c.id=$1 AND c.user_sub=$2"
)
CREATE_SQL = (
    "INSERT INTO kroma_conversations(user_sub, title) VALUES ($1,$2)"
    " RETURNING id, title, created_at, updated_at"
)
RENAME_SQL = (
    "UPDATE kroma_conversations SET title=$3, updated_at=now()"
    " WHERE id=$1 AND user_sub=$2 RETURNING id, title, created_at, updated_at"
)
DELETE_SQL = "DELETE FROM kroma_conversations WHERE id=$1 AND user_sub=$2 RETURNING id"
OWNER_SQL = "SELECT id FROM kroma_conversations WHERE id=$1 AND user_sub=$2"
ANCHOR_SQL = "SELECT created_at, id FROM kroma_messages WHERE id=$1 AND conv_id=$2"
MSGS_SQL = (
    "SELECT id, role, content, trace_id, created_at FROM kroma_messages"
    " WHERE conv_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2"
)
MSGS_BEFORE_SQL = (
    "SELECT id, role, content, trace_id, created_at FROM kroma_messages"
    " WHERE conv_id=$1 AND (created_at, id) < ($2, $3)"
    " ORDER BY created_at DESC, id DESC LIMIT $4"
)


def _pool():
    from app.services.db import get_pool

    return get_pool()


def _404() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)


def _row_to_meta(row: dict, message_count: int = 0) -> dict:
    d = dict(row)
    return {
        "id": str(d["id"]),
        "title": d.get("title") or DEFAULT_TITLE,
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "message_count": int(d.get("message_count", message_count) or 0),
    }


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            {"code": "DB_UNAVAILABLE", "message": "no db"})
    title = (body.title or "").strip() or DEFAULT_TITLE
    async with pool.acquire() as conn:
        row = await conn.fetchrow(CREATE_SQL, p.sub, title[:120])
    if not row:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            {"code": "DB_UNAVAILABLE", "message": "create failed"})
    meta = _row_to_meta(row, 0)
    return meta


@router.get("/conversations")
async def list_conversations(limit: int = 20, offset: int = 0,
                             p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        return {"items": [], "total": 0}
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    async with pool.acquire() as conn:
        rows = await conn.fetch(LIST_SQL, p.sub, limit, offset)
        total = await conn.fetchval(LIST_COUNT_SQL, p.sub)
    return {"items": [_row_to_meta(r) for r in (rows or [])], "total": int(total or 0)}


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(GET_SQL, conv_id, p.sub)
        except Exception:
            raise _404()
    if not row:
        raise _404()
    return _row_to_meta(row)


@router.patch("/conversations/{conv_id}")
async def rename_conversation(conv_id: str, body: ConversationRename,
                              p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(RENAME_SQL, conv_id, p.sub, body.title.strip()[:120])
        except Exception:
            raise _404()
        if not row:
            raise _404()
        try:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM kroma_messages WHERE conv_id=$1", conv_id)
        except Exception:
            count = 0
    meta = _row_to_meta(row, int(count or 0))
    return meta


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DELETE_SQL, conv_id, p.sub)
        except Exception:
            raise _404()
    if not row:
        raise _404()
    return Response(status_code=204)


@router.get("/conversations/{conv_id}/messages")
async def list_messages(conv_id: str, limit: int = 50, before_id: str | None = None,
                        p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    limit = max(1, min(int(limit or 50), 100))
    async with pool.acquire() as conn:
        try:
            owner = await conn.fetchrow(OWNER_SQL, conv_id, p.sub)
        except Exception:
            raise _404()
        if not owner:
            raise _404()
        anchor = None
        if before_id:
            try:
                anchor = await conn.fetchrow(ANCHOR_SQL, before_id, conv_id)
            except Exception:
                raise _404()
            if not anchor:
                raise _404()
        try:
            if anchor:
                a = dict(anchor)
                rows = await conn.fetch(MSGS_BEFORE_SQL, conv_id, a["created_at"], str(a["id"]), limit)
            else:
                rows = await conn.fetch(MSGS_SQL, conv_id, limit)
        except HTTPException:
            raise
        except Exception:
            raise _404()
    items_desc = [dict(r) for r in (rows or [])]
    items_asc = list(reversed(items_desc))
    items = [{
        "id": str(r["id"]), "role": r["role"], "content": r["content"],
        "trace_id": r.get("trace_id"), "created_at": r.get("created_at"),
    } for r in items_asc]
    next_before = str(items_asc[0]["id"]) if len(items_asc) == limit else None
    # If the page is full but we are already at the oldest, collapse cursor.
    # (Cheap heuristic; full check would need an extra COUNT. Client treats
    # empty next page as terminal as well.)
    return {"items": items, "next_before_id": next_before}

"""Documents lifecycle — Spec 07 (specs/07_document_lifecycle_mgmt_spec.md §3).

Routes (router prefix /api/v1 applied in main.py):
  POST /documents/upload       editor+  multipart file → 202 {doc_id,status,chunks,job_id}
  GET  /documents              viewer+  RBAC-filtered list + status/mime/q filters
  GET  /documents/{id}         visible only → DocumentDetail / uniform 404
  GET  /documents/{id}/status  visible only → DocumentStatus / uniform 404
  PATCH /documents/{id}        owner+editor or admin → DocumentDetail / uniform 404
  DELETE /documents/{id}       owner+editor or admin → 204 soft-archive (ARCHIVED)
  POST /documents/{id}/reindex owner+editor or admin → 202 async job {job_id,version}
  POST /documents/{id}/purge   owner or admin → 204 hard-delete (CASCADE)

Leak-safe rule (normative): any single-doc op on a non-visible doc returns
404 {"code":"NOT_FOUND","message":"not found or forbidden"} — byte-identical to a
random nonexistent UUID, never 403 for existence. 403 is reserved for role gates
(e.g. viewer calling the editor+ upload route).
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from app.core.security import require_role
from app.schemas import Principal
from app.services import ingest as ingest_svc

log = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

NOT_FOUND = {"code": "NOT_FOUND", "message": "not found or forbidden"}

_HIERARCHY = {"viewer": 1, "editor": 2, "admin": 3}
HTTP_422 = status.HTTP_422_UNPROCESSABLE_CONTENT
HTTP_413 = status.HTTP_413_CONTENT_TOO_LARGE
DOC_STATUSES = ("pending", "indexing", "indexed", "failed", "archived")

# ── SQL (single source; fakes in tests match on these distinctive fragments) ──

VISIBILITY_FRAG = (
    "(d.owner_sub=$2 OR 'admin' = ANY($3) OR EXISTS ("
    "SELECT 1 FROM kroma_document_roles r WHERE r.doc_id=d.id AND r.role = ANY($3)))"
)

LIST_VISIBILITY_FRAG = (
    "(d.owner_sub=$1 OR 'admin' = ANY($2) OR EXISTS ("
    "SELECT 1 FROM kroma_document_roles r WHERE r.doc_id=d.id AND r.role = ANY($2)))"
)

LIST_SQL = (
    "SELECT d.id, d.owner_sub, d.title, d.mime, d.uri, d.status, d.error,"
    " d.version, d.chunk_config, d.embedding_model, d.embedding_dim,"
    " d.created_at, d.updated_at, d.archived_at,"
    " COALESCE(c.chunks,0)::int AS chunks, COALESCE(r.roles,'{}') AS allowed_roles"
    " FROM kroma_documents d"
    " LEFT JOIN (SELECT doc_id, COUNT(*) AS chunks FROM kroma_chunks GROUP BY doc_id) c ON c.doc_id=d.id"
    " LEFT JOIN (SELECT doc_id, array_agg(role) AS roles FROM kroma_document_roles GROUP BY doc_id) r ON r.doc_id=d.id"
    " WHERE " + LIST_VISIBILITY_FRAG +
    " ORDER BY d.updated_at DESC LIMIT $3 OFFSET $4"
)
LIST_COUNT_SQL = (
    "SELECT COUNT(*) FROM kroma_documents d WHERE " + LIST_VISIBILITY_FRAG
)

DETAIL_SQL = (
    "SELECT d.id, d.owner_sub, d.title, d.mime, d.uri, d.status, d.error,"
    " d.version, d.chunk_config, d.embedding_model, d.embedding_dim,"
    " d.created_at, d.updated_at, d.archived_at,"
    " COALESCE(c.chunks,0)::int AS chunks, COALESCE(r.roles,'{}') AS allowed_roles"
    " FROM kroma_documents d"
    " LEFT JOIN (SELECT doc_id, COUNT(*) AS chunks FROM kroma_chunks GROUP BY doc_id) c ON c.doc_id=d.id"
    " LEFT JOIN (SELECT doc_id, array_agg(role) AS roles FROM kroma_document_roles GROUP BY doc_id) r ON r.doc_id=d.id"
    " WHERE d.id=$1 AND " + VISIBILITY_FRAG
)

STATUS_SQL = (
    "SELECT d.id, d.status, d.version, d.error, d.updated_at,"
    " COALESCE(c.chunks,0)::int AS chunks"
    " FROM kroma_documents d"
    " LEFT JOIN (SELECT doc_id, COUNT(*) AS chunks FROM kroma_chunks GROUP BY doc_id) c ON c.doc_id=d.id"
    " WHERE d.id=$1 AND " + VISIBILITY_FRAG
)

PATCH_DOC_SQL = (
    "UPDATE kroma_documents SET title=COALESCE($2,title), uri=COALESCE($3,uri),"
    " owner_sub=COALESCE($4,owner_sub), updated_at=now() WHERE id=$1"
)
PATCH_ROLES_DELETE_SQL = "DELETE FROM kroma_document_roles WHERE doc_id=$1"
PATCH_ROLES_INSERT_SQL = (
    "INSERT INTO kroma_document_roles(doc_id, role) VALUES($1,$2) ON CONFLICT DO NOTHING"
)

ARCHIVE_SQL = (
    "UPDATE kroma_documents SET status='archived', archived_at=now(), updated_at=now() WHERE id=$1"
)

PURGE_ROLES_SQL = "DELETE FROM kroma_document_roles WHERE doc_id=$1"
PURGE_CHUNKS_SQL = "DELETE FROM kroma_chunks WHERE doc_id=$1"
PURGE_DOC_SQL = "DELETE FROM kroma_documents WHERE id=$1"
VERIFY_ORPHANS_SQL = "SELECT COUNT(*) FROM kroma_chunks WHERE doc_id=$1"


def _pool():
    from app.services.db import get_pool

    return get_pool()


def _404() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)


def _level(roles: list[str]) -> int:
    return max((_HIERARCHY.get(r, 0) for r in (roles or [])), default=0)


def _is_admin(p: Principal) -> bool:
    return "admin" in (p.roles or [])


def _can_write(row: dict, p: Principal) -> bool:
    """Owner-with-editor+ or admin (specs/07 §4.6)."""
    if _is_admin(p):
        return True
    return row.get("owner_sub") == p.sub and _level(list(p.roles or [])) >= _HIERARCHY["editor"]


def _can_purge(row: dict, p: Principal) -> bool:
    """Owner (any role) or admin (specs/07 §4.6)."""
    if _is_admin(p):
        return True
    return row.get("owner_sub") == p.sub


def _parse_chunk_config(raw) -> dict:
    import json as _json

    try:
        if isinstance(raw, str):
            return _json.loads(raw) if raw else {}
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"chunk_tokens": 600, "chunk_overlap": 90}


def _row_to_detail(row: dict) -> dict:
    d = dict(row)
    status_val = ingest_svc.normalize_status(str(d.get("status") or "indexed"))
    roles = d.get("allowed_roles") or []
    if isinstance(roles, str):
        try:
            roles = json.loads(roles)
        except Exception:
            roles = [roles] if roles else []
    err = d.get("error")
    return {
        "id": str(d["id"]),
        "title": d.get("title") or "",
        "mime": d.get("mime") or "text/plain",
        "status": status_val,
        "chunks": int(d.get("chunks") or 0),
        "version": int(d.get("version") or 1),
        "owner_sub": d.get("owner_sub") or "",
        "uri": d.get("uri"),
        "allowed_roles": list(roles or []),
        "chunk_config": _parse_chunk_config(d.get("chunk_config")),
        "embedding_model": d.get("embedding_model") or ingest_svc.PINNED_EMBEDDING_MODEL,
        "embedding_dim": int(d.get("embedding_dim") or ingest_svc.PINNED_EMBEDDING_DIM),
        "error": (str(err)[:500] if err else None),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "archived_at": d.get("archived_at"),
    }


def _row_to_status(row: dict) -> dict:
    d = dict(row)
    err = d.get("error")
    return {
        "doc_id": str(d.get("id") or d.get("doc_id")),
        "status": ingest_svc.normalize_status(str(d.get("status") or "")),
        "chunks": int(d.get("chunks") or 0),
        "version": int(d.get("version") or 1),
        "error": (str(err)[:500] if err else None),
        "updated_at": d.get("updated_at"),
    }


def _parse_allowed_roles_form(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return ["viewer"]
    s = raw.strip()
    try:
        roles = json.loads(s)
        if isinstance(roles, list):
            return ingest_svc.validate_roles([str(r) for r in roles])
    except Exception:
        pass
    # Fallback: comma-separated "viewer,editor".
    return ingest_svc.validate_roles([r.strip() for r in s.split(",") if r.strip()])


async def _reindex_worker(reservation: dict) -> None:
    """BackgroundTasks worker: runs the reserved reindex with default embedder."""
    pool = _pool()
    if pool is None:
        log.error("reindex worker: no db pool for doc %s", reservation.get("doc_id"))
        try:
            from app.services import metrics as _m

            _m.inc_reindex("failed")
        except Exception:
            pass
        return
    try:
        async with pool.acquire() as conn:
            result = await ingest_svc.run_reindex_job(reservation, conn)
        try:
            from app.services import metrics as _m

            _m.inc_reindex("ok" if (result or {}).get("status") == "indexed" else "failed")
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001 — job row already marked failed; just log
        log.error("reindex worker failed for doc %s: %s", reservation.get("doc_id"), e)
        try:
            from app.services import metrics as _m

            _m.inc_reindex("failed")
        except Exception:
            pass


# ── Upload (multipart, editor+) ──

@router.post("/documents/upload", status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    allowed_roles: str | None = Form(None),
    chunk_tokens: int | None = Form(None),
    chunk_overlap: int | None = Form(None),
    p: Principal = Depends(require_role("editor")),
):
    from app.core.config import settings

    pool = _pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            {"code": "DB_UNAVAILABLE", "message": "no db"})
    try:
        roles = _parse_allowed_roles_form(allowed_roles)
    except ValueError as e:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": str(e)})
    target = int(chunk_tokens or settings.chunk_tokens)
    overlap = int(chunk_overlap if chunk_overlap is not None else settings.chunk_overlap)
    if target < 100 or target > 2000 or overlap < 0 or overlap > 500 or overlap >= target:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": "invalid chunk_tokens/overlap"})
    try:
        data = await file.read()
    except Exception as e:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": f"unreadable upload: {e}"})
    if len(data or b"") > int(settings.doc_upload_max_bytes):
        raise HTTPException(HTTP_413,
                            {"code": "PAYLOAD_TOO_LARGE",
                             "message": f"file exceeds {settings.doc_upload_max_bytes} byte cap"})
    if not data:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": "empty upload"})
    job_id = str(uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            result = await ingest_svc.ingest_upload(
                data, filename=file.filename, title=(title or "").strip() or None,
                content_type=file.content_type, owner_sub=p.sub, allowed_roles=roles,
                db=conn, target=target, overlap=overlap,
            )
    except ValueError as e:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": str(e)})
    except RuntimeError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            {"code": "DB_UNAVAILABLE", "message": str(e)[:500]})
    # Phase 1: upload embeds inline (small files); job_id returned for status-poll correlation.
    return {"doc_id": result["doc_id"], "status": result["status"],
            "chunks": result["chunks"], "job_id": job_id}


# ── List (viewer+, RBAC-filtered + filters) ──

@router.get("/documents")
async def list_documents(limit: int = 20, offset: int = 0,
                         status_filter: str | None = Query(None, alias="status"),
                         mime: str | None = None,
                         q: str | None = None,
                         p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        return {"items": [], "total": 0}
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    sf = (status_filter or "").strip().lower() or None
    if sf is not None and sf != "all" and sf not in DOC_STATUSES and sf != "ready":
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": f"unknown status {status_filter!r}"})
    if sf == "ready":
        sf = "indexed"  # legacy alias
    mime_f = (mime or "").strip() or None
    q_f = (q or "").strip() or None
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(LIST_SQL, p.sub, list(p.roles or []), limit, offset)
        except Exception as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                {"code": "DB_UNAVAILABLE", "message": f"db error: {str(e)[:200]}"})
        try:
            total_all = await conn.fetchval(LIST_COUNT_SQL, p.sub, list(p.roles or []))
        except Exception:
            total_all = len(rows or [])
    items = []
    for r in (rows or []):
        try:
            items.append(_row_to_detail(r))
        except Exception:
            continue
    # Python-side filters (single source of filter semantics; SQL carries RBAC).
    # Real-DB path could push these into SQL later without contract change.
    if sf is not None and sf != "all":
        items = [d for d in items if d["status"] == sf]
    elif sf is None:
        items = [d for d in items if d["status"] != "archived"]
    if mime_f:
        items = [d for d in items if d["mime"] == mime_f]
    if q_f:
        needle = q_f.lower()
        items = [d for d in items if needle in (d["title"] or "").lower()]
    return {"items": items, "total": len(items) if (sf or mime_f or q_f) else int(total_all or 0)}


# ── Get one / status (visible only, leak-safe 404) ──

@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
    if not row:
        raise _404()
    return _row_to_detail(row)


@router.get("/documents/{doc_id}/status")
async def get_document_status(doc_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(STATUS_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
    if not row:
        raise _404()
    return _row_to_status(row)


# ── Patch (owner+editor or admin) ──

@router.patch("/documents/{doc_id}")
async def patch_document(doc_id: str, body: dict,
                         p: Principal = Depends(require_role("viewer"))):
    from app.schemas import DocumentUpdate

    try:
        update = DocumentUpdate(**(body or {}))
    except Exception as e:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": str(e)[:500]})
    if not any([update.title, update.allowed_roles is not None,
                update.owner_sub, update.uri]):
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": "no actionable fields"})
    if update.owner_sub is not None and not _is_admin(p):
        raise _404()  # leak-safe: non-admin ownership probe looks like missing doc
    if update.allowed_roles is not None:
        try:
            ingest_svc.validate_roles(list(update.allowed_roles))
        except ValueError as e:
            raise HTTPException(HTTP_422,
                                {"code": "VALIDATION", "message": str(e)})
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
        if not row:
            raise _404()
        current = _row_to_detail(row)
        if not _can_write(current, p):
            raise _404()
        title = update.title.strip()[:300] if update.title else None
        if title is not None and not title:
            raise HTTPException(HTTP_422,
                                {"code": "VALIDATION", "message": "title must not be blank"})
        try:
            await conn.execute(PATCH_DOC_SQL, doc_id, title, update.uri, update.owner_sub)
            if update.allowed_roles is not None:
                await conn.execute(PATCH_ROLES_DELETE_SQL, doc_id)
                for r in dict.fromkeys(update.allowed_roles):  # dedupe, preserve order
                    await conn.execute(PATCH_ROLES_INSERT_SQL, doc_id, r)
            # PATCH never bumps version/status (§4.2 rule 1); touch updated_at only.
            await conn.execute("UPDATE kroma_documents SET updated_at=now() WHERE id=$1", doc_id)
            fresh = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                {"code": "DB_UNAVAILABLE", "message": str(e)[:300]})
    # Admin transfer may move the doc out of the caller's own visibility; re-read as admin.
    if fresh is None:
        if _is_admin(p):
            pool2 = _pool()
            async with pool2.acquire() as conn2:
                fresh = await conn2.fetchrow(
                    DETAIL_SQL, doc_id, p.sub, list(p.roles or []) + ["admin"])
        if fresh is None:
            raise _404()
    return _row_to_detail(fresh)


# ── Archive = soft-delete (owner+editor or admin) ──

@router.delete("/documents/{doc_id}", status_code=204)
async def archive_document(doc_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
        if not row:
            raise _404()
        current = _row_to_detail(row)
        if not _can_write(current, p):
            raise _404()
        if current["status"] == "archived":
            return Response(status_code=204)  # idempotent no-op
        try:
            await conn.execute(ARCHIVE_SQL, doc_id)
        except Exception as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                {"code": "DB_UNAVAILABLE", "message": str(e)[:300]})
    return Response(status_code=204)


# ── Reindex (owner+editor or admin → 202 async job) ──

@router.post("/documents/{doc_id}/reindex", status_code=202)
async def reindex_document(doc_id: str, body: dict | None = None,
                           background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
                           p: Principal = Depends(require_role("viewer"))):
    from app.core.config import settings
    from app.schemas import ReindexRequest

    try:
        req = ReindexRequest(**(body or {}))
    except Exception as e:
        raise HTTPException(HTTP_422,
                            {"code": "VALIDATION", "message": str(e)})
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
        if not row:
            raise _404()
        current = _row_to_detail(row)
        if not _can_write(current, p):
            raise _404()
        try:
            reservation = await ingest_svc.reserve_reindex(
                doc_id, conn, chunk_tokens=req.chunk_tokens,
                chunk_overlap=req.chunk_overlap, embedding_model=req.embedding_model)
        except ingest_svc.DocNotFoundError:
            raise _404()
        except ingest_svc.DocConflictError as e:
            try:
                from app.services import metrics as _m

                _m.inc_reindex("conflict")
            except Exception:
                pass
            raise HTTPException(status.HTTP_409_CONFLICT,
                                {"code": str(e.code), "message": str(e)})
        except ValueError as e:
            msg = str(e)
            code = "EMBEDDING_MISMATCH" if "EMBEDDING_MISMATCH" in msg else "VALIDATION"
            raise HTTPException(HTTP_422,
                                {"code": code, "message": msg})
        if not settings.doc_reindex_async:
            # Synchronous inline (tests): same worker, same transaction semantics.
            try:
                result = await ingest_svc.run_reindex_job(reservation, conn)
            except ValueError as e:
                try:
                    from app.services import metrics as _m

                    _m.inc_reindex("failed")
                except Exception:
                    pass
                raise HTTPException(HTTP_422,
                                    {"code": "VALIDATION", "message": str(e)})
            except Exception as e:
                try:
                    from app.services import metrics as _m

                    _m.inc_reindex("failed")
                except Exception:
                    pass
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                    {"code": "DB_UNAVAILABLE", "message": str(e)[:300]})
            try:
                from app.services import metrics as _m

                _m.inc_reindex("ok" if result.get("status") == "indexed" else "failed")
            except Exception:
                pass
            return {"doc_id": str(doc_id), "job_id": reservation["job_id"],
                    "status": result["status"], "version": result["version"]}
    # Async: reservation committed; worker runs after response.
    if background_tasks is not None:
        background_tasks.add_task(_reindex_worker, reservation)
    else:  # pragma: no cover — FastAPI always injects BackgroundTasks
        await _reindex_worker(reservation)
    return {"doc_id": str(doc_id), "job_id": reservation["job_id"],
            "status": "indexing", "version": reservation["version"]}


# ── Purge = hard-delete (owner or admin, irreversible) ──

@router.post("/documents/{doc_id}/purge", status_code=204)
async def purge_document(doc_id: str, p: Principal = Depends(require_role("viewer"))):
    pool = _pool()
    if pool is None:
        raise _404()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(DETAIL_SQL, doc_id, p.sub, list(p.roles or []))
        except Exception:
            raise _404()
        if not row:
            raise _404()
        current = _row_to_detail(row)
        if not _can_purge(current, p):
            raise _404()
        if current["status"] in ("pending", "indexing"):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                {"code": "ARCHIVE_CONFLICT",
                                 "message": f"doc {doc_id} is {current['status']}; retry after terminal state"})
        tx = None
        try:
            if hasattr(conn, "transaction"):
                tx = conn.transaction()
                await tx.__aenter__()
        except Exception:
            tx = None
        try:
            try:
                await conn.execute(PURGE_ROLES_SQL, doc_id)
                await conn.execute(PURGE_CHUNKS_SQL, doc_id)
                await conn.execute(PURGE_DOC_SQL, doc_id)
            except Exception as e:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                    {"code": "DB_UNAVAILABLE", "message": str(e)[:300]})
            try:
                orphans = await conn.fetchval(VERIFY_ORPHANS_SQL, doc_id)
                if int(orphans or 0) != 0:
                    log.error("purge orphan invariant violated for doc %s: %s", doc_id, orphans)
            except Exception:
                pass
        finally:
            try:
                if tx is not None:
                    await tx.__aexit__(None, None, None)
            except Exception:
                pass
    try:
        from app.services import metrics as _m

        _m.inc_purge()
    except Exception:
        pass
    return Response(status_code=204)

"""Ingest endpoint — Phase 2 wired to chunk+embed+DB (specs/03 §1, specs/02 §1)."""
from fastapi import APIRouter, Depends, HTTPException
from app.core.security import require_role
from app.schemas import IngestRequest, Principal

router = APIRouter(tags=["ingest"])


@router.post("/ingest", status_code=202)
async def ingest(body: IngestRequest, p: Principal = Depends(require_role("editor"))):
    from app.core.config import settings
    from app.services.db import get_pool
    from app.services.ingest import ingest_document, plan_ingest

    pool = get_pool()
    if pool is None:
        # No DB (local scaffold): validate + chunk only, so contract still verifiable
        chunks, _ = await plan_ingest(body.title, body.content or "", body.mime,
                                      target=settings.chunk_tokens, overlap=settings.chunk_overlap)
        return {"doc_id": "00000000-0000-0000-0000-000000000000", "status": "pending", "chunks": len(chunks)}
    try:
        return await ingest_document(
            title=body.title, content=body.content, url=body.url, mime=body.mime,
            owner_sub=p.sub, allowed_roles=body.allowed_roles, db=pool,
            target=settings.chunk_tokens, overlap=settings.chunk_overlap,
        )
    except ValueError as e:
        raise HTTPException(422, {"code": "VALIDATION", "message": str(e)})
    except RuntimeError as e:  # e.g. missing GOOGLE_API_KEY
        raise HTTPException(503, {"code": "DB_UNAVAILABLE", "message": str(e)})

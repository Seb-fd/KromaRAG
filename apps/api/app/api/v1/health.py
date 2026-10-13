"""System endpoints: liveness + readiness (specs/02 §1, specs/08 §5.1). No auth.

/health + /healthz → liveness (no DB). /ready + /readyz → readiness (SELECT 1).
Aliases are identical handlers (non-breaking, specs/08 §3.4).
"""
from fastapi import APIRouter

from app.core.config import settings
from app.services.db import db_ping, pool_stats

router = APIRouter(tags=["system"])

_VERSION = "0.1.0"


def _liveness_body() -> dict:
    return {"status": "ok", "service": "kromarag-api", "version": _VERSION}


@router.get("/health")
async def health():
    return _liveness_body()


@router.get("/healthz")
async def healthz():
    return _liveness_body()


async def _readiness():
    ok = await db_ping()
    body = {
        "ready": ok,
        "db": "ok" if ok else "down",
        "pool": pool_stats(),
        "models": {"gemini": settings.gemini_model, "groq": settings.groq_model},
        "version": _VERSION,
    }
    return body if ok else _service_unavailable(body)


@router.get("/ready")
async def ready():
    return await _readiness()


@router.get("/readyz")
async def readyz():
    return await _readiness()


def _service_unavailable(body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content=body)

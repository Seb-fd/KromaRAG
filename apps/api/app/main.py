"""KromaRAG FastAPI entrypoint. See specs/01 + specs/02 + specs/08 §2–§3."""
from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.documents import router as documents_router
from app.api.v1.ingest import router as ingest_router
from app.api.v1.query import router as query_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.services.db import close_pool, init_pool

setup_logging()

# Fail-closed prod guards (specs/08 §6): refuse to boot on insecure defaults.
# Honor $PORT injected by Cloud Run (specs/08 §6 matrix).
settings.validate_prod_guards()
if any((o or "").strip() == "*" for o in (settings.cors_origins or [])) and settings.is_prod:
    raise RuntimeError("cors_wildcard_rejected: CORS_ORIGINS must not contain '*' in production")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm asyncpg pool if DATABASE_URL present; /ready reports status.
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="KromaRAG API", version="0.1.0", lifespan=lifespan, server_header=False)

# Middleware stack (Starlette executes last-added first): SecurityHeaders
# outermost so EVERY response (incl. 429/CORS/errors) carries headers;
# CORS next; RateLimit innermost so its 429s still pass back through CORS +
# SecurityHeaders on the way out (plus manual headers in the 429 path).
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With",
                   "X-Trace-Id", "traceparent", "tracestate"],
    expose_headers=["X-Trace-Id", "traceparent", "X-RateLimit-Limit",
                    "X-RateLimit-Remaining", "Retry-After"],
    max_age=600,
)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(health_router)  # /health, /healthz, /ready, /readyz (no auth)
app.include_router(metrics_router)  # /metrics (dual-gated: network + admin JWT, specs/09 §3.2)
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(ingest_router, prefix="/api/v1")
app.include_router(query_router, prefix="/api/v1")

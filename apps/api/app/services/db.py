"""Async Postgres pool over generic DATABASE_URL (asyncpg).

Prod posture (specs/08 §2.4): Neon pooled host with pgBouncer transaction mode —
statement_cache_size=0 (required), command_timeout from settings, pool recycle
via max_inactive_connection_lifetime. /ready reports db=down on failure.
"""
from __future__ import annotations

import asyncpg

from app.core.config import settings

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    try:
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=max(1, settings.pool_min_size),
            max_size=max(1, settings.pool_max_size),
            timeout=5,
            command_timeout=settings.pool_command_timeout,
            statement_cache_size=0,  # REQUIRED under pgBouncer transaction pooling
            max_inactive_connection_lifetime=300,  # pool recycle (specs/08 §2.4)
        )
    except Exception:
        _pool = None  # /ready will report db=down; /health stays ok
        try:
            from app.services import metrics as _m

            _m.inc_db_error("DB_UNAVAILABLE")
        except Exception:
            pass


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def db_ping() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as c:
            await c.fetchval("SELECT 1")
        return True
    except Exception:
        try:
            from app.services import metrics as _m

            _m.inc_db_error("DB_UNAVAILABLE")
        except Exception:
            pass
        return False


def get_pool():
    """Return live asyncpg pool or None (no DB configured)."""
    return _pool


def pool_stats() -> dict:
    """Pool counters for /readyz (specs/08 §5.1). Zeroed when pool is down."""
    pool = _pool
    if pool is None:
        return {
            "min": settings.pool_min_size,
            "max": settings.pool_max_size,
            "in_use": 0,
            "idle": 0,
        }
    try:
        return {
            "min": settings.pool_min_size,
            "max": settings.pool_max_size,
            "in_use": pool.get_size() - pool.get_idle_size(),
            "idle": pool.get_idle_size(),
        }
    except Exception:
        return {"min": settings.pool_min_size, "max": settings.pool_max_size, "in_use": 0, "idle": 0}

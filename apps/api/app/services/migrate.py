"""DDL runner: applies migrations/*.sql via DATABASE_URL_SYNC direct host (asyncpg).

Applies 001→002→003 in lexicographic order (specs/08 §2.4). Idempotent;
each file runs in its own transaction. Concurrent runners serialize on a
Postgres advisory lock; failure aborts (entrypoint refuses to serve traffic).

Usage:
  $env:PYTHONPATH="apps/api"; python -m app.services.migrate
  uv run --project apps/api python -m app.services.migrate
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("kromarag.migrate")

_LOCK_KEY = 0x6B726F6D  # 'krom' — advisory lock namespace for migrations


def _migrate_dsn() -> str:
    """Direct-host DSN for DDL (specs/08 §2.4).

    Session-level pg_advisory_lock does NOT serialize across pgBouncer
    transaction pooling (each statement may land on a different backend), so
    migrations MUST prefer DATABASE_URL_SYNC (direct, non-pooled host).
    Falls back to the pooled _dsn() with a warning when SYNC is unset
    (dev/local parity) — lock is then best-effort only.
    """
    from app.core.config import settings

    sync = (getattr(settings, "database_url_sync", "") or "").strip()
    if sync:
        dsn = sync.replace("postgresql+asyncpg://", "postgresql://")
        if "-pooler" in dsn:
            log.warning('{"event":"migrate_pooled_sync_url","msg":"DATABASE_URL_SYNC contains -pooler; advisory lock may not serialize"}')
        return dsn
    from app.services.db import _dsn

    log.warning('{"event":"migrate_sync_url_unset","msg":"DATABASE_URL_SYNC unset — using pooled DSN; advisory lock is best-effort"}')
    return _dsn()


async def apply_migrations(db=None) -> list[str]:
    import asyncpg

    applied: list[str] = []
    mig_dir = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(mig_dir.glob("*.sql"))
    own = db is None
    conn = db
    if own:
        conn = await asyncpg.connect(_migrate_dsn(), timeout=10)
    try:
        # Serialize concurrent Cloud Run revisions (best-effort on fakes).
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        except Exception as e:
            log.warning('{"event":"migrate_lock_skipped","reason":"%s"}', str(e)[:120])
        try:
            for f in files:
                sql = f.read_text(encoding="utf-8")
                # asyncpg executes multi-statement strings natively
                await conn.execute(sql)
                applied.append(f.name)
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
            except Exception:
                pass
    finally:
        if own and conn is not None:
            await conn.close()
    return applied


async def _main() -> None:
    done = await apply_migrations()
    print("migrations applied:", done or ["(none)"])


if __name__ == "__main__":
    asyncio.run(_main())

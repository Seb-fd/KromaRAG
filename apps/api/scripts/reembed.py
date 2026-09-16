"""Option B — in-place re-embedding of existing kroma_chunks (specs/03 §3-§4).

Reads chunk content, re-embeds via `gemini-embedding-001` (output_dimensionality=768),
and UPDATEs `embedding` in place (preserves id/doc_id/ord/citations).

Usage (from repo root or apps/api):
  $env:PYTHONPATH="apps/api"; python apps/api/scripts/reembed.py --dry-run
  $env:PYTHONPATH="apps/api"; python apps/api/scripts/reembed.py --batch-size 32
  $env:PYTHONPATH="apps/api"; python apps/api/scripts/reembed.py --limit 50 --dry-run
  $env:PYTHONPATH="apps/api"; python apps/api/scripts/reembed.py --doc-id <uuid>
  $env:PYTHONPATH="apps/api"; python apps/api/scripts/reembed.py --no-reindex  # skip REINDEX/ANALYZE

Env: DATABASE_URL (postgresql+asyncpg://, Neon pooler), GOOGLE_API_KEY/GEMINI_API_KEY,
     EMBEDDING_MODEL (default gemini-embedding-001), PGVECTOR_DIM=768.

Flow: COUNT → SELECT id/content ORDER BY doc_id,ord → embed batch → UPDATE → REINDEX+ANALYZE → verify.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Ensure `app.*` importable when run as `python apps/api/scripts/reembed.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger("reembed")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="In-place re-embed kroma_chunks with gemini-embedding-001 (768d).")
    p.add_argument("--batch-size", type=int, default=32, help="texts per embed call (default 32)")
    p.add_argument("--limit", type=int, default=0, help="max chunks to process (0 = all)")
    p.add_argument("--doc-id", type=str, default="", help="restrict to one document UUID")
    p.add_argument("--sleep", type=float, default=0.5, help="seconds between batches (avoid 429)")
    p.add_argument("--dry-run", action="store_true", help="embed first batch only, no UPDATEs")
    p.add_argument("--no-reindex", action="store_true", help="skip REINDEX + ANALYZE")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def _dsn() -> str:
    from app.core.config import settings

    url = settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def fetch_chunks(conn, doc_id: str, limit: int):
    if doc_id:
        q = "SELECT id, doc_id, ord, content FROM kroma_chunks WHERE doc_id=$1::uuid ORDER BY ord, id"
        args: tuple = (doc_id,)
        if limit > 0:
            q += " LIMIT $2"
            args = (doc_id, limit)
        return await conn.fetch(q, *args)
    q = "SELECT id, doc_id, ord, content FROM kroma_chunks ORDER BY doc_id, ord, id"
    if limit > 0:
        q += " LIMIT $1"
        return await conn.fetch(q, limit)
    return await conn.fetch(q)


async def preflight(conn) -> dict:
    info: dict = {}
    try:
        info["pgvector"] = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
    except Exception as e:  # noqa: BLE001
        info["pgvector"] = f"ERR {e}"
    info["total_chunks"] = await conn.fetchval("SELECT COUNT(*) FROM kroma_chunks")
    info["total_docs"] = await conn.fetchval("SELECT COUNT(*) FROM kroma_documents")
    try:
        info["mismatched_dims"] = await conn.fetchval(
            "SELECT COUNT(*) FROM kroma_chunks WHERE vector_dims(embedding) != 768"
        )
    except Exception as e:  # noqa: BLE001
        info["mismatched_dims"] = f"ERR {e}"
    try:
        info["null_embeddings"] = await conn.fetchval(
            "SELECT COUNT(*) FROM kroma_chunks WHERE embedding IS NULL"
        )
    except Exception as e:  # noqa: BLE001
        info["null_embeddings"] = f"ERR {e}"
    try:
        info["embedding_idx"] = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname='kroma_chunks_embedding_idx'"
        )
    except Exception:
        info["embedding_idx"] = None
    return info


async def run_reembed(batch_size: int, limit: int, doc_id: str, sleep_s: float,
                      dry_run: bool, no_reindex: bool) -> dict:
    import asyncpg

    from app.core.config import settings
    from app.services.embeddings import DIM, DEFAULT_MODEL, embed_texts

    model = (settings.embedding_model or DEFAULT_MODEL).strip()
    log.info("model=%s dim=%d batch=%d limit=%s doc=%s dry_run=%s",
             model, DIM, batch_size, limit or "ALL", doc_id or "-", dry_run)
    if model != DEFAULT_MODEL:
        log.warning("EMBEDDING_MODEL=%r != %r — vectors will NOT match VECTOR(768) hybrid search", model, DEFAULT_MODEL)
    if not (settings.gemini_api_key or settings.google_api_key):
        raise RuntimeError("missing GOOGLE_API_KEY/GEMINI_API_KEY (see apps/api/.env)")

    conn = await asyncpg.connect(_dsn(), timeout=15)
    stats = {"updated": 0, "failed": 0, "batches": 0, "model": model, "dim": DIM}
    try:
        pre = await preflight(conn)
        log.info("preflight: %s", pre)
        stats["preflight"] = pre

        rows = await fetch_chunks(conn, doc_id, limit)
        log.info("fetched %d chunks", len(rows))
        if not rows:
            log.warning("no chunks found — nothing to re-embed (ingest docs first)")
            return stats

        # Vector literal format matching retriever.py: "[0.123456,...]" cast $1::vector.
        # (ingest.py passes list(vec); string literal is the pgvector-canonical form.)
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            texts = [(r["content"] or "")[:8000] for r in batch]
            if any(not t.strip() for t in texts):
                log.warning("batch %d contains empty content — embedding anyway (model handles it)", stats["batches"] + 1)
            t0 = time.monotonic()
            try:
                vecs = await embed_texts(texts)
            except Exception as e:  # noqa: BLE001 — per-batch failure must not abort whole run
                stats["failed"] += len(batch)
                log.error("embed batch %d FAILED (%d chunks): %s", stats["batches"] + 1, len(batch), str(e)[:500])
                if dry_run:
                    break
                await asyncio.sleep(2.0)
                continue
            dt = time.monotonic() - t0
            stats["batches"] += 1
            bad = [v for v in vecs if len(v) != DIM]
            if bad or len(vecs) != len(batch):
                stats["failed"] += len(batch)
                log.error("batch %d dim/count mismatch: got %d vecs, bad_dims=%d",
                          stats["batches"], len(vecs), len(bad))
                continue
            log.info("batch %d: embedded %d chunks in %.1fs", stats["batches"], len(batch), dt)

            if dry_run:
                log.info("dry-run: first vector dim=%d head=%s", len(vecs[0]), vecs[0][:3])
                stats["updated"] = 0
                break

            for r, v in zip(batch, vecs):
                lit = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
                try:
                    await conn.execute("UPDATE kroma_chunks SET embedding=$1::vector WHERE id=$2::uuid",
                                       lit, str(r["id"]))
                    stats["updated"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["failed"] += 1
                    log.error("UPDATE %s failed: %s", r["id"], str(e)[:300])
            if sleep_s > 0 and i + batch_size < len(rows):
                await asyncio.sleep(sleep_s)

        if not dry_run and not no_reindex:
            log.info("REINDEX INDEX kroma_chunks_embedding_idx ...")
            try:
                await conn.execute("REINDEX INDEX kroma_chunks_embedding_idx")
                log.info("REINDEX ok")
            except Exception as e:  # noqa: BLE001
                log.error("REINDEX failed: %s", str(e)[:300])
                stats["reindex_error"] = str(e)[:300]
            log.info("ANALYZE kroma_chunks ...")
            try:
                await conn.execute("ANALYZE kroma_chunks")
                log.info("ANALYZE ok")
            except Exception as e:  # noqa: BLE001
                log.error("ANALYZE failed: %s", str(e)[:300])
                stats["analyze_error"] = str(e)[:300]

        post = await preflight(conn)
        log.info("postflight: %s", post)
        stats["postflight"] = post
        return stats
    finally:
        await conn.close()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        stats = asyncio.run(run_reembed(args.batch_size, args.limit, args.doc_id,
                                        args.sleep, args.dry_run, args.no_reindex))
    except Exception as e:  # noqa: BLE001
        log.error("reembed FAILED: %s", str(e)[:1000])
        raise SystemExit(1)
    log.info("DONE updated=%d failed=%d batches=%d", stats["updated"], stats["failed"], stats["batches"])


if __name__ == "__main__":
    main()

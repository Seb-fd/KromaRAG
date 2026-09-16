"""Document ingestion + lifecycle pipeline per specs/03 §1-§2 and specs/07 §4.1-§4.2.

upload/URL → mime-validate → extract → clean → chunk (600/90) → Gemini embed (batch 32)
→ INSERT kroma_documents + kroma_chunks + kroma_document_roles

DB-agnostic: `db` is any object exposing async fetchrow/fetch/execute (asyncpg pool/conn or test fake).
`embed_fn` injectable: async (texts: list[str]) -> list[vector]; defaults to Gemini embed_texts.

Status vocabulary (specs/07 §2.2-§2.3, lowercase): pending|indexing|indexed|failed|archived.
Legacy 'ready' (001 default) is normalized to 'indexed' on read/migrate; new writes use 'indexed'.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

import httpx

from app.rag.chunker import ChunkDraft, chunk_text

log = logging.getLogger(__name__)

SUPPORTED_MIMES = {"text/plain", "text/markdown", "application/pdf"}
ALLOWED_ROLES = {"admin", "editor", "viewer"}
URL_MAX_BYTES = 50_000

# ── Spec 07 lifecycle constants ──
STATUS_PENDING = "pending"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"
STATUS_ARCHIVED = "archived"
STATUS_LEGACY_READY = "ready"  # 001-era value; treated as STATUS_INDEXED everywhere
INDEXED_STATUSES = {STATUS_INDEXED, STATUS_LEGACY_READY}

PINNED_EMBEDDING_MODEL = "gemini-embedding-001"
PINNED_EMBEDDING_DIM = 768


class DocNotFoundError(LookupError):
    """Doc invisible to caller → map to uniform 404 NOT_FOUND (specs/07 §3)."""


class DocConflictError(ValueError):
    """State conflict (reindex in flight, purge while indexing) → map to 409."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_mime(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m not in SUPPORTED_MIMES:
        raise ValueError(f"unsupported mime {mime!r}; expected one of {sorted(SUPPORTED_MIMES)}")
    return m


def validate_roles(roles: list[str]) -> list[str]:
    bad = [r for r in roles if r not in ALLOWED_ROLES]
    if bad:
        raise ValueError(f"invalid roles {bad}; allowed {sorted(ALLOWED_ROLES)}")
    return list(roles)


def normalize_status(status: str | None) -> str:
    """Map legacy 'ready' → 'indexed'; pass through canonical values."""
    if status == STATUS_LEGACY_READY:
        return STATUS_INDEXED
    return status or ""


def clean_text(text: str) -> str:
    """Normalize whitespace, strip boilerplate/control chars."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def language_warning(text: str) -> str | None:
    """Phase 1 heuristic: warn (don't block) when text looks non-en/es."""
    if not text:
        return None
    sample = text[:2000]
    if not sample.strip():
        return None
    latin = sum(1 for c in sample if c.isascii() or c in "ñÑáéíóúüÁÉÍÓÚÜ¿¡")
    if latin / max(1, len(sample)) < 0.7:
        return "non-en/es text detected; ingested with warning (Phase 1)"
    return None


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("application/pdf requires `pypdf` (pip install pypdf)") from e
    import io

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages)


# ── Spec 07 §4.1: multipart upload helpers ──

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\- ]+")


def sanitize_filename(name: str | None, default: str = "upload") -> str:
    """Strip paths/control chars; cap 255 chars. Never trusted for storage."""
    base = os.path.basename((name or "").strip()) or default
    base = base.replace("\x00", "").strip()
    base = _FILENAME_SAFE.sub("_", base).strip(" .")
    return (base or default)[:255]


def default_title_for(filename: str | None, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:300]
    base = sanitize_filename(filename)
    # Strip single extension for readability ("handbook.pdf" → "Handbook" keeps case).
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return (base.strip() or "upload")[:300]


def resolve_upload_mime(explicit: str | None, content_type: str | None, filename: str | None) -> str:
    """Trust order: explicit form field > content-type > filename extension. Else 422."""
    if explicit and explicit.strip():
        return validate_mime(explicit)
    if content_type:
        try:
            return validate_mime(content_type)
        except ValueError:
            pass
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".md", ".markdown")):
        return "text/markdown"
    if lower.endswith(".txt"):
        return "text/plain"
    raise ValueError(
        f"could not determine mime from content_type={content_type!r} filename={filename!r}; "
        f"expected one of {sorted(SUPPORTED_MIMES)}"
    )


def decode_upload_bytes(data: bytes, mime: str, filename: str | None = None, max_bytes: int | None = None) -> str:
    """Decode multipart file bytes to text. PDF via server-side pypdf (specs/07 FR-01)."""
    from app.core.config import settings

    cap = int(max_bytes or getattr(settings, "doc_upload_max_bytes", 20_971_520))
    if len(data or b"") > cap:
        raise ValueError(f"file exceeds {cap} byte cap")
    if not data:
        raise ValueError("empty upload")
    m = validate_mime(mime)
    if m == "application/pdf":
        try:
            text = extract_pdf_text(data)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001 — pypdf parse failure → failed/422 upstream
            raise ValueError(f"pdf parse failed for {filename or 'upload'}: {e}") from e
        if not (text or "").strip():
            raise ValueError(
                f"pdf extract yielded no text for {filename or 'upload'} "
                "(scanned-image PDFs need OCR, out of scope Phase 1)"
            )
        return text
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(data).decode("utf-8", errors="replace")


async def fetch_url_text(url: str, client: httpx.AsyncClient | None = None) -> str:
    own = client is None
    c = client or httpx.AsyncClient()
    try:
        r = await c.get(url, timeout=15.0, follow_redirects=True)
        r.raise_for_status()
        data = r.content[:URL_MAX_BYTES]
        return data.decode(r.encoding or "utf-8", errors="replace")
    finally:
        if own:
            await c.aclose()


async def plan_ingest(
    title: str,
    content: str,
    mime: str = "text/plain",
    target: int = 600,
    overlap: int = 90,
    max_chunks: int = 2000,
) -> tuple[list[ChunkDraft], str | None]:
    """Pure step: validate → clean → chunk. Returns (chunks, lang_warning)."""
    mime = validate_mime(mime)
    if not title or not title.strip():
        raise ValueError("title is required")
    cleaned = clean_text(content)
    if not cleaned:
        raise ValueError("empty content after cleaning")
    warn = language_warning(cleaned)
    if warn:
        log.warning("ingest language warning: %s", warn)
    chunks = chunk_text(cleaned, target=target, overlap=overlap, max_chunks=max_chunks)
    if not chunks:
        raise ValueError("no chunks produced")
    return chunks, warn


def _chunk_config_json(target: int, overlap: int) -> str:
    return json.dumps({"chunk_tokens": int(target), "chunk_overlap": int(overlap)})


async def _stamp_lifecycle_columns(db, did: str, target: int, overlap: int,
                                   embedding_model: str, embedding_dim: int) -> None:
    """Best-effort provenance stamp; ignored when 003 columns are absent (pre-migration DB)."""
    try:
        await db.execute(
            "UPDATE kroma_documents SET version=1, chunk_config=$2::jsonb,"
            " embedding_model=$3, embedding_dim=$4, updated_at=now() WHERE id=$1",
            did, _chunk_config_json(target, overlap), embedding_model, int(embedding_dim),
        )
    except Exception:
        pass  # pre-003 DB or fake without the columns — core row is already committed


async def _insert_chunks(db, did: str, chunks: list[ChunkDraft], vectors: list[list[float]]) -> None:
    for ch, vec in zip(chunks, vectors):
        # pgvector-canonical text literal: plain asyncpg has no vector codec,
        # so pass "[f1,f2,...]" string for $4::vector (same as retriever.py).
        vec_literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        await db.execute(
            "INSERT INTO kroma_chunks(id, doc_id, ord, content, embedding, token_count) "
            "VALUES(gen_random_uuid(),$1,$2,$3,$4::vector,$5)",
            did, ch.ord, ch.content, vec_literal, ch.token_count,
        )


async def ingest_document(
    title: str,
    content: str | None = None,
    url: str | None = None,
    mime: str = "text/plain",
    owner_sub: str = "",
    allowed_roles: list[str] | None = None,
    db=None,
    embed_fn=None,
    doc_id: uuid.UUID | str | None = None,
    target: int = 600,
    overlap: int = 90,
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> dict:
    """Full pipeline including DB persistence. Idempotent on doc_id (re-ingest wipes chunks)."""
    from app.services.embeddings import embed_texts as _default_embed

    if not owner_sub:
        raise ValueError("owner_sub is required")
    if db is None:
        raise ValueError("db pool/connection is required")
    roles = validate_roles(list(allowed_roles) if allowed_roles is not None else ["viewer"])
    mime = validate_mime(mime)
    did = str(doc_id) if doc_id else str(uuid.uuid4())
    model = (embedding_model or PINNED_EMBEDDING_MODEL).strip()
    dim = int(embedding_dim or PINNED_EMBEDDING_DIM)

    text = content or ""
    if url and not (content or "").strip():
        text = await fetch_url_text(url)
    chunks, _warn = await plan_ingest(title, text, mime, target, overlap)
    embed = embed_fn or _default_embed

    await db.execute(
        "INSERT INTO kroma_documents(id, owner_sub, title, mime, uri, status) "
        "VALUES($1,$2,$3,$4,$5,'pending') "
        "ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, mime=EXCLUDED.mime, uri=EXCLUDED.uri, status='pending', error=NULL",
        did, owner_sub, title.strip(), mime, url,
    )
    await _stamp_lifecycle_columns(db, did, target, overlap, model, dim)
    try:
        vectors = await embed([c.content for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"embed count mismatch: {len(vectors)} != {len(chunks)}")
        await db.execute("DELETE FROM kroma_chunks WHERE doc_id=$1", did)
        await db.execute("DELETE FROM kroma_document_roles WHERE doc_id=$1", did)
        await _insert_chunks(db, did, chunks, vectors)
        for r in roles:
            await db.execute(
                "INSERT INTO kroma_document_roles(doc_id, role) VALUES($1,$2) ON CONFLICT DO NOTHING",
                did, r,
            )
        await db.execute("UPDATE kroma_documents SET status='indexed', error=NULL, updated_at=now() WHERE id=$1", did)
        return {"doc_id": did, "status": STATUS_INDEXED, "chunks": len(chunks)}
    except Exception as e:  # noqa: BLE001 — mark failed per specs/03 §1
        await db.execute("UPDATE kroma_documents SET status='failed', error=$2 WHERE id=$1", did, str(e)[:2000])
        raise


async def ingest_upload(
    data: bytes,
    filename: str | None = None,
    title: str | None = None,
    mime: str | None = None,
    content_type: str | None = None,
    owner_sub: str = "",
    allowed_roles: list[str] | None = None,
    db=None,
    embed_fn=None,
    doc_id: uuid.UUID | str | None = None,
    target: int = 600,
    overlap: int = 90,
) -> dict:
    """Multipart upload entry (specs/07 FR-01): resolve mime → decode bytes → ingest."""
    resolved = resolve_upload_mime(mime, content_type, filename)
    text = decode_upload_bytes(data, resolved, filename)
    doc_title = default_title_for(filename, title)
    return await ingest_document(
        title=doc_title, content=text, mime=resolved, owner_sub=owner_sub,
        allowed_roles=allowed_roles, db=db, embed_fn=embed_fn, doc_id=doc_id,
        target=target, overlap=overlap,
    )


# ── Spec 07 §4.2: re-indexing (reserve sync + run async) ──

FETCH_DOC_FOR_REINDEX_SQL = (
    "SELECT d.id, d.owner_sub, d.title, d.mime, d.uri, d.status,"
    " d.version, d.chunk_config, d.embedding_model, d.embedding_dim"
    " FROM kroma_documents d WHERE d.id=$1"
)
FETCH_CHUNKS_ORDERED_SQL = (
    "SELECT content FROM kroma_chunks WHERE doc_id=$1 ORDER BY ord ASC"
)
RESERVE_REINDEX_SQL = (
    "UPDATE kroma_documents SET status='indexing', version=$2,"
    " chunk_config=$3::jsonb, embedding_model=$4, updated_at=now()"
    " WHERE id=$1 AND version=$5"
)


def _resolve_reindex_params(doc: dict, chunk_tokens: int | None,
                            chunk_overlap: int | None, embedding_model: str | None) -> tuple[int, int, str]:
    from app.core.config import settings

    stored_cfg: dict = {}
    try:
        raw_cfg = doc.get("chunk_config")
        stored_cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
    except Exception:
        stored_cfg = {}
    target = int(chunk_tokens or stored_cfg.get("chunk_tokens") or settings.chunk_tokens)
    overlap = int(chunk_overlap if chunk_overlap is not None
                  else stored_cfg.get("chunk_overlap", settings.chunk_overlap))
    if target < 100 or target > 2000:
        raise ValueError(f"chunk_tokens {target} out of range 100–2000")
    if overlap < 0 or overlap > 500:
        raise ValueError(f"chunk_overlap {overlap} out of range 0–500")
    if overlap >= target:
        raise ValueError(f"chunk_overlap {overlap} must be < chunk_tokens {target}")
    model = ((embedding_model or doc.get("embedding_model") or PINNED_EMBEDDING_MODEL)).strip()
    if model != PINNED_EMBEDDING_MODEL:
        raise ValueError(
            f"embedding model '{model}' != pinned '{PINNED_EMBEDDING_MODEL}' (EMBEDDING_MISMATCH Phase 1)"
        )
    return target, overlap, model


async def reserve_reindex(
    doc_id: str,
    db,
    chunk_tokens: int | None = None,
    chunk_overlap: int | None = None,
    embedding_model: str | None = None,
) -> dict:
    """Synchronous reservation: validate state → bump version → flip to indexing.

    Caller MUST have passed visibility + write guards (endpoint layer); this enforces
    the state machine (409 on pending/indexing). Returns reservation for the worker.
    """
    row = await db.fetchrow(FETCH_DOC_FOR_REINDEX_SQL, str(doc_id))
    if not row:
        raise DocNotFoundError(str(doc_id))
    doc = dict(row)
    status = normalize_status(doc.get("status"))
    if status in (STATUS_PENDING, STATUS_INDEXING):
        raise DocConflictError("REINDEX_IN_PROGRESS", f"doc {doc_id} already indexing (status={status})")
    if status not in (STATUS_INDEXED, STATUS_LEGACY_READY, STATUS_FAILED, STATUS_ARCHIVED):
        raise DocConflictError("ARCHIVE_CONFLICT", f"doc {doc_id} not reindexable from status={status}")
    target, overlap, model = _resolve_reindex_params(doc, chunk_tokens, chunk_overlap, embedding_model)
    old_version = int(doc.get("version") or 1)
    new_version = old_version + 1
    job_id = str(uuid.uuid4())
    await db.execute(RESERVE_REINDEX_SQL, str(doc_id), new_version,
                     _chunk_config_json(target, overlap), model, old_version)
    return {"doc_id": str(doc_id), "job_id": job_id, "version": new_version,
            "target": target, "overlap": overlap, "model": model,
            "prev_status": status, "uri": doc.get("uri"), "mime": doc.get("mime")}


async def _reassemble_source_text(db, doc: dict) -> str:
    """Authoritative source for re-chunk: URL re-fetch (URL docs) else stored chunks in ord order."""
    uri = (doc.get("uri") or "").strip()
    if uri:
        try:
            fetched = await fetch_url_text(uri)
            if fetched.strip():
                return fetched
        except Exception as e:  # noqa: BLE001 — fall back to stored chunks
            log.warning("reindex url refetch failed for %s: %s", doc.get("id"), e)
    rows = await db.fetch(FETCH_CHUNKS_ORDERED_SQL, str(doc.get("id")))
    parts = []
    for r in (rows or []):
        try:
            parts.append(r["content"])
        except Exception:
            parts.append(dict(r).get("content", ""))
    text = "\n\n".join(p for p in parts if p)
    if not text.strip():
        raise ValueError("no source text to reindex (chunks empty and no fetchable uri)")
    return text


async def run_reindex_job(reservation: dict, db, embed_fn=None) -> dict:
    """Background worker: chunk → embed → single-transaction swap → indexed/failed."""
    from app.services.embeddings import embed_texts as _default_embed

    did = str(reservation["doc_id"])
    target, overlap = int(reservation["target"]), int(reservation["overlap"])
    embed = embed_fn or _default_embed
    # Load doc row for source reassembly (title/mime for plan_ingest validation).
    try:
        row = await db.fetchrow(FETCH_DOC_FOR_REINDEX_SQL, did)
        doc = dict(row) if row else {"id": did, "uri": reservation.get("uri"), "mime": reservation.get("mime")}
    except Exception:
        doc = {"id": did, "uri": reservation.get("uri"), "mime": reservation.get("mime")}
    try:
        source = await _reassemble_source_text(db, doc)
        title = str(doc.get("title") or did)
        mime = str(doc.get("mime") or "text/plain")
        chunks, _warn = await plan_ingest(title, source, mime, target, overlap)
        vectors = await embed([c.content for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"embed count mismatch: {len(vectors)} != {len(chunks)}")
        for v in vectors:
            if len(v) != PINNED_EMBEDDING_DIM:
                raise ValueError(f"expected dim {PINNED_EMBEDDING_DIM}, got {len(v)}")
        # Single-transaction swap (atomic per specs/07 §4.2 rule 3).
        tx = None
        try:
            if hasattr(db, "transaction"):
                tx = db.transaction()
                await tx.__aenter__()
        except Exception:
            tx = None
        try:
            await db.execute("DELETE FROM kroma_chunks WHERE doc_id=$1", did)
            await _insert_chunks(db, did, chunks, vectors)
            # Respect archive-cancel: never flip archived→indexed implicitly.
            try:
                cur = await db.fetchrow("SELECT status FROM kroma_documents WHERE id=$1", did)
                cur_status = normalize_status(dict(cur).get("status") if cur else "")
            except Exception:
                cur_status = STATUS_INDEXING
            if cur_status == STATUS_ARCHIVED:
                await db.execute("UPDATE kroma_documents SET updated_at=now() WHERE id=$1", did)
            else:
                await db.execute(
                    "UPDATE kroma_documents SET status='indexed', error=NULL,"
                    " archived_at=NULL, updated_at=now() WHERE id=$1", did,
                )
        finally:
            try:
                if tx is not None:
                    await tx.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            await db.execute("ANALYZE kroma_chunks")
        except Exception:
            pass  # best-effort (fakes / non-ivfflat hosts)
        return {"doc_id": did, "status": STATUS_INDEXED, "chunks": len(chunks),
                "version": int(reservation["version"])}
    except Exception as e:  # noqa: BLE001 — mark failed, prior chunks preserved per transaction rollback
        try:
            await db.execute("UPDATE kroma_documents SET status='failed', error=$2 WHERE id=$1",
                             did, str(e)[:2000])
        except Exception:
            pass
        raise


async def reindex_document(doc_id: str, db, embed_fn=None,
                           chunk_tokens: int | None = None, chunk_overlap: int | None = None,
                           embedding_model: str | None = None) -> dict:
    """Synchronous full reindex (tests + DOC_REINDEX_ASYNC=false): reserve + run inline."""
    reservation = await reserve_reindex(doc_id, db, chunk_tokens, chunk_overlap, embedding_model)
    try:
        result = await run_reindex_job(reservation, db, embed_fn)
    except Exception:
        # run_reindex_job already flipped status to failed; surface job identity.
        raise
    return {"doc_id": result["doc_id"], "job_id": reservation["job_id"],
            "status": result["status"], "version": result["version"], "chunks": result.get("chunks", 0)}

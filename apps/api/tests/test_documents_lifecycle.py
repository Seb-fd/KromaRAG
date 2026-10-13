"""Document lifecycle tests — Spec 07 (specs/07_document_lifecycle_mgmt_spec.md §5).

Offline fakes (no network/DB): FakeConn implements the asyncpg surface used by
app/services/ingest.py and app/api/v1/documents.py (fetch/fetchrow/fetchval/execute).
Covers: TXT/MD/PDF upload + indexing, legacy JSON ingest, soft-archive vs hard-purge,
async reindex version bumps + 409s, uniform-404 RBAC matrix, retriever indexed-only gate.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.security import get_current_principal
from app.main import app
from app.schemas import Principal
from app.services import ingest as ing

ALICE = Principal(sub="alice", email="alice@x.com", roles=["editor"])  # owner
BOB = Principal(sub="bob", email="bob@x.com", roles=["viewer"])
ADMIN = Principal(sub="root", email="root@x.com", roles=["admin"])


def _lorem(paras: int = 10) -> str:
    base = (
        "## Lifecycle overview\nKromaRAG manages document lifecycles end to end. "
        "Uploads are parsed server-side, chunked, embedded, and indexed for hybrid search. "
        "Archived documents leave retrieval immediately while purge removes vectors forever. "
    )
    return "\n\n".join(f"{base} Paragraph {i} covers versioning, reindexing, and RBAC isolation." for i in range(paras))


def _minimal_pdf(text: str) -> bytes:
    """Build a tiny valid PDF with proper xref (dependency-free fixture for pypdf)."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\nBT /F1 12 Tf 72 720 Td (" % 0 + safe.encode("latin-1", "replace") + b") Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    # Fix Length of stream object (object 4).
    stream_body = b"BT /F1 12 Tf 72 720 Td (" + safe.encode("latin-1", "replace") + b") Tj ET"
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream_body) + stream_body + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref_at)
    return bytes(out)


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.01] * 768 for _ in texts]


# ── In-memory FakeConn (documents + chunks + roles surface) ──

class FakeConn:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.chunks: list[dict] = []
        self.roles: dict[str, set[str]] = {}
        self._seq = 0

    def _now(self):
        self._seq += 1
        base = datetime.now(UTC)
        return base.replace(microsecond=(base.microsecond + self._seq * 997) % 1000000)

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()

    def _visible(self, doc: dict, sub: str, roles: list[str]) -> bool:
        if doc["owner_sub"] == sub or "admin" in (roles or []):
            return True
        return bool(set(self.roles.get(doc["id"], set())) & set(roles or []))

    def _detail(self, doc: dict) -> dict:
        d = dict(doc)
        d["chunks"] = sum(1 for c in self.chunks if c["doc_id"] == doc["id"])
        d["allowed_roles"] = sorted(self.roles.get(doc["id"], set()))
        return d

    # -- writes --
    async def execute(self, sql: str, *args):
        s = sql
        if "SET LOCAL" in s or s.startswith("ANALYZE"):
            return "OK"
        if s.startswith("INSERT INTO kroma_documents(id, owner_sub"):
            did = str(args[0])
            if did in self.docs:
                self.docs[did].update(title=args[2], mime=args[3], uri=args[4],
                                      status="pending", error=None, updated_at=self._now())
            else:
                now = self._now()
                self.docs[did] = {"id": did, "owner_sub": args[1], "title": args[2],
                                  "mime": args[3], "uri": args[4], "status": "pending",
                                  "error": None, "version": 1,
                                  "chunk_config": {"chunk_tokens": 600, "chunk_overlap": 90},
                                  "embedding_model": "gemini-embedding-001", "embedding_dim": 768,
                                  "created_at": now, "updated_at": now, "archived_at": None}
            return "INSERT 0 1"
        if "SET version=1, chunk_config=" in s:
            d = self.docs.get(str(args[0]))
            if d:
                d.update(version=1, chunk_config=json.loads(args[1]), embedding_model=args[2],
                         embedding_dim=int(args[3]), updated_at=self._now())
            return "UPDATE 1"
        if s.startswith("DELETE FROM kroma_chunks WHERE doc_id=$1"):
            self.chunks = [c for c in self.chunks if c["doc_id"] != str(args[0])]
            return "DELETE"
        if s.startswith("DELETE FROM kroma_document_roles WHERE doc_id=$1"):
            self.roles.pop(str(args[0]), None)
            return "DELETE"
        if s.startswith("INSERT INTO kroma_chunks(id, doc_id"):
            self.chunks.append({"doc_id": str(args[0]), "ord": int(args[1]), "content": args[2]})
            return "INSERT 0 1"
        if s.startswith("INSERT INTO kroma_document_roles(doc_id, role)"):
            self.roles.setdefault(str(args[0]), set()).add(args[1])
            return "INSERT 0 1"
        if "SET status='indexed', error=NULL, updated_at=now() WHERE id=$1" in s:
            d = self.docs.get(str(args[0]))
            if d:
                d.update(status="indexed", error=None, updated_at=self._now())
            return "UPDATE 1"
        if "SET status='indexed', error=NULL, archived_at=NULL, updated_at=now() WHERE id=$1" in s:
            d = self.docs.get(str(args[0]))
            if d:
                d.update(status="indexed", error=None, archived_at=None, updated_at=self._now())
            return "UPDATE 1"
        if "SET status='failed', error=$2 WHERE id=$1" in s:
            d = self.docs.get(str(args[0]))
            if d:
                d.update(status="failed", error=args[1], updated_at=self._now())
            return "UPDATE 1"
        if "SET status='indexing', version=$2," in s:
            d = self.docs.get(str(args[0]))
            if d:
                d.update(status="indexing", version=int(args[1]),
                         chunk_config=json.loads(args[2]), embedding_model=args[3],
                         updated_at=self._now())
            return "UPDATE 1"
        if s.startswith("UPDATE kroma_documents SET title=COALESCE($2,title)"):
            d = self.docs.get(str(args[0]))
            if d:
                if args[1] is not None:
                    d["title"] = args[1]
                if args[2] is not None:
                    d["uri"] = args[2]
                if args[3] is not None:
                    d["owner_sub"] = args[3]
                d["updated_at"] = self._now()
            return "UPDATE 1"
        if s.startswith("UPDATE kroma_documents SET updated_at=now() WHERE id=$1"):
            d = self.docs.get(str(args[0]))
            if d:
                d["updated_at"] = self._now()
            return "UPDATE 1"
        if "SET status='archived'" in s:
            d = self.docs.get(str(args[0]))
            if d:
                now = self._now()
                d.update(status="archived", archived_at=now, updated_at=now)
            return "UPDATE 1"
        if s.startswith("DELETE FROM kroma_documents WHERE id=$1"):
            did = str(args[0])
            self.docs.pop(did, None)
            self.chunks = [c for c in self.chunks if c["doc_id"] != did]
            self.roles.pop(did, None)
            return "DELETE 1"
        return "OK"

    # -- single-row reads --
    async def fetchrow(self, sql: str, *args):
        s = sql
        if "WHERE d.id=$1 AND " in s:  # DETAIL / STATUS (visibility-enforced)
            d = self.docs.get(str(args[0]))
            if not d or not self._visible(d, str(args[1]), list(args[2] or [])):
                return None
            return self._detail(d)
        if "d.version, d.chunk_config" in s:  # service reindex fetch (no visibility)
            d = self.docs.get(str(args[0]))
            if not d:
                return None
            out = dict(d)
            out["chunk_config"] = json.dumps(d.get("chunk_config") or {})
            return out
        if s.startswith("SELECT status FROM kroma_documents WHERE id=$1"):
            d = self.docs.get(str(args[0]))
            return {"status": d["status"]} if d else None
        return None

    # -- multi-row reads --
    async def fetch(self, sql: str, *args):
        s = sql
        if "array_agg(role)" in s and "LIMIT $3 OFFSET $4" in s:  # LIST
            sub, roles, limit, offset = str(args[0]), list(args[1] or []), int(args[2]), int(args[3])
            items = [self._detail(d) for d in self.docs.values() if self._visible(d, sub, roles)]
            items.sort(key=lambda d: d["updated_at"], reverse=True)
            return items[offset:offset + limit]
        if "FROM kroma_chunks WHERE doc_id=$1 ORDER BY ord ASC" in s:  # reindex source
            rows = sorted((c for c in self.chunks if c["doc_id"] == str(args[0])), key=lambda c: c["ord"])
            return [{"content": c["content"]} for c in rows]
        return []

    async def fetchval(self, sql: str, *args):
        s = sql
        if s.startswith("SELECT COUNT(*) FROM kroma_documents"):
            sub, roles = str(args[0]), list(args[1] or [])
            return sum(1 for d in self.docs.values() if self._visible(d, sub, roles))
        if s.startswith("SELECT COUNT(*) FROM kroma_chunks WHERE doc_id=$1"):
            return sum(1 for c in self.chunks if c["doc_id"] == str(args[0]))
        return 0


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn

    # asyncpg.Pool proxies single-statement execute/fetch* (used by POST /ingest).
    async def execute(self, sql, *args):
        return await self._conn.execute(sql, *args)

    async def fetch(self, sql, *args):
        return await self._conn.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self._conn.fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        return await self._conn.fetchval(sql, *args)


# ── fixtures ──

@pytest.fixture
def fake_db():
    return FakeConn()


@pytest.fixture
def authed_docs(monkeypatch, fake_db):
    import app.services.db as db_mod
    import app.services.embeddings as emb_mod

    monkeypatch.setattr(db_mod, "get_pool", lambda: FakePool(fake_db))
    monkeypatch.setattr(emb_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr(settings, "doc_reindex_async", False)  # deterministic inline reindex
    app.dependency_overrides[get_current_principal] = lambda: ALICE
    yield fake_db
    app.dependency_overrides.clear()


def _as_user(p: Principal):
    app.dependency_overrides[get_current_principal] = lambda: p


# ── upload + indexing ──

@pytest.mark.asyncio
async def test_upload_txt_indexes(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("note.txt", _lorem(8).encode(), "text/plain")},
                         data={"allowed_roles": '["viewer"]'})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "indexed" and body["chunks"] > 0 and body["job_id"]
        did = body["doc_id"]
        st = (await c.get(f"/api/v1/documents/{did}/status")).json()
        assert st["status"] == "indexed" and st["version"] == 1 and st["error"] is None
        detail = (await c.get(f"/api/v1/documents/{did}")).json()
        assert detail["title"] == "note" and detail["owner_sub"] == "alice"
        assert detail["allowed_roles"] == ["viewer"] and detail["chunks"] == body["chunks"]
        # Shared viewer sees it.
        _as_user(BOB)
        assert (await c.get(f"/api/v1/documents/{did}")).status_code == 200


@pytest.mark.asyncio
async def test_upload_pdf_server_parses(authed_docs):
    pdf = _minimal_pdf("Hello KromaRAG PDF world. Second sentence here.")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("handbook.pdf", pdf, "application/pdf")})
        assert r.status_code == 202, r.text
        did = r.json()["doc_id"]
        detail = (await c.get(f"/api/v1/documents/{did}")).json()
        assert detail["mime"] == "application/pdf" and detail["status"] == "indexed"
        assert detail["title"] == "handbook"  # default from filename sans ext


@pytest.mark.asyncio
async def test_upload_markdown_mime_sniff(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("doc.md", b"# T\n\n" + _lorem(4).encode(), "text/markdown")})
        assert r.status_code == 202, r.text
        assert (await c.get(f"/api/v1/documents/{r.json()['doc_id']}")).json()["mime"] == "text/markdown"


@pytest.mark.asyncio
async def test_upload_guards_422_413_403(authed_docs, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Unsupported mime (explicit .png name + octet-stream).
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("x.png", b"\x89PNG", "application/octet-stream")})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "VALIDATION"
        # Empty file.
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("e.txt", b"", "text/plain")})
        assert r.status_code == 422
        # Oversize (shrink server cap for the test).
        monkeypatch.setattr(settings, "doc_upload_max_bytes", 10)
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("big.txt", b"0123456789ABCDEF", "text/plain")})
        assert r.status_code == 413 and r.json()["detail"]["code"] == "PAYLOAD_TOO_LARGE"
        # Viewer cannot upload (role gate → 403, the only 403 in this spec).
        _as_user(BOB)
        r = await c.post("/api/v1/documents/upload",
                         files={"file": ("n.txt", b"hello world hello world", "text/plain")})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_legacy_ingest_returns_indexed(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/ingest", json={"title": "Note", "content": _lorem(4)})
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "indexed"  # never legacy "ready"
        detail = (await c.get(f"/api/v1/documents/{r.json()['doc_id']}")).json()
        assert detail["version"] == 1 and detail["chunk_config"] == {"chunk_tokens": 600, "chunk_overlap": 90}


# ── metadata update (no re-embed) + admin transfer ──

@pytest.mark.asyncio
async def test_patch_no_reembed_and_visibility(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(6)})).json()["doc_id"]
        before = (await c.get(f"/api/v1/documents/{did}")).json()
        r = await c.patch(f"/api/v1/documents/{did}", json={"allowed_roles": ["editor"]})
        assert r.status_code == 200
        after = r.json()
        assert after["allowed_roles"] == ["editor"] and after["version"] == before["version"] == 1
        assert after["status"] == "indexed" and after["chunks"] == before["chunks"]
        # Viewer loses visibility → uniform 404.
        _as_user(BOB)
        assert (await c.get(f"/api/v1/documents/{did}")).status_code == 404
        # Blank title / bad roles → 422 (as owner).
        _as_user(ALICE)
        assert (await c.patch(f"/api/v1/documents/{did}", json={"title": "   "})).status_code == 422
        assert (await c.patch(f"/api/v1/documents/{did}", json={"allowed_roles": ["root"]})).status_code == 422
        assert (await c.patch(f"/api/v1/documents/{did}", json={})).status_code == 422


@pytest.mark.asyncio
async def test_admin_only_owner_transfer(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(4)})).json()["doc_id"]
        # Non-admin transfer probe → 404 (not 403), owner unchanged.
        r = await c.patch(f"/api/v1/documents/{did}", json={"owner_sub": "bob"})
        assert r.status_code == 404
        assert (await c.get(f"/api/v1/documents/{did}")).json()["owner_sub"] == "alice"
        _as_user(ADMIN)
        r = await c.patch(f"/api/v1/documents/{did}", json={"owner_sub": "bob"})
        assert r.status_code == 200 and r.json()["owner_sub"] == "bob"


# ── reindex (version bump, atomic swap, 409s) ──

@pytest.mark.asyncio
async def test_reindex_bumps_version_and_swaps(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(8)})).json()["doc_id"]
        n0 = (await c.get(f"/api/v1/documents/{did}")).json()["chunks"]
        assert n0 > 0
        r = await c.post(f"/api/v1/documents/{did}/reindex",
                         json={"chunk_tokens": 900, "chunk_overlap": 120})
        assert r.status_code == 202, r.text
        assert r.json()["version"] == 2 and r.json()["status"] == "indexed"  # inline mode
        detail = (await c.get(f"/api/v1/documents/{did}")).json()
        assert detail["version"] == 2 and detail["status"] == "indexed"
        assert detail["chunk_config"] == {"chunk_tokens": 900, "chunk_overlap": 120}
        # Swap, not append: chunk count stays in a sane range (no doubling).
        assert detail["chunks"] <= n0 + 2
        assert authed_docs.docs[did]["archived_at"] is None


@pytest.mark.asyncio
async def test_reindex_conflicts_and_validation(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(4)})).json()["doc_id"]
        # Simulate in-flight job.
        authed_docs.docs[did]["status"] = "indexing"
        r = await c.post(f"/api/v1/documents/{did}/reindex", json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "REINDEX_IN_PROGRESS"
        r = await c.post(f"/api/v1/documents/{did}/purge")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "ARCHIVE_CONFLICT"
        authed_docs.docs[did]["status"] = "indexed"
        # Validation: overlap >= tokens, tokens out of range, model mismatch.
        assert (await c.post(f"/api/v1/documents/{did}/reindex",
                             json={"chunk_tokens": 200, "chunk_overlap": 200})).status_code == 422
        assert (await c.post(f"/api/v1/documents/{did}/reindex",
                             json={"chunk_tokens": 50})).status_code == 422
        r = await c.post(f"/api/v1/documents/{did}/reindex",
                         json={"embedding_model": "other-model"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "EMBEDDING_MISMATCH"


@pytest.mark.asyncio
async def test_reindex_async_worker_path(authed_docs, monkeypatch):
    """Prod default (DOC_REINDEX_ASYNC=true): 202 reserves, BackgroundTasks worker completes."""
    monkeypatch.setattr(settings, "doc_reindex_async", True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(5)})).json()["doc_id"]
        r = await c.post(f"/api/v1/documents/{did}/reindex", json={})
        assert r.status_code == 202, r.text
        assert r.json()["version"] == 2 and r.json()["job_id"]
        # Worker ran before the test client call completed (Starlette awaits background tasks).
        detail = (await c.get(f"/api/v1/documents/{did}")).json()
        assert detail["status"] == "indexed" and detail["version"] == 2


@pytest.mark.asyncio
async def test_reindex_restores_archived(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(5)})).json()["doc_id"]
        assert (await c.delete(f"/api/v1/documents/{did}")).status_code == 204
        assert (await c.get(f"/api/v1/documents/{did}")).json()["status"] == "archived"
        r = await c.post(f"/api/v1/documents/{did}/reindex", json={})
        assert r.status_code == 202
        detail = (await c.get(f"/api/v1/documents/{did}")).json()
        assert detail["status"] == "indexed" and detail["archived_at"] is None and detail["version"] == 2


# ── archive vs purge ──

@pytest.mark.asyncio
async def test_archive_hides_purge_destroys(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        did = (await c.post("/api/v1/ingest", json={"title": "D", "content": _lorem(5)})).json()["doc_id"]
        assert (await c.delete(f"/api/v1/documents/{did}")).status_code == 204
        assert (await c.get(f"/api/v1/documents/{did}")).json()["status"] == "archived"
        # Default list hides archived; explicit filter shows it.
        assert all(d["id"] != did for d in (await c.get("/api/v1/documents")).json()["items"])
        got = (await c.get("/api/v1/documents?status=archived")).json()
        assert any(d["id"] == did for d in got["items"])
        # Archive is idempotent.
        assert (await c.delete(f"/api/v1/documents/{did}")).status_code == 204
        # Purge destroys chunks + roles + row (orphan invariant = 0).
        assert (await c.post(f"/api/v1/documents/{did}/purge")).status_code == 204
        assert (await c.get(f"/api/v1/documents/{did}")).status_code == 404
        assert (await c.get(f"/api/v1/documents/{did}/status")).status_code == 404
        assert sum(1 for ch in authed_docs.chunks if ch["doc_id"] == did) == 0
        assert did not in authed_docs.roles and did not in authed_docs.docs
        assert all(ch["doc_id"] in authed_docs.docs for ch in authed_docs.chunks)  # no orphans
        assert (await c.post(f"/api/v1/documents/{did}/purge")).status_code == 404  # idempotent


# ── RBAC leak-safe 404 matrix ──

@pytest.mark.asyncio
async def test_rbac_404_matrix_byte_identical(authed_docs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Private doc: owner-only (deselect default viewer share).
        did = (await c.post("/api/v1/ingest",
                            json={"title": "secret", "content": _lorem(4),
                                  "allowed_roles": []})).json()["doc_id"]
        ghost = str(uuid.uuid4())
        _as_user(BOB)
        cases = [
            ("GET", f"/api/v1/documents/{did}", None),
            ("GET", f"/api/v1/documents/{did}/status", None),
            ("PATCH", f"/api/v1/documents/{did}", {"title": "x"}),
            ("POST", f"/api/v1/documents/{did}/reindex", {}),
            ("DELETE", f"/api/v1/documents/{did}", None),
            ("POST", f"/api/v1/documents/{did}/purge", None),
        ]
        for method, path, body in cases:
            kwargs = {"json": body} if body is not None else {}
            r = await c.request(method, path, **kwargs)
            assert r.status_code == 404, path
            assert r.json()["detail"]["code"] == "NOT_FOUND"
            g = await c.request(method, path.replace(did, ghost), **kwargs)
            assert g.status_code == 404 and g.json() == r.json(), path  # no existence leak
        assert all(i["id"] != did for i in (await c.get("/api/v1/documents")).json()["items"])
        # Admin bypasses share check.
        _as_user(ADMIN)
        assert (await c.get(f"/api/v1/documents/{did}")).status_code == 200


# ── service-level parsing + retriever gate + migration contract ──

def test_upload_helpers_mime_and_pdf():
    assert ing.resolve_upload_mime(None, "application/pdf", "a.bin") == "application/pdf"
    assert ing.resolve_upload_mime(None, None, "doc.md") == "text/markdown"
    assert ing.resolve_upload_mime(None, None, "n.txt") == "text/plain"
    assert ing.resolve_upload_mime("text/plain", None, "a.pdf") == "text/plain"  # explicit wins
    with pytest.raises(ValueError):
        ing.resolve_upload_mime(None, "application/octet-stream", "noext")
    text = ing.decode_upload_bytes(_minimal_pdf("Server side PDF text."), "application/pdf", "h.pdf")
    assert "Server side PDF text" in text
    with pytest.raises(ValueError):
        ing.decode_upload_bytes(b"", "text/plain", "e.txt")
    with pytest.raises(ValueError):
        ing.decode_upload_bytes(b"x" * 11, "text/plain", "b.txt", max_bytes=10)
    assert ing.default_title_for("handbook.pdf", None) == "handbook"
    assert ing.default_title_for("x.pdf", "  Custom  ") == "Custom"
    assert ing.normalize_status("ready") == "indexed"


@pytest.mark.asyncio
async def test_service_reindex_guards():
    db = FakeConn()
    await ing.ingest_document("D", _lorem(4), owner_sub="u1", db=db, embed_fn=_fake_embed)
    did = next(iter(db.docs))
    with pytest.raises(ing.DocConflictError):
        db.docs[did]["status"] = "indexing"
        await ing.reserve_reindex(did, db)
    db.docs[did]["status"] = "indexed"
    with pytest.raises(ValueError):
        await ing.reserve_reindex(did, db, chunk_tokens=200, chunk_overlap=200)
    with pytest.raises(ValueError):
        await ing.reserve_reindex(did, db, embedding_model="other")
    with pytest.raises(ing.DocNotFoundError):
        await ing.reserve_reindex(str(uuid.uuid4()), db)


def test_retriever_filters_indexed_only():
    from app.services.retriever import HYBRID_SQL

    assert "d.status = 'indexed'" in HYBRID_SQL
    assert "'ready'" not in HYBRID_SQL


def test_migration_003_contract():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "003_kroma_documents_lifecycle.sql").read_text()
    for needle in [
        "UPDATE kroma_documents SET status = 'indexed' WHERE status = 'ready'",
        "kroma_documents_status_chk",
        "pending','indexing','indexed','failed','archived",
        "ADD COLUMN IF NOT EXISTS updated_at",
        "ADD COLUMN IF NOT EXISTS archived_at",
        "ADD COLUMN IF NOT EXISTS version",
        "ADD COLUMN IF NOT EXISTS chunk_config",
        "ADD COLUMN IF NOT EXISTS embedding_model",
        "ADD COLUMN IF NOT EXISTS embedding_dim",
        "kroma_documents_owner_status_idx",
        "kroma_documents_status_updated_idx",
    ]:
        assert needle in sql, needle

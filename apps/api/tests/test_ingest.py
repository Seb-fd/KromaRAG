"""Phase 2 tests: chunking (600/90) + ingestion pipeline (specs/03 §1-§2). No network/DB."""
import uuid

import pytest

from app.rag.chunker import ChunkDraft, chunk_text, count_tokens
from app.services import ingest as ing


def _lorem(paras: int = 12) -> str:
    base = (
        "## KromaRAG overview\nKromaRAG is a role-based retrieval architecture. "
        "Hybrid search blends vector cosine with full-text rank for enterprise docs. "
        "Each chunk carries citations so answers stay grounded. "
    )
    return "\n\n".join(f"{base} Paragraph {i} adds detail about RBAC filtering and SSE streaming." for i in range(paras))


def test_chunk_sizes_and_ord():
    chunks = chunk_text(_lorem(14))
    assert 2 <= len(chunks) <= 12
    for i, c in enumerate(chunks):
        assert isinstance(c, ChunkDraft)
        assert c.ord == i
        assert c.token_count == count_tokens(c.content)
        assert c.token_count <= 750  # target 600 + unit/overlap tolerance


def test_chunk_overlap_carries_tail():
    chunks = chunk_text(_lorem(14))
    assert len(chunks) >= 2
    w0, w1 = chunks[0].content.split(), chunks[1].content.split()
    common = set(w0[-120:]) & set(w1[:80])
    assert len(common) >= 5  # 90-token overlap must echo vocabulary


def test_code_fence_never_unbalanced():
    fence = "```python\ndef SECRET_FN():\n    return 42\n```"
    text = fence + "\n\n" + _lorem(10)
    chunks = chunk_text(text)
    assert any("SECRET_FN" in c.content for c in chunks)
    for c in chunks:  # every chunk keeps fence markers balanced
        assert c.content.count("```") % 2 == 0


def test_chunk_max_chunks_guards():
    with pytest.raises(ValueError):
        chunk_text(_lorem(14), max_chunks=1)


def test_chunk_empty():
    assert chunk_text("   ") == []


async def _fake_embed(texts):
    return [[0.01] * 768 for _ in texts]


class FakeDB:
    def __init__(self):
        self.docs: dict = {}
        self.chunks: list = []
        self.roles: list = []
        self.calls: list = []

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        if sql.startswith("INSERT INTO kroma_documents"):
            did, owner, title, mime, uri = a[:5]
            self.docs[did] = {"id": did, "owner_sub": owner, "title": title, "mime": mime, "uri": uri, "status": "pending", "error": None}
        elif sql.startswith("DELETE FROM kroma_chunks"):
            self.chunks = [c for c in self.chunks if c["doc_id"] != a[0]]
        elif sql.startswith("DELETE FROM kroma_document_roles"):
            self.roles = [r for r in self.roles if r[0] != a[0]]
        elif sql.startswith("INSERT INTO kroma_chunks"):
            self.chunks.append({"doc_id": a[0], "ord": a[1], "content": a[2]})
        elif sql.startswith("INSERT INTO kroma_document_roles"):
            self.roles.append((a[0], a[1]))
        elif "SET status='indexed'" in sql:
            self.docs[a[0]]["status"] = "indexed"
        elif "SET status='failed'" in sql:
            self.docs[a[0]].update(status="failed", error=a[1])
        return "OK"


def test_clean_and_mime_validation():
    assert ing.clean_text("a  \t b\r\n\r\n\r\nc") == "a b\n\nc"
    with pytest.raises(ValueError):
        ing.validate_mime("image/png")
    with pytest.raises(ValueError):
        ing.validate_roles(["superuser"])


@pytest.mark.asyncio
async def test_ingest_happy_path_and_idempotent():
    db = FakeDB()
    did = str(uuid.uuid4())
    r1 = await ing.ingest_document("Doc", _lorem(8), mime="text/markdown",
                                   owner_sub="u1", allowed_roles=["viewer", "editor"], db=db, embed_fn=_fake_embed, doc_id=did)
    assert r1 == {"doc_id": did, "status": "indexed", "chunks": len(db.chunks)}
    assert db.docs[did]["status"] == "indexed"
    assert (did, "viewer") in db.roles and (did, "editor") in db.roles
    n = len(db.chunks)
    r2 = await ing.ingest_document("Doc", _lorem(8), mime="text/markdown",
                                   owner_sub="u1", allowed_roles=["viewer"], db=db, embed_fn=_fake_embed, doc_id=did)
    assert r2["chunks"] == n and len(db.chunks) == n  # re-ingest wipes, not doubles


@pytest.mark.asyncio
async def test_ingest_failure_marks_failed():
    db = FakeDB()

    async def boom(texts):
        raise RuntimeError("gemini down")

    with pytest.raises(RuntimeError):
        await ing.ingest_document("Doc", _lorem(4), owner_sub="u1", db=db, embed_fn=boom)
    did = next(iter(db.docs))
    assert db.docs[did]["status"] == "failed"
    assert "gemini down" in db.docs[did]["error"]


@pytest.mark.asyncio
async def test_ingest_requires_owner_and_db():
    with pytest.raises(ValueError):
        await ing.ingest_document("T", "hello", owner_sub="", db=FakeDB(), embed_fn=_fake_embed)
    with pytest.raises(ValueError):
        await ing.ingest_document("T", "hello", owner_sub="u", db=None, embed_fn=_fake_embed)

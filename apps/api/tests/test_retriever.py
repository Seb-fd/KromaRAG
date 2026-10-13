"""Phase 2 tests: hybrid search 0.7/0.3 + RBAC (specs/03 §4). No network/DB."""
import pytest

from app.schemas import Principal
from app.services.retriever import HYBRID_SQL, build_hybrid_params, get_doc_metadata, hybrid_search, rerank_blended

VEC = [0.02] * 768
P = Principal(sub="u1", email="u1@x.com", roles=["viewer"])


def test_sql_has_rbac_and_weights():
    sql, args = build_hybrid_params(VEC, "hello world", "u1", ["viewer"], top_k=6)
    assert sql is HYBRID_SQL
    for needle in ["owner_sub = $3", "kroma_document_roles", "ANY($4)", "0.7", "0.3",
                   "ts_rank", "ORDER BY c.embedding", "status = 'indexed'", "LIMIT $5"]:
        assert needle in sql, needle
    assert args[2] == "u1" and args[3] == ["viewer"] and args[4] == 18  # top_k*3


def test_sql_validates_inputs():
    with pytest.raises(ValueError):
        build_hybrid_params([0.1] * 10, "q", "u1", ["viewer"], 6)
    with pytest.raises(ValueError):
        build_hybrid_params(VEC, "q", "", ["viewer"], 6)


def test_rerank_sorts_filters_caps():
    rows = [
        {"chunk_id": "a", "doc_id": "d", "title": "T", "content": "c", "ord": 0, "score": 0.9},
        {"chunk_id": "b", "doc_id": "d", "title": "T", "content": "c", "ord": 1, "score": 0.1},  # below min
        {"chunk_id": "c", "doc_id": "d", "title": "T", "content": "c", "ord": 2, "score": 0.5},
    ]
    out = rerank_blended(rows, top_k=6, score_min=0.25)
    assert [r.chunk_id for r in out] == ["a", "c"]
    out1 = rerank_blended(rows, top_k=1, score_min=0.0)
    assert len(out1) == 1 and out1[0].chunk_id == "a"
    assert rerank_blended(rows, top_k=6, score_min=0.99) == []  # no evidence


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.seen_sql: list = []
        self.probes_set = False

    async def execute(self, sql, *a):
        if "ivfflat.probes" in sql:
            self.probes_set = True
        self.seen_sql.append(sql)
        return "OK"

    async def fetch(self, sql, *args):
        self.seen_sql.append(sql)
        self.fetch_args = args
        return self.rows

    async def fetchrow(self, sql, *args):
        self.seen_sql.append(sql)
        self.fetchrow_args = args
        return self.rows[0] if self.rows else None


@pytest.mark.asyncio
async def test_hybrid_search_wires_rbac_and_probes():
    rows = [
        {"chunk_id": "c1", "doc_id": "d1", "title": "Doc", "content": "alpha", "ord": 0, "score": 0.8},
        {"chunk_id": "c2", "doc_id": "d1", "title": "Doc", "content": "beta", "ord": 1, "score": 0.3},
    ]
    db = FakeDB(rows)

    async def fake_embed(q: str):
        assert q == "alpha query"
        return list(VEC)

    out = await hybrid_search("alpha query", P, top_k=6, db=db, embed_fn=fake_embed)
    assert db.probes_set  # ivfflat probes=10 attempted
    assert db.fetch_args[2] == "u1" and db.fetch_args[3] == ["viewer"]  # RBAC bound, not post-filtered
    assert [r.chunk_id for r in out] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_hybrid_search_empty_query_skips_db():
    db = FakeDB([])
    assert await hybrid_search("   ", P, db=db, embed_fn=lambda q: VEC) == []
    assert db.seen_sql == []


@pytest.mark.asyncio
async def test_get_doc_metadata_rbac():
    row = {"id": "d1", "title": "Doc", "mime": "text/plain", "status": "indexed"}
    db = FakeDB([row])
    got = await get_doc_metadata("d1", P, db=db)
    assert got == row
    assert "ANY($3)" in db.seen_sql[-1]  # role filter in SQL
    assert await get_doc_metadata("d9", P, db=FakeDB([])) is None  # unauthorized → None (no leak)

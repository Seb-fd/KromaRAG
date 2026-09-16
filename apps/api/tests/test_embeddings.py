"""Embeddings unit tests: batching, dim guard, query LRU cache (no network)."""
import time

import pytest

from app.services import embeddings as E


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, dim=768):
        self.dim = dim
        self.calls = 0

    async def post(self, url, params=None, json=None, timeout=None):
        self.calls += 1
        n = len(json["requests"])
        return _Resp({"embeddings": [{"values": [0.5] * self.dim} for _ in range(n)]})

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_embed_texts_batches_and_dim(monkeypatch):
    monkeypatch.setattr(E, "_api_key", lambda: "k")
    c = FakeClient()
    vecs = await E.embed_texts(["a"] * 33, client=c)  # 33 → 2 batches of 32/1
    assert len(vecs) == 33 and all(len(v) == 768 for v in vecs)
    assert c.calls == 2


@pytest.mark.asyncio
async def test_embed_texts_rejects_bad_dim(monkeypatch):
    monkeypatch.setattr(E, "_api_key", lambda: "k")
    with pytest.raises(Exception):
        await E.embed_texts(["a"], client=FakeClient(dim=10))


@pytest.mark.asyncio
async def test_embed_query_cache(monkeypatch):
    monkeypatch.setattr(E, "_api_key", lambda: "k")
    E._cache.clear()
    c = FakeClient()
    v1 = await E.embed_query("Hello  World", client=c)
    v2 = await E.embed_query("hello world", client=c)  # normalized → cache hit, no new POST
    assert v1 == v2 and c.calls == 1
    # Expire manually → refetch
    k = E._norm("hello world")
    ts, vec = E._cache[k]
    E._cache[k] = (ts - E.CACHE_TTL - 1, vec)
    await E.embed_query("hello world", client=c)
    assert c.calls == 2


def test_embed_requires_key(monkeypatch):
    import asyncio

    monkeypatch.setattr(E, "_api_key", lambda: "")
    with pytest.raises(RuntimeError):
        asyncio.run(E.embed_texts(["x"], client=FakeClient()))
    assert time  # silence lint

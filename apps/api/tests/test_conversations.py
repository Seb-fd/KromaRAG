"""Phase 1 tests: Conversations & History (specs/06). Offline fakes, no network/DB."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent import graph as G
from app.agent.graph import AgentDeps
from app.agent.llm import GenerateResult
from app.api.v1 import query as query_mod
from app.core.config import settings
from app.core.security import get_current_principal
from app.main import app
from app.schemas import Principal
from app.services import conversations as conv_svc
from app.services.conversations import auto_title, truncate_history

ALICE = Principal(sub="alice", email="alice@x.com", roles=["viewer"])
BOB = Principal(sub="bob", email="bob@x.com", roles=["viewer"])


# ── In-memory FakeDB (asyncpg surface) ──

class FakeConn:
    def __init__(self):
        self.convs: dict[str, dict] = {}
        self.msgs: list[dict] = []
        self.traces: set[str] = set()
        self._seq = 0

    def _now(self):
        self._seq += 1
        # Distinct, monotonic timestamps (ms resolution + seq micros).
        base = datetime.now(UTC)
        return base.replace(microsecond=(base.microsecond + self._seq) % 1000000)

    # -- transaction (no-op, asyncpg-compatible) --
    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()

    # -- writes --
    async def execute(self, sql: str, *args):
        s = sql
        if "SET LOCAL" in s:
            return "SET"
        if "INSERT INTO kroma_conversations(id, user_sub, title)" in s:
            cid, sub, title = str(args[0]), args[1], args[2]
            if cid not in self.convs:
                now = self._now()
                self.convs[cid] = {"id": cid, "user_sub": sub, "title": title,
                                   "created_at": now, "updated_at": now}
            return "INSERT 0 1"
        if "INSERT INTO kroma_messages(conv_id, role, content) VALUES" in s:
            cid, content = str(args[0]), args[1]
            if cid not in self.convs:
                raise Exception("FK violation")
            self.msgs.append({"id": str(uuid.uuid4()), "conv_id": cid, "role": "user",
                              "content": content, "trace_id": None, "created_at": self._now(),
                              "prompt_tokens": 0, "completion_tokens": 0, "model": "", "route": "retrieval"})
            return "INSERT 0 1"
        if "INSERT INTO kroma_messages(conv_id, role, content, trace_id" in s:
            cid, content, trace = str(args[0]), args[1], args[2]
            pt, ct, model, route = args[3], args[4], args[5], args[6]
            if trace is not None and trace in self.traces:
                return "INSERT 0 0"  # ON CONFLICT DO NOTHING
            if cid not in self.convs:
                raise Exception("FK violation")
            self.msgs.append({"id": str(uuid.uuid4()), "conv_id": cid, "role": "assistant",
                              "content": content, "trace_id": trace, "created_at": self._now(),
                              "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0),
                              "model": str(model or ""), "route": str(route or "retrieval")})
            if trace is not None:
                self.traces.add(trace)
            return "INSERT 0 1"
        if s.strip().startswith("UPDATE kroma_conversations SET title="):
            cid, sub, title = str(args[0]), args[1], args[2]
            c = self.convs.get(cid)
            if c and c["user_sub"] == sub:
                c["title"] = title
                c["updated_at"] = self._now()
            return "UPDATE 1"
        if s.strip().startswith("UPDATE kroma_conversations SET updated_at=now()"):
            cid, sub = str(args[0]), args[1]
            c = self.convs.get(cid)
            if c and c["user_sub"] == sub:
                c["updated_at"] = self._now()
            return "UPDATE 1"
        return "OK"

    # -- single-row reads --
    async def fetchrow(self, sql: str, *args):
        s = sql
        if s.strip().startswith("INSERT INTO kroma_conversations(user_sub, title)"):
            sub, title = args[0], args[1]
            cid = str(uuid.uuid4())
            now = self._now()
            self.convs[cid] = {"id": cid, "user_sub": sub, "title": title,
                               "created_at": now, "updated_at": now}
            return dict(self.convs[cid])
        if "UPDATE kroma_conversations SET title=" in s and "RETURNING" in s:
            cid, sub, title = str(args[0]), args[1], args[2]
            c = self.convs.get(cid)
            if not c or c["user_sub"] != sub:
                return None
            c["title"] = title
            c["updated_at"] = self._now()
            return dict(c)
        if s.strip().startswith("DELETE FROM kroma_conversations"):
            cid, sub = str(args[0]), args[1]
            c = self.convs.get(cid)
            if not c or c["user_sub"] != sub:
                return None
            del self.convs[cid]
            self.msgs = [m for m in self.msgs if m["conv_id"] != cid]
            return {"id": cid}
        if "FROM kroma_conversations c WHERE c.id=$1" in s:
            cid, sub = str(args[0]), args[1]
            c = self.convs.get(cid)
            if not c or c["user_sub"] != sub:
                return None
            d = dict(c)
            d["message_count"] = sum(1 for m in self.msgs if m["conv_id"] == cid)
            return d
        if "SELECT id FROM kroma_conversations WHERE id=$1" in s:
            cid, sub = str(args[0]), args[1]
            c = self.convs.get(cid)
            if not c or c["user_sub"] != sub:
                return None
            return {"id": cid}
        if "SELECT title FROM kroma_conversations WHERE" in s:
            cid, sub = str(args[0]), args[1]
            c = self.convs.get(cid)
            if not c or c["user_sub"] != sub:
                return None
            return {"title": c["title"]}
        if "FROM kroma_messages WHERE id=$1 AND conv_id=$2" in s:
            mid, cid = str(args[0]), str(args[1])
            for m in self.msgs:
                if m["id"] == mid and m["conv_id"] == cid:
                    return {"created_at": m["created_at"], "id": m["id"]}
            return None
        if "FROM kroma_messages WHERE trace_id=$1" in s:
            for m in self.msgs:
                if m.get("trace_id") == args[0]:
                    return {"id": m["id"]}
            return None
        return None

    # -- multi-row reads --
    async def fetch(self, sql: str, *args):
        s = sql
        if "FROM kroma_conversations c WHERE c.user_sub=$1" in s:
            sub, limit, offset = args[0], int(args[1]), int(args[2])
            items = [c for c in self.convs.values() if c["user_sub"] == sub]
            items.sort(key=lambda c: c["updated_at"], reverse=True)
            page = items[offset:offset + limit]
            out = []
            for c in page:
                d = dict(c)
                d["message_count"] = sum(1 for m in self.msgs if m["conv_id"] == c["id"])
                out.append(d)
            return out
        if "FROM kroma_messages" in s and "ORDER BY created_at DESC" in s:
            if "(created_at, id) <" in s:
                cid = str(args[0])
                anchor_created, anchor_id, limit = args[1], str(args[2]), int(args[3])
                rows = [m for m in self.msgs if m["conv_id"] == cid]
                rows.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
                # Anchor = exclusive upper bound in DESC order.
                out = []
                for m in rows:
                    if (m["created_at"], m["id"]) < (anchor_created, anchor_id):
                        out.append(dict(m))
                    if len(out) >= limit:
                        break
                # Keep DESC order (caller reverses to ASC).
                # Our loop already emits DESC; fine.
                return out[:limit]
            cid, limit = str(args[0]), int(args[1])
            rows = [m for m in self.msgs if m["conv_id"] == cid]
            rows.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
            return [dict(m) for m in rows[:limit]]
        return []

    async def fetchval(self, sql: str, *args):
        s = sql
        if "FROM kroma_conversations WHERE user_sub=$1" in s:
            return sum(1 for c in self.convs.values() if c["user_sub"] == args[0])
        if "FROM kroma_messages WHERE conv_id=$1" in s:
            return sum(1 for m in self.msgs if m["conv_id"] == str(args[0]))
        return 0


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


# ── fixtures ──

@pytest.fixture
def fake_db():
    return FakeConn()


@pytest.fixture
def authed_crud(monkeypatch, fake_db):
    """Conversations endpoints see FakePool; query LLM fakes answer with citation."""
    import app.api.v1.conversations as conv_mod  # noqa: F401 (ensures router import)
    import app.services.db as db_mod

    pool = FakePool(fake_db)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    async def _gemini_ok(prompt, system=None, timeout_s=25, client=None):
        return GenerateResult(text="Answer here [doc:Doc:0]",
                              prompt_tokens=10, completion_tokens=5, model=settings.gemini_model)

    async def _groq_ok(prompt, system=None, timeout_s=25, client=None):
        return GenerateResult(text="Groq answer [doc:Doc:0]",
                              prompt_tokens=5, completion_tokens=2, model=settings.groq_model)

    async def _hybrid_ok(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25):
        return []

    monkeypatch.setattr(G, "hybrid_search", _hybrid_ok)
    monkeypatch.setattr(query_mod, "resolve_deps",
                        lambda: AgentDeps(db=fake_db, gemini_fn=_gemini_ok, groq_fn=_groq_ok))
    app.dependency_overrides[get_current_principal] = lambda: ALICE
    yield fake_db
    app.dependency_overrides.clear()


def _as_user(sub: Principal):
    app.dependency_overrides[get_current_principal] = lambda: sub


# ── CRUD lifecycle ──

@pytest.mark.asyncio
async def test_crud_lifecycle(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/conversations", json={})
        assert r.status_code == 201
        tid = r.json()["id"]
        assert r.json()["title"] == "New conversation" and r.json()["message_count"] == 0

        r = await c.get(f"/api/v1/conversations/{tid}")
        assert r.status_code == 200 and r.json()["id"] == tid

        r = await c.patch(f"/api/v1/conversations/{tid}", json={"title": "Project X"})
        assert r.status_code == 200 and r.json()["title"] == "Project X"

        r = await c.get(f"/api/v1/conversations/{tid}/messages")
        assert r.status_code == 200 and r.json()["items"] == [] and r.json()["next_before_id"] is None

        r = await c.get("/api/v1/conversations")
        assert r.json()["total"] == 1 and r.json()["items"][0]["id"] == tid

        r = await c.delete(f"/api/v1/conversations/{tid}")
        assert r.status_code == 204
        assert (await c.get(f"/api/v1/conversations/{tid}")).status_code == 404
        assert (await c.get(f"/api/v1/conversations/{tid}/messages")).status_code == 404
        assert (await c.delete(f"/api/v1/conversations/{tid}")).status_code == 404  # idempotent


@pytest.mark.asyncio
async def test_list_isolation(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/conversations", json={"title": "A1"})
        await c.post("/api/v1/conversations", json={"title": "A2"})
        _as_user(BOB)
        await c.post("/api/v1/conversations", json={"title": "B1"})
        rb = await c.get("/api/v1/conversations")
        assert rb.json()["total"] == 1 and rb.json()["items"][0]["title"] == "B1"
        _as_user(ALICE)
        ra = await c.get("/api/v1/conversations")
        assert ra.json()["total"] == 2


@pytest.mark.asyncio
async def test_leak_safe_404_matrix(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        tid = (await c.post("/api/v1/conversations", json={"title": "secret"})).json()["id"]
        ghost = str(uuid.uuid4())
        _as_user(BOB)
        paths = [
            ("GET", f"/api/v1/conversations/{tid}", None),
            ("GET", f"/api/v1/conversations/{tid}/messages", None),
            ("PATCH", f"/api/v1/conversations/{tid}", {"title": "x"}),
            ("DELETE", f"/api/v1/conversations/{tid}", None),
        ]
        for method, path, body in paths:
            r = await c.request(method, path, json=body) if body else await c.request(method, path)
            assert r.status_code == 404, path
            assert r.json()["detail"]["code"] == "NOT_FOUND"
            # Byte-identical to a truly-missing id (no existence leak).
            g = await c.request(method, path.replace(tid, ghost), json=body) if body else await c.request(method, path.replace(tid, ghost))
            assert g.status_code == 404 and g.json() == r.json()
        # Query with foreign conv_id is also 404, never 403.
        r = await c.post("/api/v1/query", json={"query": "hi there?", "conv_id": tid, "stream": False})
        assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
        # Bob's own list never contains Alice's thread.
        assert all(i["id"] != tid for i in (await c.get("/api/v1/conversations")).json()["items"])


@pytest.mark.asyncio
async def test_blank_patch_422(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        tid = (await c.post("/api/v1/conversations", json={})).json()["id"]
        assert (await c.patch(f"/api/v1/conversations/{tid}", json={"title": "   "})).status_code == 422


# ── Query wiring: implicit create, persist, history ──

@pytest.mark.asyncio
async def test_query_implicit_creates_and_persists(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/query", json={"query": "What is KromaRAG?", "stream": False})
        assert r.status_code == 200
        tid = r.json()["conv_id"]
        assert r.json()["history_tokens"] == 0  # brand-new thread
        msgs = (await c.get(f"/api/v1/conversations/{tid}/messages")).json()["items"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "What is KromaRAG?"
        # Auto-title from first query (first sentence, delimiter stripped per §4.3).
        meta = (await c.get(f"/api/v1/conversations/{tid}")).json()
        assert meta["title"] == "What is KromaRAG" and meta["message_count"] == 2


@pytest.mark.asyncio
async def test_followup_injects_history_retrieval_isolated(authed_crud, monkeypatch):
    seen: dict = {}

    async def _hybrid_capture(query, principal, top_k=6, db=None, embed_fn=None, score_min=0.25):
        seen["query"] = query
        return []

    async def _gemini_capture(prompt, system=None, timeout_s=25, client=None):
        seen["prompt"] = prompt
        return GenerateResult(text="Follow-up [doc:Doc:0]",
                              prompt_tokens=10, completion_tokens=5, model=settings.gemini_model)

    monkeypatch.setattr(G, "hybrid_search", _hybrid_capture)
    import app.api.v1.query as qm

    orig = qm.resolve_deps
    monkeypatch.setattr(qm, "resolve_deps",
                        lambda: AgentDeps(db=authed_crud, gemini_fn=_gemini_capture,
                                         groq_fn=_gemini_capture))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        t1 = (await c.post("/api/v1/query", json={"query": "What is KromaRAG?", "stream": False})).json()
        tid = t1["conv_id"]
        # Use a non-greeting follow-up so the retrieval route runs.
        r2 = await c.post("/api/v1/query", json={"query": "Explain its fallback mechanism in detail?", "conv_id": tid, "stream": False})
        assert r2.status_code == 200 and r2.json()["conv_id"] == tid
        assert r2.json()["history_tokens"] > 0
        assert "What is KromaRAG?" in seen["prompt"]  # verbatim history injected
        assert "Conversation history" in seen["prompt"]
        assert seen["query"] == "Explain its fallback mechanism in detail?"  # retriever isolated
        assert "What is KromaRAG?" not in seen["query"]
        msgs = (await c.get(f"/api/v1/conversations/{tid}/messages")).json()["items"]
        assert len(msgs) == 4  # two pairs ascending
    monkeypatch.setattr(qm, "resolve_deps", orig)


@pytest.mark.asyncio
async def test_direct_greeting_persists(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        tid = (await c.post("/api/v1/conversations", json={})).json()["id"]
        r = await c.post("/api/v1/query", json={"query": "hello", "conv_id": tid, "stream": False})
        assert r.status_code == 200
        msgs = (await c.get(f"/api/v1/conversations/{tid}/messages")).json()["items"]
        assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["content"] == "hello"
        assert authed_crud.msgs[-1]["route"] == "direct"


@pytest.mark.asyncio
async def test_auto_title_once_then_patch_wins(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        long_q = "Explain hybrid search 0.7 cosine plus 0.3 ts_rank in detail please? Extra words here."
        tid = (await c.post("/api/v1/query", json={"query": long_q, "stream": False})).json()["conv_id"]
        t1 = (await c.get(f"/api/v1/conversations/{tid}")).json()["title"]
        assert len(t1) <= 61 and t1 != "New conversation"
        await c.post("/api/v1/query", json={"query": "second question here?", "conv_id": tid, "stream": False})
        assert (await c.get(f"/api/v1/conversations/{tid}")).json()["title"] == t1
        await c.patch(f"/api/v1/conversations/{tid}", json={"title": "Custom"})
        await c.post("/api/v1/query", json={"query": "third question here?", "conv_id": tid, "stream": False})
        assert (await c.get(f"/api/v1/conversations/{tid}")).json()["title"] == "Custom"


# ── Pagination / truncation / idempotency (unit + integration) ──

@pytest.mark.asyncio
async def test_message_pagination_replays_all(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        tid = (await c.post("/api/v1/conversations", json={})).json()["id"]
        for i in range(15):
            await authed_crud.execute(
                "INSERT INTO kroma_messages(conv_id, role, content) VALUES ($1,'user',$2)", tid, f"q{i}")
        # 15 msgs, pages of 10 → 10 + 5, ascending within each page.
        p1 = (await c.get(f"/api/v1/conversations/{tid}/messages?limit=10")).json()
        assert len(p1["items"]) == 10 and p1["next_before_id"] is not None
        assert [m["content"] for m in p1["items"]] == [f"q{i}" for i in range(5, 15)]
        p2 = (await c.get(
            f"/api/v1/conversations/{tid}/messages?limit=10&before_id={p1['next_before_id']}")).json()
        assert [m["content"] for m in p2["items"]] == [f"q{i}" for i in range(5)]
        assert p2["next_before_id"] is None
        # Foreign before_id → 404.
        other = (await c.post("/api/v1/conversations", json={})).json()["id"]
        await authed_crud.execute(
            "INSERT INTO kroma_messages(conv_id, role, content) VALUES ($1,'user',$2)", other, "zzz")
        foreign_id = authed_crud.msgs[-1]["id"]
        r = await c.get(f"/api/v1/conversations/{tid}/messages?before_id={foreign_id}")
        assert r.status_code == 404


def test_truncate_history_caps():
    msgs = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"question number {i} " + "x" * 50})
        msgs.append({"role": "assistant", "content": f"answer number {i} " + "y" * 50})
    kept, toks = truncate_history(msgs, max_turns=10, max_chars=6000, max_tokens=1500)
    assert len(kept) <= 20  # ≤10 pairs
    assert sum(len(m["content"]) for m in kept) <= 6000
    assert toks <= 1500
    # Oldest dropped first: newest survives.
    assert kept[-1]["content"].startswith("answer number 19")
    # Char cap never splits a message mid-way (except single-oversize marker).
    single = [{"role": "user", "content": "z" * 7000}]
    kept2, _ = truncate_history(single, max_turns=10, max_chars=6000, max_tokens=100000)
    assert kept2[0]["content"].endswith("… [truncated]")


def test_auto_title_rules():
    assert auto_title("") == "New conversation"
    assert auto_title("What is KromaRAG? Extra.") == "What is KromaRAG"
    long_q = "Explain hybrid search 0.7 cosine plus 0.3 ts_rank in detail please? Extra"
    assert len(auto_title(long_q)) <= 61 and auto_title(long_q).endswith("…")


@pytest.mark.asyncio
async def test_idempotent_redelivery_no_duplicate(fake_db):
    cid = str(uuid.uuid4())
    now = datetime.now(UTC)
    fake_db.convs[cid] = {"id": cid, "user_sub": "alice", "title": "t",
                          "created_at": now, "updated_at": now}
    await conv_svc.persist_pair(fake_db, cid, "alice", "q?", "a.", "TRACE-1",
                                {"prompt_tokens": 1, "completion_tokens": 1, "model": "m"}, "retrieval")
    await conv_svc.persist_pair(fake_db, cid, "alice", "q?", "a.", "TRACE-1",
                                {"prompt_tokens": 1, "completion_tokens": 1, "model": "m"}, "retrieval")
    assistants = [m for m in fake_db.msgs if m["role"] == "assistant"]
    assert len(assistants) == 1 and assistants[0]["trace_id"] == "TRACE-1"


@pytest.mark.asyncio
async def test_sse_persists_and_reports_history_tokens(authed_crud):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/api/v1/query", json={"query": "What is KromaRAG?"})
        assert r1.status_code == 200
        import json as _json

        kinds = []
        for blk in r1.text.replace("\r\n", "\n").split("\n\n"):
            for line in blk.splitlines():
                if line.startswith("event:"):
                    kinds.append(line.split(":", 1)[1].strip())
        assert kinds[0] == "source" and kinds[-1] == "done"
        done = None
        for blk in r1.text.replace("\r\n", "\n").split("\n\n"):
            if "event: done" in blk:
                for line in blk.splitlines():
                    if line.startswith("data:"):
                        done = _json.loads(line.split(":", 1)[1].strip())
        tid = done["conv_id"]
        assert done["history_tokens"] == 0
        r2 = await c.post("/api/v1/query", json={"query": "Second detailed question here?", "conv_id": tid})
        for blk in r2.text.replace("\r\n", "\n").split("\n\n"):
            if "event: done" in blk:
                for line in blk.splitlines():
                    if line.startswith("data:"):
                        done2 = _json.loads(line.split(":", 1)[1].strip())
        assert done2["conv_id"] == tid
        msgs = (await c.get(f"/api/v1/conversations/{tid}/messages")).json()["items"]
        assert len(msgs) == 4

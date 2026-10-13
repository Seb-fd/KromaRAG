# 06 — Conversations & History

> Multi-turn conversational memory for KromaRAG. Closes the single-turn gap in `01`–`05`: defines thread/message entities, thread-level RBAC, history injection, lifecycle endpoints, and frontend migration from `localStorage` to API-persisted threads. **Spec-first, no implementation code** — schemas, contracts, and rules only.

- **Status:** Draft (Build mode, approved 2026-09-16)
- **Refs:** `01 §2–§4` (data flow, `kroma_*` schema, RBAC), `02 §1–§5` (endpoints, Pydantic, SSE, JWT), `03 §5–§6` (LangGraph nodes, config knobs), `04 §1–§2` (tracing, usage)
- **Code truth:** `migrations/001_kroma_schema.sql:47-60`, `app/schemas/__init__.py:13-17`, `app/api/v1/query.py:41-173`, `app/agent/graph.py:31-50`, `web/lib/api-client.ts:67-79`, `web/lib/store.tsx:63-98`

---

## 1. Requirements & User Stories

### 1.1 Feature Objective

Eliminate stateless isolation: users ask follow-ups, refine retrieval context over extended sessions, and keep distinct project chat histories without re-uploading or re-explaining context. Guarantee strict multi-tenant isolation (`owner_sub` scoping at SQL layer), cap token inflation via sliding-window truncation, and survive SSE disconnects via replay.

### 1.2 Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-01 | Authenticated `viewer+` can **create** a thread explicitly (`POST /conversations`) or **implicitly** on first `POST /query` without `conv_id` (server mints UUIDv4). |
| FR-02 | Thread owner can **list** own threads paginated, newest-first (`GET /conversations?limit&offset`), with `message_count` and `updated_at`. |
| FR-03 | Thread owner can **get** one thread meta (`GET /conversations/{id}`) and **fetch messages** paginated (`GET /conversations/{id}/messages?limit&before_id`). |
| FR-04 | Thread owner can **rename** via `PATCH /conversations/{id} {title}`. Server **auto-titles** on first user turn (see §4.3). |
| FR-05 | Thread owner can **hard-delete** thread (`DELETE /conversations/{id}` → cascade messages). No soft-delete in Phase 1. |
| FR-06 | **Persist ALL turns** — both `direct` (greeting/smalltalk) and `retrieval` routes — as a `(user, assistant)` message pair per query, in order, only after terminal `done` (or non-stream JSON response). |
| FR-07 | On each `POST /query` with `conv_id`, server **loads verbatim history** (last-N turns, §4.4) and injects it into the generator prompt **after chunks, before current query**. History MUST NOT alter retrieval embedding or RBAC filter. |
| FR-08 | `POST /query` with explicit `conv_id` MUST validate ownership; non-owner / nonexistent → `404 NOT_FOUND` (leak-safe, never `403` for thread existence). |
| FR-09 | SSE contract unchanged (`source` exactly once → `token` 0..N → `done` exactly once with stable `conv_id`); client reconnect replays via `GET .../messages` (no duplicate writes). |
| FR-10 | Frontend migrates `store.tsx` from `localStorage kromarag.convs.v1` (client `uid()`) to API threads with offline fallback (local cache read-through, server authoritative). |

### 1.3 User Stories

- **US-1 Follow-up:** As a viewer, I send "What is KromaRAG?" then "How does its fallback work?" in the same thread so the second answer uses prior context without me repeating the topic.
- **US-2 Projects:** As an editor, I keep separate threads per project so histories never interleave.
- **US-3 Resume:** As a viewer, I reload the app / lose SSE mid-stream and see the full thread replayed from the server.
- **US-4 Privacy:** As a viewer, I cannot list, read, query, or infer the existence of another user's thread (uniform `404`).
- **US-5 Cleanup:** As a viewer, I delete a thread and all its messages disappear for all my devices.

### 1.4 Non-Functional Requirements

| Category | Target |
|----------|--------|
| Performance | History load (`SELECT` last-N) p95 `< 150ms` @ 10K msgs/thread; retrieval p95 `< 400ms` unchanged (`01`/`03` budgets preserved); history injection adds `≤ 15%` prompt tokens vs zero-history baseline. |
| Scaling | Threads/messages scale horizontally (stateless API); keyset pagination only (no `OFFSET` scan on messages beyond `limit ≤ 100`); indexes in §2.2 required. |
| Security | `user_sub` = JWT `sub` (validated by `get_current_principal`); every thread/message SQL carries `user_sub` predicate or owner-join; never log `sub` raw (hash) / content excerpts `≤ 500` chars per `04 §1`; `404` on cross-tenant access. |
| Reliability | Message pair written atomically (single transaction); `trace_id` idempotency key prevents double-write on retry/reconnect; SSE disconnect mid-`token` still yields exactly-once `done` semantics via replay (either full pair persisted or nothing). |
| Observability | LangSmith span `history.load{msg_count,truncated}` + `done.usage` includes `history_tokens`; `log_usage` extended with `history_tokens` (additive, no breaking change). |
| Configurability | `HISTORY_MAX_TURNS=10`, `HISTORY_MAX_CHARS=6000`, `HISTORY_MAX_TOKENS=1500` (env-overridable, §2.4). Summarization explicitly out of scope (deferred). |

---

## 2. Data Models / Schemas

### 2.1 Current Baseline (frozen, `001`)

```sql
-- 001 (exists, DO NOT rewrite):
CREATE TABLE kroma_conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_sub TEXT NOT NULL,
  title TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE kroma_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conv_id UUID NOT NULL REFERENCES kroma_conversations(id) ON DELETE CASCADE,
  role TEXT,            -- unconstrained, nullable (gap)
  content TEXT,         -- nullable (gap)
  trace_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

Gaps closed by this spec: no `updated_at`, no indexes, no role/content constraints, no token accounting, no idempotency guard.

### 2.2 Migration `002_kroma_conversations.sql` (idempotent DDL contract)

```sql
-- 002 — history hardening. Idempotent; runs via psql or app.services.migrate.
ALTER TABLE kroma_conversations
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
-- Backfill then keep via trigger/app UPDATE on each message write:
-- UPDATE kroma_conversations SET updated_at = now() WHERE id = $1;

ALTER TABLE kroma_messages
  ADD COLUMN IF NOT EXISTS prompt_tokens INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS completion_tokens INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS route TEXT NOT NULL DEFAULT 'retrieval';
  -- route ∈ {'direct','retrieval'}; informational, mirrors AgentState.route.

-- Constrain role/content (enforce going forward; backfill NULLs first):
-- UPDATE kroma_messages SET role='user' WHERE role IS NULL;
-- UPDATE kroma_messages SET content='' WHERE content IS NULL;
ALTER TABLE kroma_messages
  ADD CONSTRAINT kroma_messages_role_chk CHECK (role IN ('user','assistant','system'));
ALTER TABLE kroma_messages
  ALTER COLUMN role SET NOT NULL,
  ALTER COLUMN content SET NOT NULL;

-- Idempotency: one assistant row per trace (prevents double-write on reconnect).
CREATE UNIQUE INDEX IF NOT EXISTS kroma_messages_trace_uidx
  ON kroma_messages (trace_id) WHERE trace_id IS NOT NULL;

-- Access + pagination indexes:
CREATE INDEX IF NOT EXISTS kroma_conversations_user_updated_idx
  ON kroma_conversations (user_sub, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_messages_conv_created_idx
  ON kroma_messages (conv_id, created_at ASC, id ASC);
```

Notes:

- No RLS in Phase 1 (app-layer `user_sub` predicates normative, per `01 §4` pattern). RLS deferred to prod-hardening spec.
- `VECTOR(768)` / `ivfflat` / `GIN` untouched.
- `kroma_usage` extension (additive, optional Phase 1): `ALTER TABLE kroma_usage ADD COLUMN IF NOT EXISTS history_tokens INT NOT NULL DEFAULT 0;` — if table absent, log to stdout JSON only.

### 2.3 Pydantic v2 DTOs (additive to `02 §2`, `app/schemas`)

```python
class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=120)

class ConversationMeta(BaseModel):
    id: UUID; title: str; created_at: datetime | None = None
    updated_at: datetime | None = None; message_count: int = 0

class ConversationList(BaseModel):
    items: list[ConversationMeta]; total: int

class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)

class Message(BaseModel):
    id: UUID; role: Literal["user", "assistant", "system"]
    content: str; trace_id: str | None = None
    created_at: datetime | None = None

class MessageList(BaseModel):
    items: list[Message]  # ascending by (created_at, id)
    next_before_id: UUID | None = None  # keyset cursor; None = oldest reached

class QueryRequest(BaseModel):  # EXTENDS 02 §2, additive
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)
    stream: bool = True
    conv_id: UUID | None = None  # semantics tightened: MUST be owned else 404
```

Validation: `ConversationRename.title` stripped, blank → `422 VALIDATION`; `MessageList` `limit` default `50`, clamped `1–100`; `before_id` must be UUID belonging to same thread else `404`.

### 2.4 TypeScript Types (`apps/web/lib`, additive)

```typescript
export type ConversationMeta = {
  id: string; title: string;
  created_at: string | null; updated_at: string | null;
  message_count: number;
};
export type ThreadMessage = {
  id: string; role: "user" | "assistant" | "system";
  content: string; trace_id: string | null; created_at: string | null;
};
```

### 2.5 Agent State Extension (`app/agent/graph.py`)

```python
class AgentState(TypedDict, total=False):
    # ... existing keys unchanged (query, principal_sub, conv_id, chunks, ...)
    history: list[dict]  # [{role: "user"|"assistant", content: str}] verbatim, oldest→newest, capped §4.4
    history_tokens: int  # estimated prompt tokens contributed by history
```

History is **prompt-only** input to `node_generator`; `node_retriever` MUST NOT read `state["history"]` for embedding.

### 2.6 Config Knobs (env, extends `03 §6` + `app/core/config.py`)

```python
history_max_turns: int = 10      # HISTORY_MAX_TURNS — max (user+assistant) pairs injected
history_max_chars: int = 6000    # HISTORY_MAX_CHARS — hard char cap after turn cap
history_max_tokens: int = 1500   # HISTORY_MAX_TOKENS — estimated-token cap (cl100k or len//4 fallback)
```

Precedence on truncation: turn-count → char-cap (drop oldest first) → token-cap (drop oldest first). All three enforced server-side.

---

## 3. System Interface & Contracts

Base `{API_URL}/api/v1`. Auth `Authorization: Bearer <Clerk JWT (template kromarag)>` (`viewer+` all routes below). Errors `02 §4` shape `{"detail":{"code","message"}}` + `X-Trace-Id` header.

| Method & Path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `POST /api/v1/conversations` | `viewer+` | `ConversationCreate{title?}` | `201 ConversationMeta` | Explicit create; empty title → server auto `"New conversation"`, renamed on first turn. |
| `GET /api/v1/conversations?limit=20&offset=0` | `viewer+` | — | `ConversationList` | Owner-scoped, `ORDER BY updated_at DESC`; `limit` clamp `1–100`. |
| `GET /api/v1/conversations/{id}` | owner | — | `ConversationMeta` | Non-owner/nonexistent → `404 NOT_FOUND` (never `403`). |
| `PATCH /api/v1/conversations/{id}` | owner | `ConversationRename{title}` | `ConversationMeta` | Blank/overlong → `422`. |
| `DELETE /api/v1/conversations/{id}` | owner | — | `204` | Hard-delete cascade; idempotent (second call → `404`). |
| `GET /api/v1/conversations/{id}/messages?limit=50&before_id=` | owner | keyset cursor | `MessageList` ascending | `before_id` = exclusive upper bound; `next_before_id` = oldest returned id or `None`. |
| `POST /api/v1/query` (extended) | `viewer+` | `QueryRequest{query,top_k,stream,conv_id?}` | SSE (`02 §3`) or JSON | `conv_id` absent → mint + auto-title; present → ownership check → `404` if foreign/missing. `done` echoes stable `conv_id`. |

### 3.1 Ownership SQL Predicates (normative, never post-filter)

```sql
-- Single-thread guard (get/rename/delete/messages/query-with-conv_id):
SELECT id FROM kroma_conversations WHERE id = $1 AND user_sub = $2;
-- Miss → 404 {"code":"NOT_FOUND","message":"conversation not found"}.

-- List:
SELECT c.id, c.title, c.created_at, c.updated_at,
       (SELECT COUNT(*) FROM kroma_messages m WHERE m.conv_id = c.id) AS message_count
FROM kroma_conversations c WHERE c.user_sub = $1
ORDER BY c.updated_at DESC LIMIT $2 OFFSET $3;
-- + SELECT COUNT(*) ... WHERE user_sub=$1 AS total.

-- Message page (ascending, keyset):
SELECT id, role, content, trace_id, created_at FROM kroma_messages
WHERE conv_id = $1
  AND ($2::uuid IS NULL OR (created_at, id) < (SELECT created_at, id FROM kroma_messages WHERE id = $2))
  -- $2 must itself belong to conv_id else 404 (validate first).
ORDER BY created_at ASC, id ASC LIMIT $3;
-- Simpler compliant alternative: fetch DESC+LIMIT then reverse in Python.
```

### 3.2 SSE / JSON Deltas (extends `02 §3`, non-breaking)

- SSE frames unchanged: `source` (once, first) → `token` (0..N) → `done` (once). `done.data = {conv_id, trace_id, usage{prompt_tokens,completion_tokens,model}, model, fallback_used, eval}` — `conv_id` is now **stable server thread id** (minted or validated), safe for reconnect replay.
- `X-Trace-Id` header = per-query `trace_id`, also stored on assistant message for idempotency.
- Non-stream (`stream=false`) JSON gains `history_tokens: int` (additive): `{trace_id, conv_id, answer, chunks, usage, model, fallback_used, eval, history_tokens}`.
- Frontend `streamQuery` signature extended (additive): `streamQuery({query, conv_id?, jwt, top_k, signal, onSource, onToken, onDone, onError})` — body now includes `conv_id` when active thread exists.

### 3.3 Frontend Client Functions (`apps/web/lib/api-client.ts` contracts)

```typescript
createConversation(args: { title?: string; jwt?: string }): Promise<ConversationMeta>;
listConversations(jwt?: string, limit?: number, offset?: number): Promise<ConversationList>;
getConversation(jwt: string | undefined, id: string): Promise<ConversationMeta>;
renameConversation(jwt: string | undefined, id: string, title: string): Promise<ConversationMeta>;
deleteConversationThread(jwt: string | undefined, id: string): Promise<void>;
getMessages(jwt: string | undefined, convId: string, limit?: number, before_id?: string): Promise<MessageList>;
```

`store.tsx` migration: `createConversation/selectConversation/deleteConversation/appendMessage` become async server-backed (optimistic local echo + reconcile on `done`); `localStorage` retained as read-through cache only (`kromarag.convs.v1` namespaced, capped 50, server wins on conflict).

---

## 4. Business Logic & Edge Cases

### 4.1 Thread Resolution on `POST /query`

1. `conv_id` absent → `gen_random_uuid()` → `INSERT kroma_conversations(id, user_sub, title='New conversation')` → proceed.
2. `conv_id` present → ownership `SELECT ... WHERE id=$1 AND user_sub=$2` → miss → `404 NOT_FOUND` (uniform for foreign vs nonexistent; MUST NOT return `403` or distinct message).
3. `conv_id` malformed UUID → Pydantic `422 VALIDATION` (existing behavior preserved).
4. New thread + empty-history: `history=[]`, `history_tokens=0`.

### 4.2 Persistence (atomic pair write)

- Trigger: only after terminal success — SSE `done` path (`finally` in `run_query_events`) or non-stream return. Total-LLM-failure path (`FRIENDLY_LLM_DOWN`) still persists the pair (user turn + friendly assistant turn) so replay matches what the user saw. Exception-before-`source` path persists nothing and yields `source:[] + error + done` without DB write (no fabricated content).
- Transaction (single, ordered):
  1. `INSERT kroma_messages(conv_id, role='user', content=<trimmed query>, trace_id=NULL)` — user row carries no trace.
  2. `INSERT kroma_messages(conv_id, role='assistant', content=<final guarded answer>, trace_id=$trace, prompt_tokens, completion_tokens, model, route)` — `ON CONFLICT (trace_id) WHERE trace_id IS NOT NULL DO NOTHING` for idempotent retry.
  3. `UPDATE kroma_conversations SET updated_at=now()[, title=<auto-title if first turn>] WHERE id=$conv AND user_sub=$sub`.
- Both `direct` and `retrieval` routes persist (approved decision — continuity over token savings). `direct` rows carry `route='direct'`, empty chunks, PII-redacted content (same `guardrails` output the user saw).
- Concurrency: concurrent `POST /query` on same `conv_id` serialize on `SELECT ... FOR UPDATE` of the conversation row before history-load + write; order = commit order.

### 4.3 Auto-Title (server, approved)

- Condition: thread `title IS NULL OR title='New conversation'` AND this is the first persisted pair (`message_count` pre-write `== 0`).
- Algorithm (pure, deterministic): take first user `query.strip()` → first sentence (`split` on `[.!?\n]` take `[0]`) → collapse whitespace → truncate to **60 chars** (append `…` if cut) → fallback `"New conversation"` if empty.
- Client override anytime via `PATCH`; server never re-titles after first pair.

### 4.4 History Injection (verbatim last-N, approved)

- Loader: `SELECT role, content FROM kroma_messages WHERE conv_id=$1 ORDER BY created_at DESC, id DESC LIMIT (HISTORY_MAX_TURNS*2)` → reverse to oldest→newest. Include both `user` and `assistant` rows; exclude `system` (none written in Phase 1, ignored if present).
- Caps (drop oldest first): keep at most `HISTORY_MAX_TURNS` pairs → join and cut to `HISTORY_MAX_CHARS` (cut at message boundary, never mid-message) → estimate tokens (`tiktoken cl100k` if available else `ceil(chars/4)`) and drop oldest pairs until `≤ HISTORY_MAX_TOKENS`. Emit `history_tokens` estimate.
- Prompt placement (`node_generator`): `SYSTEM_PROMPT_V2` → `Valid citations allowlist + chunk context` → `\n\n## Conversation history (verbatim, oldest first, may be truncated):\n<turn>User: ...\nAssistant: ...</...>` → `Current user query: ...`. Explicit instruction: history is context only; answer ONLY from current accessible chunks; never cite history as `[doc:...]`; if history conflicts with chunks, chunks win.
- Retrieval isolation: `node_retriever` embeds **current `query` only**; `state["history"]` unread in retriever/reranker (unit-test enforced).
- No summarization in Phase 1. Oversized single message (> `HISTORY_MAX_CHARS`) truncated tail-side with `… [truncated]` marker.

### 4.5 Deletion (hard-delete, approved)

```sql
DELETE FROM kroma_conversations WHERE id = $1 AND user_sub = $2;
-- ON DELETE CASCADE removes kroma_messages. Missing row → 404.
```

No tombstone, no background job, no restore. In-flight `POST /query` holding the `conv_id` completes its stream but its trailing write becomes a no-op FK-safe skip (conversation gone → skip pair insert, still yield `done`).

### 4.6 Validation, Fallbacks, Error Handling

| Case | Behavior |
|------|----------|
| Missing/expired JWT, bad `iss/aud`, missing `sub` | `401 UNAUTHENTICATED` (per `02 §5`, unchanged). |
| `viewer` lists another user's thread id (guess UUID) | `404 NOT_FOUND` (leak-safe; identical body to truly-missing id). |
| `PATCH` blank title / `>120` chars | `422 VALIDATION`. |
| `GET messages` with foreign `before_id` | `404 NOT_FOUND`. |
| `POST /query` LLM total failure | SSE `error{code:LM_*} + friendly tokens + done`; pair persisted with friendly text; `trace_id` idempotent. |
| SSE disconnect (`request.is_disconnected()`) | Break token loop; still attempt pair write if answer finalized; client replays via `GET messages` — dedup by `trace_id`. |
| Retry with same `trace_id` (client-generated? No — server generates; retry = new query) | New `trace_id` → new pair. Duplicate delivery of same SSE `done` MUST NOT double-write (guard by unique `trace_id` index). |
| History table empty / thread brand-new | `history=[]`; prompt omits history section entirely (no empty header). |
| PII in history | History stores post-guardrail assistant text (already redacted) + raw user query (user-authored; never redact user words). Never log full content server-side beyond `≤500`-char excerpts. |

---

## 5. Acceptance Criteria (Gherkin)

```gherkin
Feature: Conversations & History

  Background:
    Given users "alice" and "bob" with valid JWTs (roles ["viewer"])
      And HYBRID weights 0.7/0.3 and citation format [doc:Title:ord] unchanged

  Scenario: Implicit thread creation on first query
    Given "alice" has no threads
    When "alice" POSTs /api/v1/query {"query":"What is KromaRAG?"} with no conv_id
    Then response is 200 SSE with exactly one "source" then zero or more "token" then one "done"
      And "done".conv_id is a UUIDv4
      And GET /api/v1/conversations contains 1 item titled from the first query truncated to 60 chars
      And GET /api/v1/conversations/{id}/messages contains 2 messages [user, assistant] in order

  Scenario: Follow-up uses verbatim history without changing retrieval
    Given "alice" thread T with prior turn "What is KromaRAG?"
    When "alice" POSTs /api/v1/query {"query":"How does its fallback work?","conv_id":"T"}
    Then "done".conv_id equals "T"
      And generator prompt contains the prior turn verbatim under "Conversation history"
      And retriever embedding input equals "How does its fallback work?" only
      And messages for T now total 4 in ascending order

  Scenario: Direct greeting turns persist
    When "alice" POSTs /api/v1/query {"query":"hello","conv_id":"T"}
    Then route is "direct"
      And a (user="hello", assistant=PII-redacted greeting) pair is persisted with route="direct"
      And next retrieval turn's history includes the greeting pair

  Scenario: Cross-tenant access is leak-safe 404
    Given thread T owned by "alice"
    When "bob" GETs /api/v1/conversations/T
      Or "bob" GETs /api/v1/conversations/T/messages
      Or "bob" POSTs /api/v1/query {"query":"hi","conv_id":"T"}
      Or "bob" DELETEs /api/v1/conversations/T
    Then response is 404 {"code":"NOT_FOUND"} in every case
      And response body is identical to a random nonexistent UUID
      And "bob" GET /api/v1/conversations does not contain T

  Scenario: Pagination replays oldest correctly
    Given thread T with 30 persisted messages
    When "alice" GETs /api/v1/conversations/T/messages?limit=10
    Then 10 oldest messages return ascending with next_before_id set
    When "alice" follows next_before_id cursors to the end
    Then all 30 messages replay in order with final next_before_id null

  Scenario: Sliding-window caps enforced
    Given HISTORY_MAX_TURNS=10 and HISTORY_MAX_CHARS=6000 and HISTORY_MAX_TOKENS=1500
      And thread T with 40 messages
    When "alice" POSTs /api/v1/query {"query":"summarize above","conv_id":"T"}
    Then injected history contains at most 10 pairs oldest-dropped-first
      And injected chars <= 6000 and estimated tokens <= 1500
      And done payload reports history_tokens matching the estimate

  Scenario: Rename and hard-delete lifecycle
    When "alice" PATCHes /api/v1/conversations/T {"title":"Project X"}
    Then GET returns title "Project X"
    When "alice" DELETEs /api/v1/conversations/T
    Then response is 204
      And GET /api/v1/conversations/T is 404
      And GET /api/v1/conversations/T/messages is 404
      And kroma_messages holds zero rows for T
      And second DELETE returns 404 (idempotent)

  Scenario: Reconnect does not duplicate
    Given SSE for query Q in thread T disconnected mid-token with trace_id R
    When client GETs /api/v1/conversations/T/messages
    Then either 0 new rows (write skipped, safe retry with new trace) or exactly 1 pair with trace_id R
      And re-delivery of done for R never creates a second assistant row (unique trace index)

  Scenario: Auto-title only on first turn
    Given new thread T titled "New conversation"
    When first query "Explain hybrid search 0.7 cosine plus 0.3 ts_rank in detail please?" persists
    Then title becomes first-sentence truncated to 60 chars
    When second query persists
    Then title is unchanged
    When "alice" PATCHes a custom title
    Then subsequent queries never overwrite it
```

### Exit Gates (Phase mapping, `05` cross-cutting)

- `uv run pytest -q` green incl. new `tests/test_conversations.py` (ownership matrix, pagination, truncation caps, idempotent write, retriever-isolation).
- `npm run build --workspace apps/web` green; E2E login→create→query→follow-up→reload-replay→rename→delete passes; Lighthouse `≥ 85` unaffected.
- `npm run spec:check` green (this file additive; `02` normative claims untouched).
- Manual: cross-user `404` uniformity diffed byte-for-byte; disconnect-then-replay shows no duplicate pair.

---

## Appendix — Normative Deltas Index (for reviewers)

- **SQL:** `002` adds `updated_at`, token/model/route cols, `role` CHECK + NOT NULLs, `trace_uidx`, 2 composite indexes. Deletes cascade.
- **API:** 6 endpoints (5 new + `POST /query` tightened). All thread reads/writes owner-scoped. `404`-not-`403` on thread existence.
- **Agent:** `AgentState.history/history_tokens`; loader + truncation helper (signatures only — implementation follows in code PR).
- **Web:** `api-client.ts` +6 fns, `streamQuery` gains `conv_id`; `store.tsx` server-authoritative.
- **Out of scope:** summarization/compression, sharing/collaboration on threads, RLS policies, retention/TTL, per-thread `top_k` overrides, message edit/regenerate.

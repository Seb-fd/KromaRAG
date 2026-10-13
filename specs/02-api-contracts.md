# 02 — KromaRAG API Contracts

> Frozen OpenAPI/FastAPI signatures, SSE format, Pydantic schemas, Clerk JWT claims. `apps/api` must implement exactly.

Base: `{API_URL}/api/v1`. Auth: `Authorization: Bearer <Clerk JWT (template kromarag)>` except `/health`, `/ready`.

## 1. Endpoints

| Method & Path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `GET /health` | none | — | `200 {"status":"ok","service":"kromarag-api","version":"0.1.0"}` | liveness, no DB |
| `GET /ready` | none | — | `200 {"ready":true,"db":"ok"}` / `503` | checks `SELECT 1` via asyncpg pool |
| `POST /api/v1/ingest` | `editor+` | `IngestRequest` (multipart or JSON) | `202 {"doc_id":"uuid","status":"pending\|ready","chunks":N}` | large → background |
| `POST /api/v1/query` | `viewer+` | `QueryRequest` | `text/event-stream` SSE | see §3 |
| `GET /api/v1/documents` | `viewer+` | `?limit=20&offset=0` | `{"items":[DocMeta],"total":N}` | RBAC-filtered |
| `GET /api/v1/documents/{id}` | owner or role | — | `DocMeta` | 403/404 |
| `DELETE /api/v1/documents/{id}` | `admin` or owner+editor | — | `204` | cascade chunks |

FastAPI router prefix `/api/v1`, tags `ingest|query|documents|system`.

## 2. Pydantic Schemas (v2)

```python
class Principal(BaseModel):  # from JWT, not client-supplied
    sub: str; email: EmailStr | None = None; roles: list[str] = []

class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)
    stream: bool = True
    conv_id: UUID | None = None

class ChunkResult(BaseModel):
    chunk_id: UUID; doc_id: UUID; title: str; content: str; score: float; ord: int

class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300); content: str | None = Field(default=None, max_length=200000); url: str | None = Field(default=None, max_length=2000)
    mime: str = "text/plain"; allowed_roles: list[str] = ["viewer"]

class DocMeta(BaseModel):
    id: UUID; title: str; mime: str; status: str; chunks: int; created_at: datetime
```

Validation: `allowed_roles ⊆ {admin,editor,viewer}`; `query` stripped; `top_k` capped server-side. `IngestRequest.title 1–300` (stripped, blank rejected), `content ≤200000` chars, `url ≤2000` chars http/https only (SSRF guard `08 SEC-06`); `DocumentUpdate.uri ≤2000` http/https only; `content` empty after cleaning → `422`. Codes add `URL_FETCH_BLOCKED/URL_FETCH_FAILED(422)`.

## 3. SSE Streaming Format (`POST /api/v1/query`)

Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Trace-Id: <uuid>`.

```
event: source
data: {"chunks":[{"chunk_id":"…","doc_id":"…","title":"…","score":0.87}]}

event: token
data: {"delta":"Hello"}

event: done
data: {"conv_id":"…","trace_id":"…","usage":{"prompt_tokens":412,"completion_tokens":98,"model":"gemini-3.6-flash"}}

event: error
data: {"code":"LLM_TIMEOUT","message":"fallback to groq"}
```

Rules: `source` first (exactly once), `token` 0..N, `done` exactly once. Heartbeat `: ping` every 15s. Client (`apps/web/lib/api-client.ts`) uses `fetch` + `ReadableStream`, not `EventSource` (POST body).

## 4. Errors

```json
{"detail":{"code":"FORBIDDEN","message":"requires role editor"}}
```
Codes: `UNAUTHENTICATED(401)`, `FORBIDDEN(403)`, `NOT_FOUND(404)`, `VALIDATION(422)`, `LLM_TIMEOUT(504)`, `DB_UNAVAILABLE(503)`. Always include `trace_id` header.

## 5. Auth — Clerk JWT Payload / Claims (normative)

Clerk session JWT with the custom `kromarag` template (configured in the Clerk
dashboard per app: `kromarag-dev`, `kromarag-prod`): `email` =
`{{user.primary_email_address}}`, `roles` = `{{user.public_metadata.roles}}`,
`aud` = `kromarag-web`, lifetime 60s. Roles live in `publicMetadata.roles`
(Dashboard → Users → Metadata; default `["viewer"]`); the browser resolves a
fresh token per request via `getToken({ template: "kromarag" })`
(`apps/web/lib/kroma-token.ts`) and sends it as `Authorization: Bearer`.

**Required claims — backend MUST validate:**

```jsonc
{
  "sub": "user_2abcXYZ123",              // ★ Clerk user id → kroma owner_sub / user_sub (required, else 401)
  "email": "admin@kromarag.com",         // top-level from the template (expected)
  "roles": ["viewer"],                   // ★ authoritative RBAC array, ⊆ ["admin","editor","viewer"]
  "iss": "https://kromarag-prod.clerk.accounts.dev", // == JWT_ISSUER (dev: kromarag-dev issuer; local dev-bypass: http://localhost:3000)
  "aud": "kromarag-web",                 // == JWT_AUDIENCE (template carries it)
  "exp": 1726400000,                     // expiry, skew ±60s
  "iat": 1726396400,
  "jti": "uuid"
}
```

One-time identity note: Clerk `sub` (`user_xxx`) replaces the old
`demo|email` / `google-oauth2|…` subs. Rows keyed by old subs (`owner_sub` /
`user_sub` on docs, threads, messages) orphan on cutover — run the one-off
backfill (`UPDATE … SET owner_sub = map …` joining old `demo|email` → Clerk
`user_id` by email from the Clerk user export; new users start clean).

**`apps/api/app/core/security.py` contract (`get_current_principal` + `require_role`):**
1. Parse `Bearer`; fetch JWKS from `{JWT_ISSUER}/.well-known/jwks.json` (explicit `CLERK_JWKS_URL` / `JWT_JWKS_URL` wins; cache 600s + prev-set fallback).
2. Verify sig (`RS256` via JWKS `kid`; `HS256` dev-bypass only when NOT prod — prod rejects kid-less tokens outright), then `iss`, `aud`, `exp`.
3. Normalize: `roles = claims.get("roles") or claims.get("public_metadata",{}).get("roles",[])`, keep only `admin|editor|viewer`; default `["viewer"]` if empty and `sub` present; missing `sub` → 401.
4. Return `Principal(sub,email,roles)`. `require_role("editor")` checks hierarchy `admin>editor>viewer`, else 403.
5. How `roles` get into token: Clerk dashboard `publicMetadata.roles` per user (ops click path documented in README/deploy runbook); the `kromarag` JWT template stamps them into every session token — no web DB join.

Example denial: user `roles:["viewer"]` → `POST /ingest` → `403 {"code":"FORBIDDEN"}`.

## 6. Versioning

Breaking contract change → bump `version` in `/health` + update this spec first (SDD). Additive fields allowed without bump.

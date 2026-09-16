# 02 — KromaRAG API Contracts

> Frozen OpenAPI/FastAPI signatures, SSE format, Pydantic schemas, Auth.js JWT claims. `apps/api` must implement exactly.

Base: `{API_URL}/api/v1`. Auth: `Authorization: Bearer <Auth.js JWT>` except `/health`, `/ready`.

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
    title: str; content: str | None = None; url: str | None = None
    mime: str = "text/plain"; allowed_roles: list[str] = ["viewer"]

class DocMeta(BaseModel):
    id: UUID; title: str; mime: str; status: str; chunks: int; created_at: datetime
```

Validation: `allowed_roles ⊆ {admin,editor,viewer}`; `query` stripped; `top_k` capped server-side.

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

## 5. Auth — Auth.js JWT Payload / Claims (normative)

Auth.js v5 JWT (JWE-decrypted by NextAuth, re-signed as JWS for API — configured in `apps/web/auth.ts` `jwt.encode` with `AUTH_SECRET`, `HS256` or `RS256` via JWKS).

**Required claims — backend MUST validate:**

```jsonc
{
  "sub": "google-oauth2|108241234567890", // stable user id → kroma owner_sub / user_sub
  "email": "admin@kromarag.com",           // optional but expected
  "name": "Sebastian Florez",
  "roles": ["viewer"],                   // ★ authoritative RBAC array, ⊆ ["admin","editor","viewer"]
  "iss": "http://localhost:3000",        // == JWT_ISSUER (prod: https://kromarag.vercel.app)
  "aud": "kromarag-web",                 // == JWT_AUDIENCE
  "exp": 1726400000,                     // expiry, skew ±60s
  "iat": 1726396400,
  "jti": "uuid"
}
```

**`apps/api/app/core/security.py` contract (`get_current_principal` + `require_role`):**
1. Parse `Bearer`; fetch JWKS from `{JWT_ISSUER}/.well-known/jwks.json` (cache 600s).
2. Verify sig (`HS256` with `AUTH_SECRET` OR `RS256` via JWKS `kid`), then `iss`, `aud`, `exp`.
3. Normalize: `roles = [r for r in claims.get("roles",[]) if r in {"admin","editor","viewer"}]`; default `["viewer"]` if empty and `sub` present; missing `sub` → 401.
4. Return `Principal(sub,email,roles)`. `require_role("editor")` checks hierarchy `admin>editor>viewer`, else 403.
5. How `roles` get into token: Auth.js `jwt({token,user})` callback joins `kroma_user_roles` (via `DATABASE_URL` lookup in web or baked at login) — see `apps/web/auth.ts` stub.

Example denial: user `roles:["viewer"]` → `POST /ingest` → `403 {"code":"FORBIDDEN"}`.

## 6. Versioning

Breaking contract change → bump `version` in `/health` + update this spec first (SDD). Additive fields allowed without bump.

"""Centralized settings. DB is fully generic via DATABASE_URL (Neon/Postgres).

Implements specs/08 §2.6 + §6: prod knobs, dual-secret rotation, rate limits,
pool/timeout budgets. Backward compatible with AUTH_SECRET (legacy single-secret).
"""
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env", "../../.env"), extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/kromarag"
    database_url_sync: str = ""
    pgvector_dim: int = 768

    google_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    embedding_model: str = "gemini-embedding-001"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    llm_primary: str = "gemini"
    llm_fallback: str = "groq"

    langchain_tracing_v2: bool = False
    langsmith_api_key: str = ""
    langchain_project: str = "KromaRAG"
    langchain_endpoint: str = "https://api.smith.langchain.com"

    # ── Auth secrets: dual-secret overlap (specs/08 §3.3). Legacy AUTH_SECRET kept. ──
    # Default stays an obvious non-secret that still trips the prod guard
    # (startswith "change-me") but is ≥32 chars so dev/test JWT round-trips
    # don't emit PyJWT InsecureKeyLengthWarning noise.
    auth_secret: str = "change-me-dev-only-override-in-production"
    auth_secret_current: str = ""
    auth_secret_previous: str = ""
    jwt_issuer: str = "http://localhost:3000"
    jwt_audience: str = "kromarag-web"
    # ── JWKS fetch control (HS256-only dev must not storm Next.js /.well-known) ──
    # Empty = derive {jwt_issuer}/.well-known/jwks.json (prod RS256 path).
    jwt_jwks_url: str = ""
    # False = never fetch JWKS (pure HS256). Default True so prod RS256 works;
    # _get_jwks() still self-skips loopback issuers without an explicit URL.
    jwt_jwks_enabled: bool = True

    cors_origins: list[str] = ["http://localhost:3000"]

    top_k_default: int = 6

    # ── Phase 2: RAG knobs (specs/03 §6) ──
    chunk_tokens: int = 600
    chunk_overlap: int = 90
    chunk_max_per_doc: int = 2000
    hybrid_vector_w: float = 0.7
    hybrid_text_w: float = 0.3
    score_min: float = 0.25
    llm_timeout_s: int = 25
    embed_batch: int = 32

    # ── Phase 1: Conversations & history (specs/06 §2.6) ──
    history_max_turns: int = 10
    history_max_chars: int = 6000
    history_max_tokens: int = 1500

    # ── Spec 07: Document lifecycle (specs/07 §2.6) ──
    doc_upload_max_bytes: int = 20_971_520  # DOC_UPLOAD_MAX_BYTES — server multipart cap (20 MB)
    doc_web_max_bytes: int = 2_000_000  # DOC_WEB_MAX_BYTES — web guard (existing 2 MB)
    url_max_bytes: int = 50_000  # URL_MAX_BYTES — unchanged
    doc_reindex_async: bool = True  # DOC_REINDEX_ASYNC — False only for tests (sync inline)

    # ── Spec 08: Production hardening (specs/08 §2.6 + §6) ──
    web_concurrency: int = 1
    pool_min_size: int = 2
    pool_max_size: int = 20
    pool_command_timeout: int = 10  # POOL_COMMAND_TIMEOUT_S
    rate_limit_query_per_min: int = 100
    rate_limit_ingest_per_min: int = 20
    rate_limit_unauth_per_min: int = 10
    rate_limit_admin_per_min: int = 300
    rate_limit_read_per_min: int = 300
    llm_fallback_timeout_s: int = 12
    embed_timeout_s: int = 8
    retriever_timeout_ms: int = 3000

    # ── Spec 09: Telemetry & operations (specs/09 §2.5) ──
    otel_enabled: bool = True  # OTEL_ENABLED — W3C join/inject layer on/off
    otel_sampling_ok: float = 0.1  # OTEL_SAMPLING_OK — success trace sample rate
    otel_sampling_error: float = 1.0  # OTEL_SAMPLING_ERROR — error sample rate (keep 1.0)
    metrics_enabled: bool = True  # METRICS_ENABLED — serve GET /metrics (dual-gated)
    metrics_scrape_allowlist: str = ""  # METRICS_SCRAPE_ALLOWLIST — comma-separated IP/CIDR
    chaos_sql_delay_ms: int = 0  # CHAOS_SQL_DELAY_MS — test/staging only, forced 0 in prod
    chaos_gemini_down: bool = False  # CHAOS_GEMINI_DOWN — force primary failure (staging chaos)
    chaos_pool_max: int = 0  # CHAOS_POOL_MAX — override pool max in chaos runs (0 = follow POOL_MAX_SIZE)
    k6_query_vus: int = 20  # K6_QUERY_VUS
    k6_upload_vus: int = 5  # K6_UPLOAD_VUS

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v: Any) -> Any:
        # Accept JSON array, comma-separated string, or list (dev + prod .env styles).
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    import json as _json

                    parsed = _json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [p.strip().strip('"').strip("'") for p in s.split(",") if p.strip().strip('"').strip("'")]
        return v

    @property
    def is_prod(self) -> bool:
        return (self.env or "").lower() == "production"

    @property
    def effective_current_secret(self) -> str:
        """Signing secret: AUTH_SECRET_CURRENT, fallback to legacy AUTH_SECRET."""
        return self.auth_secret_current or self.auth_secret

    @property
    def effective_verify_secrets(self) -> list[str]:
        """Verify order: CURRENT then PREVIOUS (specs/08 §3.3). Legacy fallback included."""
        secrets: list[str] = []
        cur = self.auth_secret_current or self.auth_secret
        if cur:
            secrets.append(cur)
        if self.auth_secret_previous and self.auth_secret_previous not in secrets:
            secrets.append(self.auth_secret_previous)
        # Legacy AUTH_SECRET doubles as previous when rotation fields partially set.
        if self.auth_secret and self.auth_secret not in secrets:
            secrets.append(self.auth_secret)
        return [s for s in secrets if s]

    def validate_prod_guards(self) -> None:
        """Fail-closed boot checks (specs/08 §6). No-op unless ENV=production."""
        if not self.is_prod:
            return
        cur = self.effective_current_secret
        if not cur or len(cur) < 32 or cur.startswith("change-me"):
            raise RuntimeError("auth_secret_invalid: AUTH_SECRET_CURRENT must be set (≥32 chars, non-default) in production")
        origins = [o.strip() for o in (self.cors_origins or [])]
        if not origins:
            raise RuntimeError("cors_invalid: CORS_ORIGINS must not be empty in production")
        if any(o == "*" for o in origins):
            raise RuntimeError("cors_wildcard_rejected: CORS_ORIGINS must not contain '*' in production")
        if not (self.jwt_issuer or "").startswith("https://"):
            raise RuntimeError("jwt_issuer_invalid: JWT_ISSUER must be https:// in production")
        url = self.database_url or ""
        # specs/08 §2.4 + §6 RES-03: prod runtime connects ONLY via Neon pooled
        # host with TLS. Both predicates are fail-closed (release decision 2026-09-16).
        if "-pooler" not in url:
            raise RuntimeError("database_url_invalid: DATABASE_URL must use Neon pooled host ('-pooler') in production")
        if "sslmode=require" not in url:
            raise RuntimeError("database_url_invalid: DATABASE_URL must include sslmode=require in production")
        # Spec 09: chaos flags are staging-only — fail closed in production.
        if (self.chaos_sql_delay_ms or 0) != 0:
            raise RuntimeError("chaos_enabled_in_production: CHAOS_SQL_DELAY_MS must be 0 in production")
        if bool(self.chaos_gemini_down):
            raise RuntimeError("chaos_enabled_in_production: CHAOS_GEMINI_DOWN must be off in production")
        if (self.chaos_pool_max or 0) != 0:
            raise RuntimeError("chaos_enabled_in_production: CHAOS_POOL_MAX must be 0 in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

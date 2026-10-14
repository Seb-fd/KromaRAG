"""Production hardening tests (specs/08 §7): rate limits, Clerk kid-rollover,
security headers, health/readyz probes, fail-closed boot guards. Offline (fakes)."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from app.core import rate_limit as rl
from app.core import security as sec
from app.core.config import Settings, settings
from app.main import app
from tests._clerk import (
    CLERK_KID,
    CLERK_PREV_KID,
    CLERK_TEST_AUD,
    CLERK_TEST_ISS,
    clerk_token,
    hs256_token,
    install_clerk,
    jwks_list,
)


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unsigned_token(sub: str = "u1", roles: list[str] | None = None) -> str:
    """JWT-shaped token with decodable payload (signature never verified for bucketing)."""
    roles = roles if roles is not None else ["viewer"]
    return f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url({'sub': sub, 'roles': roles})}.sig"


def _signed_token(secret: str, sub: str = "u1", roles: list[str] | None = None) -> str:
    """Legacy HS256 helper — dev-bypass only (non-prod). Kept for the single
    non-prod test proving local bypass still verifies; all other auth tests
    use Clerk RS256 via tests/_clerk.py."""
    return hs256_token(secret, sub=f"demo|{sub}@x.com", roles=roles)


async def _no_jwks() -> list[dict]:
    return []


# ── Probes ──

@pytest.mark.asyncio
async def test_healthz_alias_matches_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        h1 = await c.get("/health")
        h2 = await c.get("/healthz")
    assert h1.status_code == 200 and h2.status_code == 200
    assert h1.json() == h2.json() == {"status": "ok", "service": "kromarag-api", "version": "0.1.0"}


@pytest.mark.asyncio
async def test_readyz_alias_matches_ready():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.get("/ready")
        r2 = await c.get("/readyz")
    assert r1.status_code in (200, 503) and r1.status_code == r2.status_code
    assert r1.json().keys() == r2.json().keys()
    body = r2.json()
    assert "ready" in body and "db" in body and "pool" in body and "version" in body


# ── Security headers ──

@pytest.mark.asyncio
async def test_security_headers_present():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
        q = await c.post("/api/v1/query", json={"query": "hi"})
    for resp in (r, q):
        assert resp.headers.get("Strict-Transport-Security", "").startswith("max-age=63072000")
        assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "server" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_security_headers_on_rate_limited(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_query_per_min", 1)
    tok = _unsigned_token("rl-hdr")
    hdr = {"Authorization": f"Bearer {tok}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/query", json={"query": "one"}, headers=hdr)
        r = await c.post("/api/v1/query", json={"query": "two"}, headers=hdr)
    assert r.status_code == 429
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


# ── Rate limiting ──

@pytest.mark.asyncio
async def test_rate_limit_query_breach(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_query_per_min", 2)
    tok = _unsigned_token("rl-query")
    hdr = {"Authorization": f"Bearer {tok}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/api/v1/query", json={"query": "one"}, headers=hdr)
        r2 = await c.post("/api/v1/query", json={"query": "two"}, headers=hdr)
        r3 = await c.post("/api/v1/query", json={"query": "three"}, headers=hdr)
    assert r1.status_code != 429 and r2.status_code != 429
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "RATE_LIMITED"
    assert int(r3.headers["Retry-After"]) >= 1
    assert r3.headers["X-RateLimit-Remaining"] == "0"
    assert r3.headers["X-RateLimit-Limit"] == "2"
    assert r3.headers["X-Trace-Id"]
    # Allowed traffic carries informative headers too.
    assert r1.headers["X-RateLimit-Limit"] == "2"


@pytest.mark.asyncio
async def test_rate_limit_ingest_bucket(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_ingest_per_min", 2)
    tok = _unsigned_token("rl-ingest")
    hdr = {"Authorization": f"Bearer {tok}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/ingest", json={"title": "a", "content": "x"}, headers=hdr)
        await c.post("/api/v1/ingest", json={"title": "b", "content": "x"}, headers=hdr)
        r3 = await c.post("/api/v1/ingest", json={"title": "c", "content": "x"}, headers=hdr)
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_rate_limit_unauth_ip_bucket(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_unauth_per_min", 2)
    hdr = {"Authorization": "Bearer opaque-not-a-jwt"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/query", json={"query": "one"}, headers=hdr)
        await c.post("/api/v1/query", json={"query": "two"}, headers=hdr)
        r3 = await c.post("/api/v1/query", json={"query": "three"}, headers=hdr)
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "RATE_LIMITED"
    assert int(r3.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_probes_exempt_from_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_unauth_per_min", 1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(3):
            assert (await c.get("/healthz")).status_code == 200
            r = await c.get("/readyz")
            assert r.status_code in (200, 503)  # never 429


def test_rate_limit_unit_sliding_window():
    rl.reset_rate_limiter()
    ok1, rem1, _ = rl.check_limit("unit:k", 2)
    ok2, rem2, _ = rl.check_limit("unit:k", 2)
    ok3, rem3, reset = rl.check_limit("unit:k", 2)
    assert (ok1, ok2, ok3) == (True, True, False)
    assert (rem1, rem2, rem3) == (1, 0, 0)
    assert reset >= 1


# ── Clerk kid-rollover (replaces dual-secret HS256 rotation) ──

@pytest.mark.asyncio
async def test_clerk_kid_rollover_current_and_prev(monkeypatch):
    """Current kid verifies; prev-set kid verifies via fallback; unknown kid 401s."""
    install_clerk(monkeypatch, sec, settings, current_kids=[CLERK_KID])
    # Point the prev-set at the older key (rotation overlap).
    monkeypatch.setattr(
        sec, "_jwks_prev", {"keys": jwks_list([CLERK_PREV_KID]), "fetched_at": 0.0}
    )
    cur_tok = clerk_token(sub="user_alice", roles=["viewer"])
    p = await sec.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=cur_tok)
    )
    assert p.sub == "user_alice" and p.roles == ["viewer"]
    assert p.email and "@" in p.email

    prev_tok = clerk_token(sub="user_bob", roles=["editor"], kid=CLERK_PREV_KID)
    p2 = await sec.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=prev_tok)
    )
    assert p2.sub == "user_bob" and p2.roles == ["editor"]

    # Unknown kid (fresh key never published) still rejected.
    import time as _time
    import uuid as _uuid

    import jwt as _jwt

    from tests._clerk import _ensure_keypair

    _ensure_keypair("clerk-test-unknown")
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    _priv = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _pem = _priv.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()
    ).decode()
    now = int(_time.time())
    bad = _jwt.encode(
        {"sub": "user_mallory", "email": "m@example.com", "roles": ["viewer"],
         "iss": CLERK_TEST_ISS, "aud": CLERK_TEST_AUD,
         "exp": now + 3600, "iat": now, "jti": str(_uuid.uuid4())},
        _pem, algorithm="RS256", headers={"kid": "clerk-test-unknown"},
    )
    with pytest.raises(Exception):
        await sec.get_current_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=bad)
        )


@pytest.mark.asyncio
async def test_clerk_roles_from_public_metadata_fallback(monkeypatch):
    """Template `roles` absent → public_metadata.roles used; empty → viewer."""
    install_clerk(monkeypatch, sec, settings, current_kids=[CLERK_KID])
    _ = clerk_token(sub="user_pm", roles=None, public_metadata={"roles": ["admin"]})
    # clerk_token always sets email; drop top-level roles by re-minting without it.
    import time as _time2
    import uuid as _uuid2

    import jwt as _jwt2

    # Re-mint manually without `roles` to force the fallback path.
    from tests._clerk import _ensure_keypair

    _pem2 = _ensure_keypair(CLERK_KID)
    now2 = int(_time2.time())
    tok2 = _jwt2.encode(
        {"sub": "user_pm", "email": "pm@example.com",
         "public_metadata": {"roles": ["admin"]},
         "iss": CLERK_TEST_ISS, "aud": CLERK_TEST_AUD,
         "exp": now2 + 3600, "iat": now2, "jti": str(_uuid2.uuid4())},
        _pem2, algorithm="RS256", headers={"kid": CLERK_KID},
    )
    p = await sec.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok2)
    )
    assert p.roles == ["admin"]

    tok3 = clerk_token(sub="user_empty", roles=[])
    p3 = await sec.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok3)
    )
    assert p3.roles == ["viewer"]


@pytest.mark.asyncio
async def test_hs256_dev_bypass_still_verifies_locally(monkeypatch):
    """Single non-prod test proving the dev Credentials bypass still works locally."""
    install_clerk(monkeypatch, sec, settings, current_kids=[CLERK_KID], env="development")
    secret = "dev-bypass-secret-" + "x" * 24
    monkeypatch.setattr(settings, "auth_secret", secret)
    monkeypatch.setattr(settings, "auth_secret_current", "")
    monkeypatch.setattr(settings, "auth_secret_previous", "")
    # Dev bypass mints with the loopback issuer (never the Clerk issuer).
    monkeypatch.setattr(settings, "jwt_issuer", "http://localhost:3000")
    tok = hs256_token(secret, sub="demo|bob@example.com", roles=["editor"])
    p = await sec.get_current_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)
    )
    assert p.sub == "demo|bob@example.com" and p.roles == ["editor"]


@pytest.mark.asyncio
async def test_prod_rejects_hs256_dev_bypass(monkeypatch):
    """Prod physically cannot accept kid-less HS256 bypass tokens."""
    install_clerk(
        monkeypatch, sec, settings,
        current_kids=[CLERK_KID], iss="https://clerk.test", env="production",
    )
    secret = "dev-bypass-secret-" + "x" * 24
    monkeypatch.setattr(settings, "auth_secret", secret)
    tok = hs256_token(secret, sub="demo|mallory@example.com", roles=["admin"])
    with pytest.raises(Exception):
        await sec.get_current_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)
        )


# ── Fail-closed guards ──

def _prod_settings(**over) -> Settings:
    base = {
        "env": "production",
        "jwt_issuer": "https://kromarag-prod.clerk.accounts.dev",
        "jwt_audience": "kromarag-web",
        "cors_origins": ["https://kromarag.vercel.app"],
        "database_url": "postgresql+asyncpg://u:p@host-pooler.neon.tech:5432/db?sslmode=require",
        "google_api_key": "test-google-key",
        "groq_api_key": "test-groq-key",
    }
    base.update(over)
    return Settings(**base)


def test_fail_closed_guards_pass_when_secure():
    _prod_settings().validate_prod_guards()  # must not raise


def test_fail_closed_cors_wildcard():
    s = _prod_settings(cors_origins=["*"])
    with pytest.raises(RuntimeError, match="cors_wildcard_rejected"):
        s.validate_prod_guards()


def test_fail_closed_http_issuer():
    s = _prod_settings(jwt_issuer="http://localhost:3000")
    with pytest.raises(RuntimeError, match="jwt_issuer_invalid"):
        s.validate_prod_guards()


def test_fail_closed_localhost_clerk_issuer():
    s = _prod_settings(jwt_issuer="https://localhost:3000")
    with pytest.raises(RuntimeError, match="jwt_issuer_invalid"):
        s.validate_prod_guards()


def test_fail_closed_wrong_audience():
    s = _prod_settings(jwt_audience="wrong-aud")
    with pytest.raises(RuntimeError, match="jwt_audience_invalid"):
        s.validate_prod_guards()


def test_fail_closed_db_without_ssl():
    s = _prod_settings(database_url="postgresql+asyncpg://u:p@localhost:5432/db")
    with pytest.raises(RuntimeError, match="database_url_invalid"):
        s.validate_prod_guards()


def test_nonprod_guards_relaxed():
    s = Settings(env="development", cors_origins=["*"], jwt_issuer="http://localhost:3000")
    s.validate_prod_guards()  # must not raise outside production


def test_fail_closed_missing_gemini_key():
    s = _prod_settings(google_api_key="", gemini_api_key="")
    with pytest.raises(RuntimeError, match="llm_key_missing"):
        s.validate_prod_guards()


def test_fail_closed_missing_groq_key():
    s = _prod_settings(groq_api_key="")
    with pytest.raises(RuntimeError, match="llm_key_missing"):
        s.validate_prod_guards()


def test_prod_app_includes_trusted_host_middleware(monkeypatch):
    """main.py adds TrustedHostMiddleware only when ENV=production (specs/08 §3.1).

    Reloads app.main against prod-like settings, then restores. The singleton
    Settings is patched (not env), so local .env files cannot leak in.
    """
    import importlib

    import app.main as main_mod
    from app.core import config as cfg

    prod = _prod_settings(
        cors_origins=["https://kromarag-staging.vercel.app"],
        jwt_issuer="https://kromarag-dev.clerk.accounts.dev",
    )
    monkeypatch.setattr(cfg.settings, "env", prod.env)
    monkeypatch.setattr(cfg.settings, "cors_origins", prod.cors_origins)
    monkeypatch.setattr(cfg.settings, "jwt_issuer", prod.jwt_issuer)
    monkeypatch.setattr(cfg.settings, "jwt_audience", prod.jwt_audience)
    monkeypatch.setattr(cfg.settings, "database_url", prod.database_url)
    monkeypatch.setattr(cfg.settings, "google_api_key", prod.google_api_key)
    monkeypatch.setattr(cfg.settings, "groq_api_key", prod.groq_api_key)
    reloaded = importlib.reload(main_mod)
    try:
        kinds = [m.cls.__name__ for m in reloaded.app.user_middleware]
        assert "TrustedHostMiddleware" in kinds
        assert "CORSMiddleware" in kinds
    finally:
        importlib.reload(main_mod)


# ── Prod verify hygiene (dev HS256 filter kept harmlessly; Clerk is prod path) ──

def test_prod_verify_drops_documented_legacy_default():
    # Explicit legacy default (hermetic: local dev .env must not leak into the assertion).
    s = _prod_settings(auth_secret="change-me-dev-only-override-in-production")
    assert s.effective_verify_secrets == []


def test_prod_verify_keeps_strong_previous_and_legacy():
    s = _prod_settings(auth_secret_current="c" * 40, auth_secret_previous="p" * 40, auth_secret="l" * 40)
    assert s.effective_verify_secrets == ["c" * 40, "p" * 40, "l" * 40]


def test_prod_verify_drops_weak_previous_but_keeps_current():
    s = _prod_settings(auth_secret_current="c" * 40, auth_secret_previous="short", auth_secret="l" * 40)
    assert s.effective_verify_secrets == ["c" * 40, "l" * 40]


@pytest.mark.asyncio
async def test_prod_attacker_hs256_rejected(monkeypatch):
    """End-to-end: HS256 token forged with the documented dev default 401s in prod."""
    install_clerk(
        monkeypatch, sec, settings,
        current_kids=[CLERK_KID], iss="https://kromarag-prod.clerk.accounts.dev",
        env="production",
    )
    monkeypatch.setattr(settings, "auth_secret", "change-me-dev-only-override-in-production")
    forged = hs256_token(
        "change-me-dev-only-override-in-production",
        sub="demo|mallory@example.com", roles=["admin"],
    )
    with pytest.raises(Exception):
        await sec.get_current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials=forged))

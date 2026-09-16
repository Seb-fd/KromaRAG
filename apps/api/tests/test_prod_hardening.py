"""Production hardening tests (specs/08 §7): rate limits, dual-secret rotation,
security headers, health/readyz probes, fail-closed boot guards. Offline (fakes)."""
from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from app.core import rate_limit as rl
from app.core.config import Settings, settings
from app.core import security as sec
from app.main import app


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unsigned_token(sub: str = "u1", roles: list[str] | None = None) -> str:
    """JWT-shaped token with decodable payload (signature never verified for bucketing)."""
    roles = roles if roles is not None else ["viewer"]
    return f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url({'sub': sub, 'roles': roles})}.sig"


def _signed_token(secret: str, sub: str = "u1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": f"{sub}@x.com",
        "roles": roles or ["viewer"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "exp": now + 3600,
        "iat": now,
        "jti": "t1",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


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
        assert "server" not in {k.lower() for k in resp.headers.keys()}


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


# ── Dual-secret rotation ──

@pytest.mark.asyncio
async def test_dual_secret_current_and_previous(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    cur, prev = "c" * 32, "p" * 32
    monkeypatch.setattr(settings, "auth_secret_current", cur)
    monkeypatch.setattr(settings, "auth_secret_previous", prev)
    monkeypatch.setattr(settings, "auth_secret", "legacyUNUSED" + "x" * 20)
    for secret in (cur, prev):
        tok = _signed_token(secret, sub="alice")
        p = await sec.get_current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok))
        assert p.sub == "alice" and p.roles == ["viewer"]
    # Unknown secret still rejected.
    bad = _signed_token("z" * 32, sub="mallory")
    with pytest.raises(Exception):
        await sec.get_current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials=bad))


@pytest.mark.asyncio
async def test_dual_secret_legacy_fallback(monkeypatch):
    monkeypatch.setattr(sec, "_get_jwks", _no_jwks)
    monkeypatch.setattr(settings, "auth_secret_current", "")
    monkeypatch.setattr(settings, "auth_secret_previous", "")
    monkeypatch.setattr(settings, "auth_secret", "legacy-secret-" + "x" * 24)
    tok = _signed_token(settings.auth_secret, sub="bob", roles=["editor"])
    p = await sec.get_current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok))
    assert p.sub == "bob" and p.roles == ["editor"]


# ── Fail-closed guards ──

def _prod_settings(**over) -> Settings:
    base = {
        "env": "production",
        "auth_secret_current": "c" * 40,
        "jwt_issuer": "https://kromarag.vercel.app",
        "cors_origins": ["https://kromarag.vercel.app"],
        "database_url": "postgresql+asyncpg://u:p@host-pooler.neon.tech:5432/db?sslmode=require",
    }
    base.update(over)
    return Settings(**base)


def test_fail_closed_guards_pass_when_secure():
    _prod_settings().validate_prod_guards()  # must not raise


def test_fail_closed_short_secret():
    s = _prod_settings(auth_secret_current="short")
    with pytest.raises(RuntimeError, match="auth_secret_invalid"):
        s.validate_prod_guards()


def test_fail_closed_default_secret():
    s = _prod_settings(auth_secret_current="change-me-32-chars-min-xxxxxxxx")
    with pytest.raises(RuntimeError, match="auth_secret_invalid"):
        s.validate_prod_guards()


def test_fail_closed_cors_wildcard():
    s = _prod_settings(cors_origins=["*"])
    with pytest.raises(RuntimeError, match="cors_wildcard_rejected"):
        s.validate_prod_guards()


def test_fail_closed_http_issuer():
    s = _prod_settings(jwt_issuer="http://localhost:3000")
    with pytest.raises(RuntimeError, match="jwt_issuer_invalid"):
        s.validate_prod_guards()


def test_fail_closed_db_without_ssl():
    s = _prod_settings(database_url="postgresql+asyncpg://u:p@localhost:5432/db")
    with pytest.raises(RuntimeError, match="database_url_invalid"):
        s.validate_prod_guards()


def test_nonprod_guards_relaxed():
    s = Settings(env="development", cors_origins=["*"], auth_secret_current="x")
    s.validate_prod_guards()  # must not raise outside production

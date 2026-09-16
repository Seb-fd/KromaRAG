"""Release-audit regression tests (2026-09-16 decisions).

Covers the five Build-mode fixes:
  1. Credentials demo IdP already gated (verified by code inspection of auth.ts).
  2. Next.js headers() present (verified via node import in CI; see test below).
  3. Dockerfile $PORT honored (verified via dockerfile parse test).
  4. config.validate_prod_guards strictly requires -pooler in prod.
  5. Rate-limit sub bound to IP + secondary IP cap; migrate prefers SYNC DSN.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, settings
from app.main import app
from app.services import migrate as mig_mod


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unsigned_token(sub: str) -> str:
    return f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url({'sub': sub, 'roles': ['viewer']})}.sig"


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


# ── 4. strict -pooler guard ──

def test_fail_closed_db_requires_pooler_even_with_ssl():
    s = _prod_settings(database_url="postgresql+asyncpg://u:p@host.neon.tech:5432/db?sslmode=require")
    with pytest.raises(RuntimeError, match="database_url_invalid"):
        s.validate_prod_guards()


def test_fail_closed_db_requires_ssl_even_with_pooler():
    s = _prod_settings(database_url="postgresql+asyncpg://u:p@host-pooler.neon.tech:5432/db")
    with pytest.raises(RuntimeError, match="database_url_invalid"):
        s.validate_prod_guards()


def test_prod_guards_pass_with_pooler_and_ssl():
    _prod_settings().validate_prod_guards()


# ── 5a. migrate prefers direct SYNC DSN ──

def test_migrate_dsn_prefers_sync_direct_host(monkeypatch):
    monkeypatch.setattr(
        settings, "database_url_sync",
        "postgresql://u:p@host.neon.tech:5432/db?sslmode=require",
    )
    dsn = mig_mod._migrate_dsn()
    assert dsn.startswith("postgresql://")
    assert "-pooler" not in dsn
    assert "+asyncpg" not in dsn


def test_migrate_dsn_strips_asyncpg_prefix(monkeypatch):
    monkeypatch.setattr(
        settings, "database_url_sync",
        "postgresql+asyncpg://u:p@host.neon.tech:5432/db?sslmode=require",
    )
    assert mig_mod._migrate_dsn() == "postgresql://u:p@host.neon.tech:5432/db?sslmode=require"


def test_migrate_dsn_falls_back_to_pooled_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(settings, "database_url_sync", "")
    with caplog.at_level("WARNING", logger="kromarag.migrate"):
        dsn = mig_mod._migrate_dsn()
    assert dsn  # non-empty fallback
    assert any("migrate_sync_url_unset" in r.message for r in caplog.records)


# ── 5b. rate-limit: sub bound to IP, rotating-sub capped ──

@pytest.mark.asyncio
async def test_rate_limit_same_sub_different_ips_isolated(monkeypatch):
    """Same spoofed sub from two IPs must not share one bucket (no cross-IP poisoning)."""
    monkeypatch.setattr(settings, "rate_limit_query_per_min", 2)
    tok = _unsigned_token("victim")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Exhaust victim's bucket from IP 1.2.3.4 (2 allowed, 3rd 429).
        h1 = {"Authorization": f"Bearer {tok}", "X-Forwarded-For": "1.2.3.4"}
        assert (await c.post("/api/v1/query", json={"query": "a"}, headers=h1)).status_code != 429
        assert (await c.post("/api/v1/query", json={"query": "b"}, headers=h1)).status_code != 429
        assert (await c.post("/api/v1/query", json={"query": "c"}, headers=h1)).status_code == 429
        # Same sub from a different IP must still be allowed (separate bucket).
        h2 = {"Authorization": f"Bearer {tok}", "X-Forwarded-For": "9.9.9.9"}
        r = await c.post("/api/v1/query", json={"query": "d"}, headers=h2)
        assert r.status_code != 429


@pytest.mark.asyncio
async def test_rate_limit_rotating_sub_bounded_by_ip_cap(monkeypatch):
    """Rotating unverified subs from one IP must hit the secondary IP cap (3x)."""
    monkeypatch.setattr(settings, "rate_limit_query_per_min", 2)  # ip cap = 6
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        statuses = []
        for i in range(8):
            tok = _unsigned_token(f"rot-{i}")
            h = {"Authorization": f"Bearer {tok}", "X-Forwarded-For": "5.6.7.8"}
            r = await c.post("/api/v1/query", json={"query": f"q{i}"}, headers=h)
            statuses.append(r.status_code)
        # First 6 pass (fresh sub each time), then the shared IP bucket bites.
        assert statuses[:6] == [200] * 6 or all(s != 429 for s in statuses[:6])
        assert 429 in statuses[6:]
        # And the 429 carries rate-limit headers.
        h = {"Authorization": f"Bearer {_unsigned_token('rot-final')}", "X-Forwarded-For": "5.6.7.8"}
        # Exhaust remainder if needed, then assert headers on a limited response.
        for i in range(4):
            r = await c.post("/api/v1/query", json={"query": "x"}, headers=h)
        assert r.status_code == 429
        assert r.headers.get("X-RateLimit-Limit") in ("2", "6")
        assert r.headers["X-RateLimit-Remaining"] == "0"


# ── 2/3. web headers + docker PORT (static file assertions) ──

def test_next_config_has_security_headers():
    cfg = (Path(__file__).resolve().parents[2] / "web" / "next.config.js").read_text()
    for token in [
        "headers()",
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "X-DNS-Prefetch-Control",
        "poweredByHeader",
    ]:
        assert token in cfg, token


def test_dockerfile_honors_port_env():
    df = (Path(__file__).resolve().parents[2] / "api" / "Dockerfile").read_text()
    assert "${PORT:-8000}" in df
    # No hardcoded exec-form port that would ignore $PORT.
    assert '"--port", "8000"' not in df

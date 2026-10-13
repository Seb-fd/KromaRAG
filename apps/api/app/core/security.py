"""Clerk JWT validation + RBAC. Implements specs/02 §5 + specs/08 §3.3 (Clerk cutover).

Claims (Clerk `kromarag` template): sub=user_xxx, email top-level,
roles[] top-level (from {{user.public_metadata.roles}}), aud=kromarag-web.
Fallback: public_metadata.roles / publicMetadata.roles when `roles` is absent.
Default roles=["viewer"] when empty (locked decision).

Paths:
- Clerk RS256 via JWKS (kid match + prev-set fallback) — prod path.
- HS256 dev-bypass (non-prod only): local web mints with dev AUTH_SECRET,
  JWT_ISSUER=http://localhost:3000, jwt_jwks_enabled=false. Prod physically
  cannot accept bypass tokens (kid-less HS256 rejected outright when is_prod).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.schemas.common import Principal

bearer = HTTPBearer(auto_error=False)
_jwks_cache: dict = {"keys": [], "fetched_at": 0.0}
_jwks_prev: dict = {"keys": [], "fetched_at": 0.0}
ALLOWED_ROLES = {"admin", "editor", "viewer"}
_HIERARCHY = {"viewer": 1, "editor": 2, "admin": 3}
SKEW = 60  # seconds
_JWKS_TTL_OK = 600  # seconds: positive cache (keys found)
_JWKS_TTL_NEG = 60  # seconds: negative cache (404 / error / empty)
log = logging.getLogger("kromarag.auth")


def _jwks_url() -> str:
    """Explicit CLERK_JWKS_URL > JWT_JWKS_URL wins; otherwise derive from JWT_ISSUER."""
    for attr in ("clerk_jwks_url", "jwt_jwks_url"):
        override = (getattr(settings, attr, "") or "").strip()
        if override:
            return override
    return f"{settings.jwt_issuer.rstrip('/')}/.well-known/jwks.json"


def _jwks_fetch_skipped() -> bool:
    """True when no HTTP fetch should be attempted (HS256-only dev)."""
    if not getattr(settings, "jwt_jwks_enabled", True):
        return True
    # Loopback issuer without an explicit JWKS URL = local Auth.js HS256 dev:
    # fetching {localhost}/.well-known/jwks.json always 404s (Next.js has no
    # such route) and _get_jwks() would retry on EVERY request. Skip silently.
    if (getattr(settings, "jwt_jwks_url", "") or "").strip():
        return False
    try:
        host = (urlparse(settings.jwt_issuer or "").hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


async def _get_jwks() -> list[dict]:
    now = time.time()
    age = now - float(_jwks_cache.get("fetched_at") or 0.0)
    keys = _jwks_cache.get("keys") or []
    # Timestamp-based TTL covers both positive AND negative results, so a 404
    # or network error cannot cause a per-request retry storm.
    ttl = _JWKS_TTL_OK if keys else _JWKS_TTL_NEG
    if age < ttl:
        return keys
    if _jwks_fetch_skipped():
        return keys
    url = _jwks_url()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url)
            if r.status_code == 200:
                fetched = r.json().get("keys", [])
                if keys and fetched != keys:
                    _jwks_prev.update(keys=keys, fetched_at=_jwks_cache.get("fetched_at", now))
                _jwks_cache.update(keys=fetched, fetched_at=now)
                return fetched
            # Non-200 (e.g. Next.js 404): negative-cache the miss.
            _jwks_cache.update(fetched_at=now)
    except Exception:
        # Network error: negative-cache so callers fall through to HS256
        # without hammering the issuer on every request.
        _jwks_cache.update(fetched_at=now)
    return _jwks_cache.get("keys") or []


def _decode_hs256(token: str) -> tuple[dict | None, str | None]:
    """Try HS256 secrets in order CURRENT → PREVIOUS. Returns (claims, generation)."""
    last_err: Exception | None = None
    secrets = settings.effective_verify_secrets
    current = settings.effective_current_secret
    for s in secrets:
        try:
            claims = jwt.decode(token, s, algorithms=["HS256"],
                                issuer=settings.jwt_issuer, audience=settings.jwt_audience, leeway=SKEW)
            generation = "current" if s == current else "previous"
            if generation == "previous":
                log.warning('{"event":"jwt_previous_key_used"}')
            return claims, generation
        except Exception as e:
            last_err = e
    raise last_err if last_err else Exception("invalid token")


def _normalize_roles(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return ["viewer"]
    roles = [r for r in raw if r in ALLOWED_ROLES]
    return roles or ["viewer"]


def _extract_roles(claims: dict) -> list[str]:
    """Clerk claim mapping: `roles` top-level (template) or public_metadata.roles.

    Template `kromarag` emits email={{user.primary_email_address}},
    roles={{user.public_metadata.roles}}, aud=kromarag-web. Older/alternate
    shapes may nest under public_metadata / publicMetadata — accept both.
    Unknown roles dropped, empty → ["viewer"] (locked default).
    """
    raw = claims.get("roles")
    if raw is None:
        pm = claims.get("public_metadata")
        if isinstance(pm, dict):
            raw = pm.get("roles")
    if raw is None:
        pm2 = claims.get("publicMetadata")
        if isinstance(pm2, dict):
            raw = pm2.get("roles")
    return _normalize_roles(raw)


async def get_current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if not creds or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHENTICATED", "message": "missing bearer token"})
    token = creds.credentials
    # Routing hint only (never trusted for verification): HS256 dev-bypass tokens
    # carry no kid, so verify them locally FIRST (non-prod only) and skip the
    # JWKS HTTP fetch entirely. Clerk RS256 tokens (kid present) keep JWKS-first.
    # Prod rule: tokens without kid (HS256/dev-bypass) are rejected outright.
    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        header = {}
    kid = header.get("kid")
    alg = header.get("alg")
    hs256_first = alg == "HS256" or not kid
    if hs256_first and settings.is_prod:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHENTICATED", "message": "HS256 dev-bypass tokens are rejected in production (Clerk RS256 required)"})
    claims: dict | None = None
    last_err: Exception | None = None
    hs_err: Exception | None = None
    if hs256_first and not settings.is_prod:
        try:
            claims, _gen = _decode_hs256(token)
        except Exception as e:
            hs_err = e
    if claims is None:
        jwks = await _get_jwks()
        # Prefer kid match when the token carries one (kid-rollover safe).
        ordered_keys = sorted(jwks, key=lambda k: 0 if (kid and k.get("kid") == kid) else 1)
        for key in ordered_keys:
            try:
                claims = jwt.decode(token, jwt.algorithms.RSAAlgorithm.from_jwk(key), algorithms=["RS256"],  # type: ignore[arg-type]
                                    issuer=settings.jwt_issuer, audience=settings.jwt_audience, leeway=SKEW)
                break
            except Exception as e:
                last_err = e
        if claims is None:
            for key in _jwks_prev.get("keys", []):
                try:
                    claims = jwt.decode(token, jwt.algorithms.RSAAlgorithm.from_jwk(key), algorithms=["RS256"],  # type: ignore[arg-type]
                                        issuer=settings.jwt_issuer, audience=settings.jwt_audience, leeway=SKEW)
                    log.warning('{"event":"jwks_previous_key_used"}')
                    break
                except Exception as e:
                    last_err = e
    # Non-prod fallback: RS256-shaped token that missed JWKS may still be a
    # local HS256 dev token (e.g. forged alg header). Prod never reaches here.
    if claims is None and not hs256_first and not settings.is_prod:
        try:
            claims, _gen = _decode_hs256(token)
        except Exception as e:
            hs_err = e
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHENTICATED", "message": f"invalid token: {last_err or hs_err}"})
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHENTICATED", "message": "missing sub claim"})
    # Clerk template maps email top-level + roles top-level (or public_metadata).
    return Principal(sub=sub, email=claims.get("email"), roles=_extract_roles(claims))


def require_role(minimum: str):
    need = _HIERARCHY[minimum]
    async def dep(p: Principal = Depends(get_current_principal)) -> Principal:
        have = max((_HIERARCHY.get(r, 0) for r in p.roles), default=0)
        if have < need:
            raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "FORBIDDEN", "message": f"requires role {minimum}"})
        return p
    return dep

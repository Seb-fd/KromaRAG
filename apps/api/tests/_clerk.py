"""Clerk test helper: session-scoped RSA-2048 keypair + JWKS + token signers.

Clerk cutover: backend verifies Clerk RS256 (JWKS) in all envs, plus HS256
dev-bypass only when not prod. Tests mint RS256 with a local keypair whose
public JWK is installed via monkeypatched `_get_jwks`.

- iss=https://clerk.test (matches Settings override in tests)
- aud=kromarag-web (template `kromarag` carries it)
- kids: `clerk-test-k1` (current), `clerk-test-k0` (previous for rollover)

Requires `cryptography` (already a backend dep).
"""
from __future__ import annotations

import base64
import time
import uuid

import jwt

CLERK_TEST_ISS = "https://clerk.test"
CLERK_TEST_AUD = "kromarag-web"
CLERK_KID = "clerk-test-k1"
CLERK_PREV_KID = "clerk-test-k0"

_priv_cache: dict[str, object] = {}
_pub_jwk_cache: dict[str, dict] = {}


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _ensure_keypair(kid: str):
    """Lazily generate (and cache) an RSA-2048 keypair for `kid`."""
    if kid in _priv_cache:
        return _priv_cache[kid]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    numbers = pub.public_numbers()
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(n_bytes),
        "e": _b64u(e_bytes),
    }
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    _priv_cache[kid] = priv_pem
    _pub_jwk_cache[kid] = jwk
    return priv_pem


def _pub_jwk(kid: str) -> dict:
    _ensure_keypair(kid)
    return dict(_pub_jwk_cache[kid])


def jwks(kids: list[str] | None = None) -> dict:
    """Public JWKS dict for the given kids (default: current only)."""
    kids = kids if kids is not None else [CLERK_KID]
    return {"keys": [_pub_jwk(k) for k in kids]}


def jwks_list(kids: list[str] | None = None) -> list[dict]:
    return jwks(kids)["keys"]


def clerk_token(
    sub: str = "user_test123",
    email: str | None = None,
    roles: list[str] | None = None,
    kid: str = CLERK_KID,
    iss: str = CLERK_TEST_ISS,
    aud: str = CLERK_TEST_AUD,
    ttl: int = 3600,
    public_metadata: dict | None = None,
    extra: dict | None = None,
) -> str:
    """Mint a Clerk-shaped RS256 JWT (template `kromarag`).

    Top-level `email` + `roles` mirror the custom template
    (email={{user.primary_email_address}}, roles={{user.public_metadata.roles}}).
    Pass roles=None + public_metadata={"roles": [...]} to exercise the fallback.
    """
    priv_pem = _ensure_keypair(kid)
    now = int(time.time())
    payload: dict = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "exp": now + ttl,
        "iat": now,
        "jti": str(uuid.uuid4()),
    }
    if email is not None:
        payload["email"] = email
    else:
        payload["email"] = f"{sub}@example.com"
    if roles is not None:
        payload["roles"] = roles
    if public_metadata is not None:
        payload["public_metadata"] = public_metadata
    if extra:
        payload.update(extra)
    headers = {"kid": kid}
    return jwt.encode(payload, priv_pem, algorithm="RS256", headers=headers)


def hs256_token(
    secret: str,
    sub: str = "demo|dev@example.com",
    roles: list[str] | None = None,
    iss: str = "http://localhost:3000",
    aud: str = CLERK_TEST_AUD,
    ttl: int = 3600,
) -> str:
    """Dev-bypass HS256 token (non-prod only). Mirrors web dev mint."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": sub.split("|")[-1] if "|" in sub else f"{sub}@x.com",
        "roles": roles or ["viewer"],
        "iss": iss,
        "aud": aud,
        "exp": now + ttl,
        "iat": now,
        "jti": "dev-bypass",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def install_clerk(
    monkeypatch,
    sec_mod,
    settings_obj,
    current_kids: list[str] | None = None,
    prev_keys: list[dict] | None = None,
    iss: str = CLERK_TEST_ISS,
    aud: str = CLERK_TEST_AUD,
    env: str = "development",
):
    """Install Clerk JWKS + issuer/audience on the live settings singleton.

    - Patches `sec_mod._get_jwks` to return `jwks_list(current_kids)`.
    - Sets `sec_mod._jwks_prev` to {"keys": prev_keys or [], ...}.
    - Sets settings.jwt_issuer/jwt_audience/env (+ jwt_jwks_enabled True).
    """
    import time as _time

    current_kids = current_kids if current_kids is not None else [CLERK_KID]
    keys = jwks_list(current_kids)

    async def _fake_jwks() -> list[dict]:
        return keys

    monkeypatch.setattr(sec_mod, "_get_jwks", _fake_jwks)
    monkeypatch.setattr(
        sec_mod, "_jwks_prev", {"keys": list(prev_keys or []), "fetched_at": _time.time()}
    )
    monkeypatch.setattr(settings_obj, "jwt_issuer", iss)
    monkeypatch.setattr(settings_obj, "jwt_audience", aud)
    monkeypatch.setattr(settings_obj, "env", env)
    monkeypatch.setattr(settings_obj, "jwt_jwks_enabled", True)

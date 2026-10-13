"""Mint Clerk sign-in tickets for smoke tests (replaces HS256 mint-test-jwt.py).

Clerk session JWTs (template `kromarag`: email+roles+aud, 60s TTL) can only be
issued by Clerk itself. The smoke runner therefore needs real session JWTs:

  Preferred: sign in as each smoke user in the browser (Google SSO or the
  fixed smoke-viewer/-editor/-admin accounts), copy the `Authorization: Bearer`
  header from any /api/v1/* request in DevTools, and export:
    VIEWER_JWT='...' EDITOR_JWT='...' ADMIN_JWT='...' ./scripts/deploy/smoke.sh

  Ticket helper (this script): with CLERK_SECRET_KEY (Backend API key, kept ONLY
  on the smoke runner — never in Cloud Run env, the backend needs no Clerk
  secret for JWKS), create a one-time sign-in ticket per smoke user:
    CLERK_SECRET_KEY='sk_...' python scripts/deploy/mint-clerk-token.py \
      --user-id user_xxx --frontend https://kromarag.vercel.app
  Open the printed ticket URL in a browser (signed out), which creates a Clerk
  session; then copy the session JWT (DevTools -> Authorization header) and
  feed the existing smoke gates unchanged (SSE framing, RBAC 403, oracle 404,
  trust-derived metrics).

  Local self-test (no Clerk calls): --self-test mints a local RS256 JWT with an
  ephemeral keypair for backend dev loops only (never valid against prod JWKS).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid


def _create_sign_in_token(secret: str, user_id: str, ttl: int = 300) -> str:
    url = "https://api.clerk.com/v1/sign_in_tokens"
    body = json.dumps({"user_id": user_id, "expires_in_seconds": ttl}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:
        print(f"error: Clerk sign_in_tokens call failed: {e}", file=sys.stderr)
        print("hint: check CLERK_SECRET_KEY (sk_live_/sk_test_) and --user-id (user_xxx).", file=sys.stderr)
        raise SystemExit(1)
    token = payload.get("token") or payload.get("id")
    if not token:
        print(f"error: unexpected Clerk response: {payload}", file=sys.stderr)
        raise SystemExit(1)
    return str(token)


def _self_test() -> None:
    try:
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as e:
        print(f"error: self-test needs PyJWT + cryptography (backend venv): {e}", file=sys.stderr)
        raise SystemExit(1)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user_selftest",
            "email": "selftest@example.com",
            "roles": ["viewer"],
            "iss": os.environ.get("CLERK_SELFTEST_ISS", "https://clerk.test"),
            "aud": "kromarag-web",
            "exp": now + 300,
            "iat": now,
            "jti": str(uuid.uuid4()),
        },
        pem,
        algorithm="RS256",
        headers={"kid": "selftest-k1"},
    )
    print(token)


def main() -> None:
    p = argparse.ArgumentParser(description="Clerk smoke-ticket helper (see module docstring).")
    p.add_argument("--user-id", default="", help="Clerk user_id (user_xxx) of a fixed smoke user")
    p.add_argument("--frontend", default="https://kromarag.vercel.app",
                   help="Web URL that hosts /login (ticket is exchanged there)")
    p.add_argument("--ttl", type=int, default=300, help="Ticket lifetime in seconds")
    p.add_argument("--self-test", action="store_true", help="Mint an ephemeral local RS256 JWT (no Clerk calls)")
    args = p.parse_args()

    if args.self_test:
        _self_test()
        return
    if not args.user_id:
        p.print_help()
        print("\nerror: --user-id (user_xxx) is required (or --self-test).", file=sys.stderr)
        raise SystemExit(2)
    secret = os.environ.get("CLERK_SECRET_KEY", "")
    if not secret:
        print("error: CLERK_SECRET_KEY env is required (Backend API key; smoke runner only).", file=sys.stderr)
        raise SystemExit(2)
    token = _create_sign_in_token(secret, args.user_id, args.ttl)
    frontend = args.frontend.rstrip("/")
    print(f"ticket: {token}")
    print(f"url: {frontend}/login?__clerk_ticket={token}")
    print("next: open the URL signed-out, sign in completes, then copy the")
    print("      Authorization: Bearer session JWT (DevTools) into VIEWER/EDITOR/ADMIN_JWT.")


if __name__ == "__main__":
    main()

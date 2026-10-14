"""Tiered sliding-window rate limiter (specs/08 §3.2).

In-memory Phase 1 with a Redis-compatible signature so the backend swap is
non-breaking: check_limit(key, limit, window_s) -> (allowed, remaining, reset_s).

MULTI-INSTANCE CONTRACT (load-bearing): buckets live per-process, so the
effective global limit is `per-instance limit x Cloud Run instance count`.
Deploy scripts enforce `--max-instances 3` (override: MAX_INSTANCES) to bound
this multiplier until the Redis swap lands (specs/08 §3.2). Raising
max-instances without Redis silently multiplies every bucket below — do not do
it; the Redis swap keeps this module's call signature unchanged.

Buckets (per spec):
  query        → per-sub,  RATE_LIMIT_QUERY_PER_MIN  (100)
  query:admin  → per-sub,  RATE_LIMIT_ADMIN_PER_MIN  (300)
  ingest       → per-sub,  RATE_LIMIT_INGEST_PER_MIN (20) — ingest/upload/reindex share
  read         → per-sub,  RATE_LIMIT_READ_PER_MIN   (300) — GET documents/conversations
  unauth       → per-IP,   RATE_LIMIT_UNAUTH_PER_MIN (10)
Probes (/health*, /ready*) are exempt (handled in middleware).
"""
from __future__ import annotations

import base64
import json
import threading
import time

_buckets: dict[str, list[float]] = {}
_lock = threading.Lock()
_WINDOW_S = 60.0


def _now() -> float:
    return time.time()


def check_limit(key: str, limit: int, window_s: float = _WINDOW_S) -> tuple[bool, int, int]:
    """Sliding-window check. Returns (allowed, remaining, reset_s)."""
    now = _now()
    cutoff = now - window_s
    with _lock:
        hits = [t for t in _buckets.get(key, []) if t > cutoff]
        if len(hits) >= max(1, limit):
            oldest = min(hits) if hits else now
            reset_s = max(1, int(oldest + window_s - now))
            _buckets[key] = hits
            return False, 0, reset_s
        hits.append(now)
        _buckets[key] = hits
        remaining = max(0, limit - len(hits))
        reset_s = max(1, int(max(hits) + window_s - now)) if hits else int(window_s)
        return True, remaining, reset_s


def reset_rate_limiter() -> None:
    """Clear all buckets (tests + graceful drain)."""
    with _lock:
        _buckets.clear()


def bucket_for_request(method: str, path: str) -> str:
    """Map request to a rate-limit bucket name (before per-sub/IP keying)."""
    p = path or ""
    m = (method or "").upper()
    if p.startswith("/api/v1/query"):
        return "query"
    if p.startswith("/api/v1/ingest") or p.startswith("/api/v1/documents/upload") or p.endswith("/reindex"):
        return "ingest"
    if m == "GET" and (p.startswith("/api/v1/documents") or p.startswith("/api/v1/conversations")):
        return "read"
    if m in ("POST", "PATCH", "DELETE") and (
        p.startswith("/api/v1/documents") or p.startswith("/api/v1/conversations")
    ):
        # Mutating docs/conversations (non-ingest) share the ingest budget (conservative).
        return "ingest"
    return "unauth"


def extract_identity(auth_header: str | None) -> tuple[str | None, bool]:
    """Best-effort (sub, is_admin) from a Bearer JWT WITHOUT verification.

    Used ONLY for bucket keying (never auth). Returns (None, False) when absent
    or opaque. Never raises.

    SECURITY: the returned sub is UNVERIFIED — callers MUST bind it to the
    client IP (e.g. f"{bucket}:{sub}:{ip}") and enforce a secondary per-IP cap,
    otherwise an attacker can mint arbitrary unsigned JWTs with rotating `sub`
    to get a fresh per-sub bucket per request (rate-limit bypass) or spoof a
    victim's `sub` to exhaust their bucket (bucket poisoning). See middleware.
    """
    try:
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None, False
        token = auth_header[7:].strip()
        parts = token.split(".")
        if len(parts) != 3:
            return None, False
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode("utf-8", "replace"))
        sub = payload.get("sub")
        roles = payload.get("roles") or []
        is_admin = "admin" in roles if isinstance(roles, list) else False
        return (str(sub) if sub else None), bool(is_admin)
    except Exception:
        return None, False

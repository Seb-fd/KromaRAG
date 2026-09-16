"""Prometheus metrics endpoint per specs/09 §3.2 (dual-protected ops plane).

Route: GET /metrics (no /api/v1 prefix, like /healthz). Format: Prometheus
text exposition 0.0.4. Gate order (normative): network filter → JWT admin →
render. Network miss → 404 (hide existence); bad JWT → 401; non-admin → 403.
Rate-limit exempt (like probes) with informational X-RateLimit headers.
"""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials

router = APIRouter(tags=["system"])

_NOT_FOUND = {"code": "NOT_FOUND", "message": "not found"}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    client = request.client
    return client.host if client else "unknown"


def _allowlist() -> list[str]:
    from app.core.config import settings

    raw = (settings.metrics_scrape_allowlist or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def _ip_in_allowlist(ip: str, entries: list[str]) -> bool:
    ip_low = (ip or "").strip().lower()
    for entry in entries:
        e = entry.strip()
        if not e:
            continue
        if e.lower() == ip_low:
            return True
        try:
            if "/" in e and ipaddress.ip_address(ip_low) in ipaddress.ip_network(e, strict=False):
                return True
        except Exception:
            continue
    return False


def is_trusted_scraper(request: Request) -> bool:
    """Network gate (spec §3.2): IAM/VPC header OR allowlisted IP.

    Empty allowlist: allow all in non-prod (dev convenience), deny all in
    prod (fail-closed — operator must configure METRICS_SCRAPE_ALLOWLIST).
    """
    from app.core.config import settings

    if (request.headers.get("x-cloud-run-internal") or "").strip().lower() == "true":
        return True
    entries = _allowlist()
    if not entries:
        return not settings.is_prod
    return _ip_in_allowlist(_client_ip(request), entries)


async def _require_admin(request: Request):
    """JWT gate after the network gate. Returns (principal, error_response)."""
    from fastapi import status as _status

    from app.core import security as _sec

    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None, JSONResponse(
            status_code=_status.HTTP_401_UNAUTHORIZED,
            content={"detail": {"code": "UNAUTHENTICATED", "message": "missing bearer token"}})
    token = auth[7:].strip()
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    try:
        principal = await _sec.get_current_principal(creds)
    except Exception as e:
        code = getattr(getattr(e, "detail", None), "get", lambda *_: None)("code") \
            if hasattr(getattr(e, "detail", None), "get", None) else None
        status = getattr(e, "status_code", 401)
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict) and "code" in detail:
            body = {"detail": detail}
        else:
            body = {"detail": {"code": "UNAUTHENTICATED", "message": "invalid token"}}
        return None, JSONResponse(status_code=status if status in (401, 403) else 401, content=body)
    roles = list(getattr(principal, "roles", []) or [])
    if "admin" not in roles:
        return None, JSONResponse(
            status_code=403, content={"detail": {"code": "FORBIDDEN", "message": "requires role admin"}})
    return principal, None


@router.get("/metrics")
async def metrics(request: Request):
    from app.core.config import settings

    if not settings.metrics_enabled:
        return JSONResponse(status_code=404, content={"detail": dict(_NOT_FOUND)})
    # Gate 1/2 — network filter first (cheapest deny, oracle-safe 404).
    if not is_trusted_scraper(request):
        return JSONResponse(status_code=404, content={"detail": dict(_NOT_FOUND)})
    # Gate 2/2 — admin JWT.
    _principal, err = await _require_admin(request)
    if err is not None:
        return err
    try:
        from app.services import metrics as _m
        from app.services.db import pool_stats

        body = _m.render_prometheus(pool_stats_fn=pool_stats)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "METRICS_UNAVAILABLE",
                                "message": "metrics temporarily unavailable"}},
            headers={"Retry-After": "5"})
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-RateLimit-Limit": "0",
                 "X-RateLimit-Remaining": "0"},
    )

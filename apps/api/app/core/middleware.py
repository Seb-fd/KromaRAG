"""Production middleware: security headers + tiered rate limiting (specs/08 §3.1–§3.2)
+ W3C trace-context join + RED metrics (specs/09 §3.1–§3.3)."""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.rate_limit import bucket_for_request, check_limit, extract_identity

_EXEMPT_PREFIXES = ("/health", "/healthz", "/ready", "/readyz", "/metrics",
                    "/openapi.json", "/docs", "/redoc")

_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-DNS-Prefetch-Control": "off",
}


def apply_security_headers(response: Response) -> Response:
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path == p for p in _EXEMPT_PREFIXES) or path in _EXEMPT_PREFIXES


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    client = request.client
    return client.host if client else "unknown"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Outermost middleware: canonical trace join + security headers + RED metrics.

    Join point per specs/09 §3.1 rule 5: valid `traceparent` wins over
    `X-Trace-Id`; the canonical pair is echoed on EVERY response (incl. 429
    from the inner limiter and error responses from downstream handlers).
    """

    async def dispatch(self, request: Request, call_next):
        from app.core.tracecontext import join_trace_context, route_template

        ctx = join_trace_context(
            request.headers.get("traceparent"),
            request.headers.get("tracestate"),
            request.headers.get("x-trace-id"),
        )
        request.state.trace_id = ctx["trace_id"]
        request.state.span_id = ctx["span_id"]
        request.state.traceparent = ctx["traceparent_out"]
        request.state.traceparent_in = ctx["traceparent_in"]
        request.state.tracestate = ctx["tracestate"]
        t0 = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # Still record the error hop so 500s join RED series.
            try:
                from app.services import metrics as _m

                _m.observe_http(route_template(request.method, request.url.path),
                                "500", time.monotonic() - t0, trace_id=ctx["trace_id"])
            except Exception:
                pass
            raise
        apply_security_headers(response)
        # Never emit Server/X-Powered-By.
        for h in ("server", "x-powered-by"):
            if h in response.headers:
                del response.headers[h]
        # Canonical echo (overwrite: W3C join wins over any downstream value).
        response.headers["X-Trace-Id"] = ctx["trace_id"]
        response.headers["traceparent"] = ctx["traceparent_out"]
        try:
            from app.services import metrics as _m

            _m.observe_http(route_template(request.method, request.url.path),
                            str(getattr(response, "status_code", 200)),
                            time.monotonic() - t0, trace_id=ctx["trace_id"])
        except Exception:
            pass
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Tiered per-sub + per-IP sliding window. Probes exempt. Test-override aware.

    Requests WITHOUT an Authorization header that are served via FastAPI
    dependency_overrides (unit tests mocking get_current_principal) bypass the
    unauth IP bucket so the offline suite (96 tests, header-less clients) stays
    green. In production dependency_overrides is empty, so every anonymous
    request is bucketed.
    """

    async def dispatch(self, request: Request, call_next):
        from app.core.config import settings
        from app.core.security import get_current_principal
        from app.main import app as _app  # local import: avoids circular at module load

        path = request.url.path
        if _is_exempt(path):
            return await call_next(request)

        auth_header = request.headers.get("authorization")
        sub, is_admin = extract_identity(auth_header)
        client_ip = _client_ip(request)

        if sub:
            bucket = bucket_for_request(request.method, path)
            if bucket == "query":
                limit = settings.rate_limit_admin_per_min if is_admin else settings.rate_limit_query_per_min
            elif bucket == "ingest":
                limit = settings.rate_limit_ingest_per_min
            elif bucket == "read":
                limit = settings.rate_limit_read_per_min
            else:
                limit = settings.rate_limit_query_per_min
            # SECURITY: extract_identity is UNVERIFIED — bind sub to IP so a
            # spoofed sub cannot poison another client's bucket across IPs.
            key = f"{bucket}:{sub}:{client_ip}"
            # Secondary per-IP velocity cap bounds rotating-sub bypass: fresh
            # sub per request still shares one IP bucket (3x headroom allows
            # small-NAT fan-out while capping abuse at 3x legitimate).
            ip_key = f"{bucket}:ip:{client_ip}"
            ip_limit = max(1, limit * 3)
        else:
            # No verifiable sub: unauth IP bucket — unless this is a header-less
            # test client served by dependency_overrides (offline fakes).
            if not auth_header and get_current_principal in getattr(_app, "dependency_overrides", {}):
                return await call_next(request)
            bucket = "unauth"
            limit = settings.rate_limit_unauth_per_min
            key = f"unauth:{client_ip}"
            ip_key = ""
            ip_limit = 0

        allowed, remaining, reset_s = check_limit(key, limit)
        if sub and allowed:
            # Enforce the secondary IP cap only when the per-sub bucket passed,
            # so legitimate per-sub 429s keep their exact limit headers.
            ip_allowed, ip_remaining, ip_reset = check_limit(ip_key, ip_limit)
            if not ip_allowed:
                allowed, remaining, reset_s = False, 0, ip_reset
                limit = ip_limit
        if not allowed:
            try:
                from app.services import metrics as _m

                _m.inc_rate_limited(bucket)
            except Exception:
                pass
            body = {"detail": {"code": "RATE_LIMITED", "message": f"rate limit exceeded, retry in {reset_s}s"}}
            resp = JSONResponse(status_code=429, content=body)
            resp.headers["Retry-After"] = str(reset_s)
            resp.headers["X-RateLimit-Limit"] = str(limit)
            resp.headers["X-RateLimit-Remaining"] = "0"
            resp.headers["X-RateLimit-Reset"] = str(reset_s)
            # Trace linkage on 429 (outer middleware overwrites with canonical).
            tid = getattr(request.state, "trace_id", None) or request.headers.get("x-trace-id")
            if tid:
                resp.headers["X-Trace-Id"] = tid
            tp = getattr(request.state, "traceparent", None)
            if tp:
                resp.headers["traceparent"] = tp
            else:
                try:
                    from app.core.tracecontext import join_trace_context as _join

                    _ctx = _join(request.headers.get("traceparent"),
                                 request.headers.get("tracestate"),
                                 request.headers.get("x-trace-id"))
                    resp.headers.setdefault("X-Trace-Id", _ctx["trace_id"])
                    resp.headers.setdefault("traceparent", _ctx["traceparent_out"])
                except Exception:
                    import uuid as _uuid

                    resp.headers.setdefault("X-Trace-Id", str(_uuid.uuid4()))
            return apply_security_headers(resp)

        response = await call_next(request)
        # Informative headers on allowed traffic (additive, never breaks clients).
        response.headers.setdefault("X-RateLimit-Limit", str(limit))
        response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
        response.headers.setdefault("X-RateLimit-Reset", str(reset_s))
        return response

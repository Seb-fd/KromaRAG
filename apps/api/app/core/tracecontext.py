"""W3C Trace Context join layer per specs/09 §3.1 (minimal-OTel, LangSmith primary).

Ingress: validate `traceparent` (`00-<32hex>-<16hex>-<flags>`), join to app
`trace_id` (UUID dash form). Egress: canonical `X-Trace-Id` + `traceparent`.
Invalid/missing values never fail a request — mint fresh (never 400).

Precedence: valid `traceparent` wins over `X-Trace-Id`; valid `X-Trace-Id`
(UUID) is reused when `traceparent` is absent/invalid; else fresh uuid4.
"""
from __future__ import annotations

import re
import secrets
import uuid

_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16
_TRACESTATE_MAX = 512


def parse_traceparent(value: str | None) -> tuple[str, str, str] | None:
    """Return (trace_hex, span_hex, flags) if valid, else None. Never raises."""
    try:
        if not value or not isinstance(value, str):
            return None
        m = _TRACEPARENT_RE.match(value.strip().lower())
        if not m:
            return None
        trace_hex, span_hex, flags = m.group(1), m.group(2), m.group(3)
        if trace_hex == _ZERO_TRACE or span_hex == _ZERO_SPAN:
            return None
        if flags == "ff":
            return None
        return trace_hex, span_hex, flags
    except Exception:
        return None


def _valid_uuid(value: str | None) -> str | None:
    try:
        if not value or not isinstance(value, str):
            return None
        return str(uuid.UUID(value.strip()))
    except Exception:
        return None


def build_traceparent(trace_id_uuid: str, span_id_hex: str, sampled: bool = True) -> str:
    """Build `00-<32hex>-<16hex>-<01|00>`. Never raises (falls back to fresh)."""
    try:
        hex32 = uuid.UUID(str(trace_id_uuid)).hex
    except Exception:
        hex32 = uuid.uuid4().hex
    span = (span_id_hex or "").strip().lower()
    if len(span) != 16 or span == _ZERO_SPAN or any(c not in "0123456789abcdef" for c in span):
        span = secrets.token_hex(8)
    return f"00-{hex32}-{span}-{'01' if sampled else '00'}"


def truncate_tracestate(value: str | None) -> str | None:
    """Cap tracestate at 512 chars (spec §4.2 rule 3). Returns None when absent."""
    if value is None:
        return None
    try:
        s = str(value)
    except Exception:
        return None
    if not s:
        return None
    return s if len(s) <= _TRACESTATE_MAX else s[:_TRACESTATE_MAX]


def join_trace_context(
    traceparent_in: str | None,
    tracestate_in: str | None = None,
    xtraceid_in: str | None = None,
) -> dict:
    """Join ingress context to canonical app trace ids.

    Returns {trace_id, span_id, traceparent_in, traceparent_out, tracestate, sampled}.
    `sampled` echoes ingress flag when valid, else True (conservative: error
    sampling is 1.0 and middleware cannot know the outcome yet).
    """
    parsed = parse_traceparent(traceparent_in)
    tracestate = truncate_tracestate(tracestate_in)
    if parsed is not None:
        trace_hex, _span_hex, flags = parsed
        try:
            trace_id = str(uuid.UUID(hex=trace_hex))
        except Exception:
            trace_id = str(uuid.uuid4())
        sampled = (flags == "01")
        traceparent_validated: str | None = f"00-{trace_hex}-{parsed[1]}-{flags}"
    else:
        reused = _valid_uuid(xtraceid_in)
        trace_id = reused or str(uuid.uuid4())
        sampled = True
        traceparent_validated = None
    span_id = secrets.token_hex(8)
    traceparent_out = build_traceparent(trace_id, span_id, sampled=True)
    # Always echo sampled=01 from the server side (we cannot know the outcome
    # at ingress; error paths force sampling per §4.1). Ingress flag preserved
    # separately for logs via traceparent_in.
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "traceparent_in": traceparent_validated,
        "traceparent_out": traceparent_out,
        "tracestate": tracestate,
        "sampled": True,
        "ingress_sampled": sampled,
    }


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"^\d+$")


def route_template(method: str, path: str) -> str:
    """Return `METHOD <template>` with UUID/numeric segments redacted to {id}.

    Guarantees bounded `route` label cardinality (spec §3.3): no raw ids in
    metric labels. Never raises.
    """
    try:
        m = (method or "GET").upper()
        p = (path or "/").split("?", 1)[0] or "/"
        parts: list[str] = []
        for seg in p.split("/"):
            if not seg:
                continue
            if _UUID_RE.match(seg) or _NUM_RE.match(seg) or len(seg) == 32 and all(c in "0123456789abcdefABCDEF" for c in seg):
                parts.append("{id}")
            else:
                parts.append(seg)
        return f"{m} /{'/'.join(parts)}"
    except Exception:
        return "GET /unknown"


def db_application_name(trace_id: str) -> str:
    """Short `application_name` for pg_stat_activity joins (spec §3.1 rule 8)."""
    try:
        return f"kromarag:{uuid.UUID(str(trace_id)).hex[:8]}"
    except Exception:
        return "kromarag:unknown"


def db_trace_comment(trace_id: str) -> str:
    """SQL comment carrying the full trace id (spec §3.1 rule 8)."""
    try:
        safe = str(uuid.UUID(str(trace_id)))
    except Exception:
        safe = "unknown"
    return f"/* trace_id={safe} */"

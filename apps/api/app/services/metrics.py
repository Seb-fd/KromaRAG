"""In-memory RED/USE metrics per specs/09 §3.3 (Prometheus exposition 0.0.4).

No external dependency (manual exposition) so offline unit tests stay green.
Thread-safe via a single lock. All label values are bounded: route templates
only (no ids), numeric codes, pinned model ids. Dynamic values are dropped and
counted via kromarag_metrics_cardinality_shed_total (fail-safe under-count).

Series are emitted with zero-valued baselines so scrapes always contain the
frozen catalogue (Gherkin presence checks pass on fresh boot).
"""
from __future__ import annotations

import re
import threading

SERVICE = "kromarag-api"
VERSION = "0.1.0"
LE_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25]

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$")
_CODE_RE = re.compile(r"^\d{3}$")
_BUCKET_RE = re.compile(r"^[a-z_]+$")
_OUTCOME_RE = re.compile(r"^[a-z_]+$")
_CODE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")  # LLM_*/DB_* codes (uppercase + digits)

_lock = threading.Lock()
_counters: dict[tuple, float] = {}
_histograms: dict[tuple, list[float]] = {}
_hist_exemplars: dict[tuple, str] = {}
_shed_total: float = 0.0


def reset_metrics() -> None:
    """Clear all process-local series (tests + graceful drain)."""
    global _shed_total
    with _lock:
        _counters.clear()
        _histograms.clear()
        _hist_exemplars.clear()
        _shed_total = 0.0


def _shed() -> None:
    global _shed_total
    _shed_total += 1.0


def _safe_model(model: str | None) -> str | None:
    if not model or not isinstance(model, str):
        return None
    m = model.strip()
    if not m or len(m) > 64 or not _MODEL_RE.match(m):
        with _lock:
            _shed()
        return None
    return m


def _safe_code(code: str | int | None) -> str | None:
    try:
        c = str(code or "").strip()
    except Exception:
        return None
    if not _CODE_RE.match(c):
        return None
    return c


def _safe_token(value: str | None, pattern: re.Pattern) -> str | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 64 or not pattern.match(v):
        return None
    return v


# ── HTTP RED ──

def observe_http(route: str, code: str | int, duration_s: float, trace_id: str | None = None) -> None:
    c = _safe_code(code)
    if not route or not c:
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    key = ("http_server_duration_seconds", route, c)
    with _lock:
        _histograms.setdefault(key, []).append(dur)
        if trace_id:
            _hist_exemplars[key] = str(trace_id)[:64]
        _counters[("http_server_requests_total", route, c)] = \
            _counters.get(("http_server_requests_total", route, c), 0.0) + 1.0


def inc_rate_limited(bucket: str) -> None:
    b = _safe_token(bucket, _BUCKET_RE)
    if not b:
        return
    with _lock:
        _counters[("http_rate_limited_total", b)] = _counters.get(("http_rate_limited_total", b), 0.0) + 1.0


# ── LLM / tokens / fallback ──

def inc_tokens(model: str, kind: str, n: int) -> None:
    m = _safe_model(model)
    k = _safe_token(kind, _OUTCOME_RE)
    if not m or k not in ("prompt", "completion"):
        return
    try:
        count = max(0, int(n or 0))
    except Exception:
        return
    if count == 0:
        return
    with _lock:
        _counters[("llm_tokens_total", m, k)] = _counters.get(("llm_tokens_total", m, k), 0.0) + float(count)


def inc_fallback(from_model: str, to_model: str, code: str) -> None:
    f = _safe_model(from_model)
    t = _safe_model(to_model)
    c = _safe_token(code, _CODE_TOKEN_RE)
    if not f or not t or not c:
        return
    with _lock:
        _counters[("llm_fallback_total", f, t, c)] = \
            _counters.get(("llm_fallback_total", f, t, c), 0.0) + 1.0


def observe_llm(model: str, outcome: str, duration_s: float, trace_id: str | None = None) -> None:
    m = _safe_model(model)
    o = _safe_token(outcome, _OUTCOME_RE)
    if not m or o not in ("ok", "fallback", "error"):
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    key = ("llm_request_duration_seconds", m, o)
    with _lock:
        _histograms.setdefault(key, []).append(dur)
        if trace_id:
            _hist_exemplars[key] = str(trace_id)[:64]


# ── Retrieval / embed / DB ──

def observe_vector_search(duration_s: float, outcome: str, trace_id: str | None = None) -> None:
    o = _safe_token(outcome, _OUTCOME_RE)
    if o not in ("ok", "timeout", "error"):
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    key = ("vector_search_duration_seconds", o)
    with _lock:
        _histograms.setdefault(key, []).append(dur)
        if trace_id:
            _hist_exemplars[key] = str(trace_id)[:64]


def observe_embed(model: str, cached: bool, duration_s: float) -> None:
    m = _safe_model(model)
    if not m:
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    key = ("embed_duration_seconds", m, "true" if cached else "false")
    with _lock:
        _histograms.setdefault(key, []).append(dur)


def observe_db_wait(duration_s: float, outcome: str) -> None:
    o = _safe_token(outcome, _OUTCOME_RE)
    if o not in ("acquired", "timeout"):
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    with _lock:
        _histograms.setdefault(("db_pool_wait_seconds", o), []).append(dur)


def inc_db_error(code: str) -> None:
    c = _safe_token(code, _CODE_TOKEN_RE)
    if c not in ("DB_UNAVAILABLE", "DB_TIMEOUT"):
        return
    with _lock:
        _counters[("db_errors_total", c)] = _counters.get(("db_errors_total", c), 0.0) + 1.0


# ── SSE / history / lifecycle ──

def observe_sse(route: str, outcome: str, duration_s: float) -> None:
    o = _safe_token(outcome, _OUTCOME_RE)
    if not route or o not in ("done", "disconnect", "error"):
        return
    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        return
    with _lock:
        _histograms.setdefault(("sse_stream_duration_seconds", route, o), []).append(dur)


def inc_sse_tokens(model: str, n: int) -> None:
    m = _safe_model(model)
    if not m:
        return
    try:
        count = max(0, int(n or 0))
    except Exception:
        return
    if count == 0:
        return
    with _lock:
        _counters[("sse_tokens_total", m)] = _counters.get(("sse_tokens_total", m), 0.0) + float(count)


def inc_history_tokens(n: int) -> None:
    try:
        count = max(0, int(n or 0))
    except Exception:
        return
    if count == 0:
        return
    with _lock:
        _counters[("history_tokens_total",)] = _counters.get(("history_tokens_total",), 0.0) + float(count)


def inc_reindex(outcome: str) -> None:
    o = _safe_token(outcome, _OUTCOME_RE)
    if o not in ("ok", "failed", "conflict"):
        return
    with _lock:
        _counters[("doc_reindex_total", o)] = _counters.get(("doc_reindex_total", o), 0.0) + 1.0


def inc_purge() -> None:
    with _lock:
        _counters[("doc_purge_total",)] = _counters.get(("doc_purge_total",), 0.0) + 1.0


# ── Exposition ──

def _esc(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _bucket_lines(name: str, labels: str, observations: list[float],
                  help_text: str, exemplar: str | None) -> list[str]:
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    n = len(observations)
    total = sum(observations) if observations else 0.0
    for le in LE_BUCKETS:
        count = sum(1 for v in observations if v <= le)
        out.append(f'{name}_bucket{{{labels},le="{le}"}} {count}')
    # +Inf bucket carries the exemplar (Prometheus 0.0.4 `# {trace_id="…"}` suffix).
    inf_line = f'{name}_bucket{{{labels},le="+Inf"}} {n}'
    if exemplar:
        inf_line += f' # {{trace_id="{_esc(exemplar)}"}} {observations[-1]:.3f}' if observations else \
            f' # {{trace_id="{_esc(exemplar)}"}} 0'
    out.append(inf_line)
    out.append(f"{name}_count{{{labels}}} {n}")
    out.append(f"{name}_sum{{{labels}}} {total:.6f}")
    return out


def active_series_count() -> int:
    with _lock:
        return len(_counters) + len(_histograms)


def render_prometheus(pool_stats_fn=None, image: str | None = None) -> str:
    """Render the frozen catalogue (spec §3.3) in Prometheus 0.0.4 text format."""
    from app.core.config import settings as _settings

    with _lock:
        counters = dict(_counters)
        histograms = {k: list(v) for k, v in _histograms.items()}
        exemplars = dict(_hist_exemplars)
        shed = _shed_total
    lines: list[str] = []
    svc = f'service="{SERVICE}"'

    def counter_family(name: str, help_text: str, items: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        if not items:
            return
        for label_str, value in items:
            if value != value:  # NaN guard
                continue
            lines.append(f"{name}{{{label_str}}} {value:g}")

    # Stable ordering for deterministic scrapes/tests.
    http_h = sorted(((k, v) for k, v in histograms.items() if k[0] == "http_server_duration_seconds"),
                    key=lambda kv: (kv[0][1], kv[0][2]))
    for ( _n, route, code), obs in http_h:
        labels = f'{svc},route="{_esc(route)}",code="{_esc(code)}"'
        lines.extend(_bucket_lines("http_server_duration_seconds", labels, obs,
                                   "Request latency by route and code.",
                                   exemplars.get(("http_server_duration_seconds", route, code))))
    if not http_h:
        lines.extend(_bucket_lines(
            "http_server_duration_seconds",
            f'{svc},route="POST /api/v1/query",code="200"', [], "Request latency by route and code.", None))

    req_items = sorted(
        (f'{svc},route="{_esc(k[1])}",code="{_esc(k[2])}"', v)
        for k, v in counters.items() if k[0] == "http_server_requests_total")
    counter_family("http_server_requests_total", "Request rate and error ratio.", req_items)

    rl_items = sorted(
        (f'{svc},bucket="{_esc(k[1])}"', v)
        for k, v in counters.items() if k[0] == "http_rate_limited_total")
    counter_family("http_rate_limited_total", "Rate-limited requests by bucket.", rl_items)

    tok_items = sorted(
        (f'{svc},model="{_esc(k[1])}",kind="{_esc(k[2])}"', v)
        for k, v in counters.items() if k[0] == "llm_tokens_total")
    counter_family("llm_tokens_total", "Token consumption by model and kind.", tok_items)
    if not tok_items:
        lines.append(f'llm_tokens_total{{{svc},model="gemini-3.6-flash",kind="prompt"}} 0')

    fb_items = sorted(
        (f'{svc},from_model="{_esc(k[1])}",to_model="{_esc(k[2])}",code="{_esc(k[3])}"', v)
        for k, v in counters.items() if k[0] == "llm_fallback_total")
    counter_family("llm_fallback_total", "Primary-to-fallback transitions by reason.", fb_items)
    if not fb_items:
        lines.append(f'llm_fallback_total{{{svc},from_model="gemini-3.6-flash",'
                     f'to_model="openai/gpt-oss-120b",code="LLM_TIMEOUT"}} 0')

    for ( _n, model, outcome), obs in sorted(
            ((k, v) for k, v in histograms.items() if k[0] == "llm_request_duration_seconds"),
            key=lambda kv: (kv[0][1], kv[0][2])):
        labels = f'{svc},model="{_esc(model)}",outcome="{_esc(outcome)}"'
        lines.extend(_bucket_lines("llm_request_duration_seconds", labels, obs,
                                   "LLM request latency by model and outcome.",
                                   exemplars.get(("llm_request_duration_seconds", model, outcome))))

    for ( _n, outcome), obs in sorted(
            ((k, v) for k, v in histograms.items() if k[0] == "vector_search_duration_seconds"),
            key=lambda kv: kv[0][1]):
        labels = f'{svc},outcome="{_esc(outcome)}"'
        lines.extend(_bucket_lines("vector_search_duration_seconds", labels, obs,
                                   "Vector search latency by outcome.",
                                   exemplars.get(("vector_search_duration_seconds", outcome))))
    if not any(k[0] == "vector_search_duration_seconds" for k in histograms):
        lines.extend(_bucket_lines("vector_search_duration_seconds", f'{svc},outcome="ok"',
                                   [], "Vector search latency by outcome.", None))

    for ( _n, model, cached), obs in sorted(
            ((k, v) for k, v in histograms.items() if k[0] == "embed_duration_seconds"),
            key=lambda kv: (kv[0][1], kv[0][2])):
        labels = f'{svc},model="{_esc(model)}",cached="{_esc(cached)}"'
        lines.extend(_bucket_lines("embed_duration_seconds", labels, obs,
                                   "Embedding latency by model and cache hit.", None))

    # Pool gauges are live (not stored): read at scrape time.
    try:
        pool = pool_stats_fn() if pool_stats_fn is not None else {"in_use": 0, "idle": 0}
        in_use = float(pool.get("in_use", 0) or 0)
        idle = float(pool.get("idle", 0) or 0)
    except Exception:
        in_use, idle = 0.0, 0.0
    lines.append("# HELP db_pool_in_use DB pool checked-out connections.")
    lines.append("# TYPE db_pool_in_use gauge")
    lines.append(f'db_pool_in_use{{{svc}}} {in_use:g}')
    lines.append("# HELP db_pool_idle DB pool idle connections.")
    lines.append("# TYPE db_pool_idle gauge")
    lines.append(f'db_pool_idle{{{svc}}} {idle:g}')

    for ( _n, outcome), obs in sorted(
            ((k, v) for k, v in histograms.items() if k[0] == "db_pool_wait_seconds"),
            key=lambda kv: kv[0][1]):
        labels = f'{svc},outcome="{_esc(outcome)}"'
        lines.extend(_bucket_lines("db_pool_wait_seconds", labels, obs,
                                   "DB pool acquire wait by outcome.", None))

    err_items = sorted(
        (f'{svc},code="{_esc(k[1])}"', v)
        for k, v in counters.items() if k[0] == "db_errors_total")
    counter_family("db_errors_total", "DB pool and readiness failures.", err_items)

    for ( _n, route, outcome), obs in sorted(
            ((k, v) for k, v in histograms.items() if k[0] == "sse_stream_duration_seconds"),
            key=lambda kv: (kv[0][1], kv[0][2])):
        labels = f'{svc},route="{_esc(route)}",outcome="{_esc(outcome)}"'
        lines.extend(_bucket_lines("sse_stream_duration_seconds", labels, obs,
                                   "SSE stream lifetime by route and outcome.", None))

    sse_items = sorted(
        (f'{svc},model="{_esc(k[1])}"', v)
        for k, v in counters.items() if k[0] == "sse_tokens_total")
    counter_family("sse_tokens_total", "Streamed token frames by model.", sse_items)

    hist_val = counters.get(("history_tokens_total",), 0.0)
    counter_family("history_tokens_total", "Injected history tokens.",
                   [(svc, hist_val)] if hist_val else [])

    re_items = sorted(
        (f'{svc},outcome="{_esc(k[1])}"', v)
        for k, v in counters.items() if k[0] == "doc_reindex_total")
    counter_family("doc_reindex_total", "Document reindex jobs by outcome.", re_items)

    purge_val = counters.get(("doc_purge_total",), 0.0)
    counter_family("doc_purge_total", "Document hard purges.",
                   [(svc, purge_val)] if purge_val else [])

    img = (image or "").strip() or "unknown"
    lines.append("# HELP kromarag_info Build identity.")
    lines.append("# TYPE kromarag_info gauge")
    lines.append(f'kromarag_info{{{svc},version="{VERSION}",image="{_esc(img)}"}} 1')

    lines.append("# HELP kromarag_metrics_cardinality_shed_total Dropped high-cardinality increments.")
    lines.append("# TYPE kromarag_metrics_cardinality_shed_total counter")
    lines.append(f'kromarag_metrics_cardinality_shed_total{{{svc}}} {shed:g}')

    _ = _settings  # keep settings import live for future version wiring
    return "\n".join(lines) + "\n"

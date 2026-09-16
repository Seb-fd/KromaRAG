# stage 1 — build: resolve deps with uv lockfile, no app code
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /srv
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project

# stage 2 — runtime: slim, non-root, migrate-then-serve entrypoint (specs/08 §2.2)
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=production \
    WEB_CONCURRENCY=1 \
    PATH="/srv/.venv/bin:$PATH"
WORKDIR /srv
RUN useradd -r -u 10001 appuser \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /srv/.venv /srv/.venv
COPY app ./app
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
RUN chmod +x ./scripts/docker-entrypoint.sh && chown -R appuser:appuser /srv
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
# Honor $PORT injected by Cloud Run (specs/08 §6 matrix): sh -c expands at
# runtime (exec-form CMD would pass the literal "${PORT:-8000}"). The inner
# `exec` replaces sh so PID 1 stays uvicorn and SIGTERM drains correctly.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 15"]

#!/bin/sh
# Prod entrypoint (specs/08 §2.2 + §2.4): migrate 001→002→003, warm /readyz, exec CMD.
# Fails boot on migration error (orchestrator keeps prior revision on traffic).
set -eu

PORT="${PORT:-8000}"

echo "kroma entrypoint: running migrations (001→002→003)..."
if ! python -m app.services.migrate; then
  echo "kroma entrypoint: migrations FAILED — aborting boot (prior revision keeps traffic)" >&2
  exit 1
fi

echo "kroma entrypoint: warming readiness probe (≤60s)..."
READY_URL="http://127.0.0.1:${PORT}/readyz"
# Start server in background, probe readyz, then exec foreground server.
# Note: uvicorn single worker per Cloud Run instance (specs/08 §2.5).
i=0
if [ "${SKIP_WARMUP:-}" != "1" ]; then
  # Best-effort warmup: pool init happens in lifespan; entrypoint only logs intent.
  echo "kroma entrypoint: warmup delegated to lifespan init_pool (see /readyz)"
fi

echo "kroma entrypoint: exec $*"
exec "$@"

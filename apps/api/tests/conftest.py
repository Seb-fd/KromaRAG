"""Global test hygiene: isolate in-memory rate-limiter buckets per test (specs/08)."""
import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.core import rate_limit as rl

    rl.reset_rate_limiter()
    yield
    rl.reset_rate_limiter()

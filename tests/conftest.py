"""Hermetic settings. Tests must not depend on whatever is in the developer's
.env — a placeholder there should fail the app, not the suite.
Real environment variables beat .env in pydantic-settings, so pinning them here,
before any registerwatch module is imported, is enough."""

from __future__ import annotations

import os
import pathlib

import pytest

os.environ.update({
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "USER_AGENT": "RegisterWatch/test (+https://registerwatch.test/bot; ops@registerwatch.test)",
    "BLOB_BACKEND": "local",
    "INGEST_TOKEN": "",
    "READ_TOKEN": "",
    "MIN_REFETCH_INTERVAL_H": "20",
    # Most tests send many requests from one client in well under a minute.
    # tests/test_ratelimit.py turns the limits on for itself.
    "RATE_LIMIT_READ_PER_MIN": "0",
    "RATE_LIMIT_SEARCH_PER_MIN": "0",
    "RATE_LIMIT_INGEST_PER_MIN": "0",
})

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """What a process keeps between requests (data versions, rate-limit buckets)
    starts empty in every test, as it does in a new process."""
    from registerwatch.http import caching, ratelimit

    caching.VERSIONS.clear()
    ratelimit.BUCKETS.clear()
    yield


@pytest.fixture
def configure(monkeypatch):
    """`configure(DB_POOL_MAX=2, ...)`: the real settings, read from these
    environment variables, and the app's real pools opened from them on first
    use. Both are forgotten afterwards, so the next test starts clean."""
    from registerwatch import config
    from registerwatch.db import engine

    def apply(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, str(value))
        engine.close_pools()
        config.settings.cache_clear()
        return config.settings()

    yield apply
    engine.close_pools()
    config.settings.cache_clear()

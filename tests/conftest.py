"""Hermetic settings. Tests must not depend on whatever is in the developer's
.env — a placeholder there should fail the app, not the suite.
Real environment variables beat .env in pydantic-settings, so pinning them here,
before any registerwatch module is imported, is enough."""

from __future__ import annotations

import os
import pathlib

os.environ.update({
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "USER_AGENT": "RegisterWatch/test (+https://registerwatch.test/bot; ops@registerwatch.test)",
    "BLOB_BACKEND": "local",
    "INGEST_TOKEN": "",
    "MIN_REFETCH_INTERVAL_H": "20",
})

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

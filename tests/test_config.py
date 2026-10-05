from __future__ import annotations

import pytest
from pydantic import ValidationError

from registerwatch.config import Settings


@pytest.mark.parametrize("field, value", [
    ("DATABASE_URL", "postgresql://postgres.<ref>:<pw>@host/db"),
    ("USER_AGENT", "RegisterWatch/0.1 (+https://<your-site>/bot; <your-email>)"),
])
def test_a_placeholder_left_in_the_env_is_rejected(monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(_env_file=None)


def test_blob_backend_is_checked(monkeypatch):
    monkeypatch.setenv("BLOB_BACKEND", "ftp")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

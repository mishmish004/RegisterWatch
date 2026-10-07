from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from registerwatch.config import Settings, settings


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


@pytest.fixture
def fresh_settings():
    settings.cache_clear()
    yield settings
    settings.cache_clear()


@pytest.mark.parametrize("field", ["INGEST_TOKEN", "READ_TOKEN"])
def test_a_short_token_is_refused_at_startup(monkeypatch, fresh_settings, field):
    """T6.3.a: 10 characters fail settings(); a token_urlsafe(32) (43 characters) loads;
    the error names the field but never repeats the value."""
    short = "s3cr3t-10c"
    monkeypatch.setenv(field, short)
    with pytest.raises(ValidationError, match="at least 32 characters") as exc:
        fresh_settings()
    assert field.lower() in str(exc.value) and short not in str(exc.value)
    fresh_settings.cache_clear()
    good = secrets.token_urlsafe(32)
    assert len(good) == 43
    monkeypatch.setenv(field, good)
    assert getattr(fresh_settings(), field.lower()) == good


def test_an_empty_token_still_means_off(monkeypatch, fresh_settings):
    monkeypatch.setenv("INGEST_TOKEN", "")
    monkeypatch.setenv("READ_TOKEN", "")
    s = fresh_settings()
    assert s.ingest_token == "" and s.read_token == ""

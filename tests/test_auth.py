"""Phase 6: how v1 (and, for the comparison and the logs, legacy) routes check
bearer tokens. The spec side is in test_contract.py; startup strength in
test_config.py. No database: a request that gets past the check is a pass."""

from __future__ import annotations

import contextlib
import logging
import secrets
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.db.repos import ingest_runs as ingest_repo
from registerwatch.http import deps

READ_TOKEN = "read-" + secrets.token_urlsafe(32)
INGEST_TOKEN = "ingest-" + secrets.token_urlsafe(32)
READ = {"Authorization": f"Bearer {READ_TOKEN}"}
INGEST = {"Authorization": f"Bearer {INGEST_TOKEN}"}
CHALLENGE = 'Bearer realm="registerwatch"'
INVALID = 'Bearer realm="registerwatch", error="invalid_token"'
READ_ROUTE = "/v1/registers"
INGEST_ROUTES = (("post", "/v1/ingest-runs"), ("get", "/v1/ingest-runs"), ("get", f"/v1/ingest-runs/{uuid.uuid4()}"))


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token=INGEST_TOKEN, read_token=READ_TOKEN, stale_after_h=26.0, log_level="DEBUG")
    for module in (deps, api):
        monkeypatch.setattr(module, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(deps.repo, "source_health", lambda conn: [])
    monkeypatch.setattr(api.runs, "sweep_on_startup", lambda: None)
    # Past the check, ingest routes find no runs, and a POST finds one already going (409).
    monkeypatch.setattr(ingest_repo, "page", lambda conn, **kw: [])
    monkeypatch.setattr(ingest_repo, "get", lambda conn, id: None)
    monkeypatch.setattr(ingest_repo, "active", lambda conn: None)
    monkeypatch.setattr(api.runs, "acquire", lambda: None)
    with TestClient(api.app) as c:
        c.cfg = cfg
        yield c


def _is_problem(r, slug: str) -> bool:
    return (r.headers["content-type"].startswith("application/problem+json")
            and r.json()["type"].endswith(f"#{slug}") and r.json()["status"] == r.status_code)


def test_a_missing_token_is_a_bare_challenge(client):
    """T6.2.a: no bearer token at all (none, or another scheme) → 401 with the
    realm and no `error`, which RFC 6750 3.1 keeps for a token that was sent."""
    for headers in ({}, {"Authorization": f"Basic {READ_TOKEN}"}, {"Authorization": "Bearer"}):
        r = client.get(READ_ROUTE, headers=headers)
        assert r.status_code == 401 and _is_problem(r, "unauthenticated"), headers
        assert r.headers["www-authenticate"] == CHALLENGE, headers
    for method, path in INGEST_ROUTES:
        r = getattr(client, method)(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == CHALLENGE, path
    assert client.get(READ_ROUTE, headers=READ).status_code == 200
    assert client.get(READ_ROUTE, headers=INGEST).status_code == 200  # the ingest token reads


def test_reads_stay_open_without_a_read_token(client):
    client.cfg.read_token = ""
    assert client.get(READ_ROUTE).status_code == 200
    assert client.get(READ_ROUTE, headers={"Authorization": "Bearer anything"}).status_code == 200


def test_a_wrong_token_is_invalid_token(client):
    """T6.2.b: a bearer token that matches nothing → `error="invalid_token"`, on read
    and ingest routes. A Latin-1 token is compared as bytes, so it is a 401, not a 500."""
    wrong = [{"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {READ_TOKEN}x"},
             {"Authorization": f"Bearer {READ_TOKEN[:-1]}"}, {"Authorization": "Bearer caf\xe9".encode("latin-1")}]
    for headers in wrong:
        r = client.get(READ_ROUTE, headers=headers)
        assert r.status_code == 401 and _is_problem(r, "unauthenticated"), headers
        assert r.headers["www-authenticate"] == INVALID, headers
        for method, path in INGEST_ROUTES:
            r = getattr(client, method)(path, headers=headers)
            assert r.status_code == 401 and r.headers["www-authenticate"] == INVALID, (path, headers)


def test_a_read_token_on_ingest_is_forbidden(client):
    """T6.2.c: the read token is a valid token without the right → 403 `forbidden`, no
    challenge. The ingest token gets past the check (to the fakes: 409, 200, 404)."""
    for (method, path), past in zip(INGEST_ROUTES, (409, 200, 404)):
        r = getattr(client, method)(path, headers=READ)
        assert r.status_code == 403 and _is_problem(r, "forbidden"), path
        assert "www-authenticate" not in r.headers
        assert getattr(client, method)(path, headers=INGEST).status_code == past, path


def test_auth_uses_compare_digest(client, monkeypatch):
    """T6.2.d: every configured token is compared with secrets.compare_digest, every
    time, even after one matched, so timing does not say which one (or how much of it) did."""
    calls: list[bytes] = []
    real = secrets.compare_digest

    def spy(a, b):
        calls.append(b)
        return real(a, b)

    monkeypatch.setattr(deps.secrets, "compare_digest", spy)
    read, ingest = READ_TOKEN.encode(), INGEST_TOKEN.encode()
    cases = [
        (lambda: client.get(READ_ROUTE, headers=READ), [read, ingest]),
        (lambda: client.get(READ_ROUTE, headers=INGEST), [read, ingest]),
        (lambda: client.get(READ_ROUTE, headers={"Authorization": "Bearer wrong"}), [read, ingest]),
        (lambda: client.get("/v1/ingest-runs", headers=READ), [ingest, read]),
        (lambda: client.get("/v1/ingest-runs", headers={"Authorization": "Bearer wrong"}), [ingest, read]),
        # legacy compares the whole header, through the same helper
        (lambda: client.get("/jurisdictions", headers={"Authorization": "Bearer wrong"}),
         [b"Bearer " + read, b"Bearer " + ingest]),
        (lambda: client.get("/ingest/last", headers=READ), [b"Bearer " + ingest]),
    ]
    for send, expected in cases:
        calls.clear()
        send()
        assert calls == expected


def test_tokens_never_reach_the_logs(client, caplog):
    """T6.2.e: every outcome of the check, at DEBUG, with both tokens in play: neither
    token occurs in any log record, nor in any response body."""
    caplog.set_level(logging.DEBUG)
    bodies = []
    for headers in ({}, READ, INGEST, {"Authorization": "Bearer wrong"}, {"Authorization": READ_TOKEN}):
        bodies.append(client.get(READ_ROUTE, headers=headers).text)
        for method, path in INGEST_ROUTES:
            bodies.append(getattr(client, method)(path, headers=headers).text)
        bodies.append(client.get("/jurisdictions", headers=headers).text)
        bodies.append(client.get("/ingest/last", headers=headers).text)
    assert caplog.records, "nothing was logged at DEBUG; the check would pass vacuously"
    for token in (READ_TOKEN, INGEST_TOKEN):
        for rec in caplog.records:
            text = rec.getMessage() + (rec.exc_text or "") + repr(getattr(rec, "args", ""))
            assert token not in text, rec
        assert not [b for b in bodies if token in b]

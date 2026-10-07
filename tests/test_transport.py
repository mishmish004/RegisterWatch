"""Phase 10: what every answer carries on the wire (registerwatch.http.transport,
plan.md P10.1, P10.4, P10.5), and the request body limit (http/guards.py, P10.4).

The database is faked as in test_caching.py; listRows answers N synthetic
licences, so a page can be 1000 real-looking rows. scripts/verify/net.sh checks
the same against the image (T10.1.d, T10.4, T10.5).
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from registerwatch import api, query
from registerwatch.http import deps, guards, transport
from tests.test_contract import _FakeConn

ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"
NOW = datetime(2026, 10, 7, 6, 0, 41, tzinfo=timezone.utc)
ORIGIN = "https://app.test"


def licences(n: int) -> list[dict]:
    return [{"id": i, "first_seen_snapshot_id": 7, "last_seen_snapshot_id": 7, "removed_snapshot_id": None,
             "first_seen_at": NOW, "last_seen_at": NOW, "account_number": str(200000 + i),
             "licence_number": f"000-{i:06d}-R-31{i % 997:04d}-001", "status": "Active" if i % 7 else "Revoked",
             "type": "Remote" if i % 2 else "Non-Remote", "activity": f"Casino {hashlib.md5(str(i).encode()).hexdigest()}",
             "start_date": None, "end_date": None} for i in range(1, n + 1)]


@pytest.fixture
def client(monkeypatch, configure):
    configure()
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    monkeypatch.setattr(deps.repo, "data_versions", lambda conn: [{"slug": "gb_ukgc", "complete": 7, "latest": 7}])
    monkeypatch.setattr(query, "latest_snapshot_id", lambda conn, reg: 7)
    monkeypatch.setattr(query, "row_page", lambda conn, reg, tbl, *, limit, **kw: licences(limit + 1))
    with TestClient(api.app) as c:
        yield c


def _csp(r) -> dict[str, list[str]]:
    return {d.split()[0]: d.split()[1:] for d in (x.strip() for x in r.headers["content-security-policy"].split(";"))}


# --- P10.1 security headers (T10.1.d) ------------------------------------------------------

@pytest.mark.parametrize("path, status", [(ROWS, 200), ("/v1/nope", 404), ("/livez", 200), ("/registers", 200),
                                          ("/openapi.json", 200)])
def test_every_answer_carries_the_security_headers(client, path, status):
    r = client.get(path)
    assert r.status_code == status
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert "strict-transport-security" not in r.headers  # plain http


def test_errors_from_the_middlewares_carry_them_too(client, monkeypatch):
    monkeypatch.setattr(query, "row_page", lambda *a, **kw: 1 / 0)
    for r in (client.get(ROWS), client.get(ROWS + "?q=a%00b"), client.post("/v1/ingest-runs", content=b"x" * 70000)):
        assert r.status_code in (500, 400, 413) and r.headers["x-content-type-options"] == "nosniff"


def test_hsts_only_when_the_request_came_over_https(configure):
    configure()
    with TestClient(api.app, base_url="https://testserver") as c:
        assert c.get("/livez").headers["strict-transport-security"] == "max-age=31536000"
    # Behind a proxy: https as the proxy says it, and only a proxy the server trusts.
    for trusted, hsts in (("testclient", True), ("10.255.255.1", False)):
        with TestClient(ProxyHeadersMiddleware(api.app, trusted_hosts=trusted)) as c:
            r = c.get("/livez", headers={"X-Forwarded-Proto": "https"})
            assert ("strict-transport-security" in r.headers) is hsts, trusted


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
def test_the_documentation_gets_a_policy_naming_its_own_scripts(client, path):
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", r.text, re.S)
    csp = _csp(r)
    hashes = sorted(f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'" for s in inline)
    assert sorted(x for x in csp["script-src"] if x.startswith("'sha256-")) == hashes
    assert "'unsafe-inline'" not in csp["script-src"] and "'unsafe-eval'" not in csp["script-src"]
    assert csp["default-src"] == ["'none'"] and csp["frame-ancestors"] == ["'none'"]
    assert csp["connect-src"] == ["'self'"]  # the spec, and Swagger UI's "try it out"
    if path != "/redoc":
        assert hashes  # Swagger UI starts from an inline script; Redoc does not
    for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', r.text):
        assert src.startswith("https://cdn.jsdelivr.net/"), src


# --- P10.4 compression (T10.4.a, T10.4.b) -------------------------------------------------

def test_a_1000_row_page_is_gzipped_to_a_quarter_or_less(client):
    plain = client.get(ROWS + "?limit=1000", headers={"Accept-Encoding": "identity"})
    with client.stream("GET", ROWS + "?limit=1000", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert r.headers["content-encoding"] == "gzip" and "content-encoding" not in plain.headers
    assert gzip.decompress(raw) == plain.content and len(plain.json()["data"]) == 1000
    assert len(raw) <= 0.25 * len(plain.content), (len(raw), len(plain.content))
    assert int(r.headers["content-length"]) == len(raw)
    # The weak ETag stands for both encodings; Vary names each field once.
    assert r.headers["etag"] == plain.headers["etag"]
    assert r.headers["vary"] == plain.headers["vary"] == "Authorization, Accept-Encoding"


def test_small_answers_are_not_compressed(client):
    for path in ("/livez", "/v1/registers/xx_nope", ROWS + "?limit=1"):
        r = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert len(r.content) < 1024 and "content-encoding" not in r.headers, path


def test_a_304_stays_empty(client):
    tag = client.get(ROWS).headers["etag"]
    r = client.get(ROWS, headers={"If-None-Match": tag, "Accept-Encoding": "gzip"})
    assert r.status_code == 304 and r.content == b"" and "content-encoding" not in r.headers


def test_the_documentation_policy_is_read_before_compression(client):
    with client.stream("GET", "/docs/oauth2-redirect", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    page = gzip.decompress(raw).decode()
    (script,) = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)
    assert base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() in r.headers["content-security-policy"]


# --- P10.4 body limit (T10.4.c) -----------------------------------------------------------

def test_a_body_declared_over_the_limit_is_refused_unread():
    """The middleware answers from the headers: it never calls `receive`, so a
    client waiting for `100 Continue` never sends the body."""
    import asyncio

    sent: list[dict] = []

    async def receive():
        raise AssertionError("the body was read")

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/ingest-runs", "raw_path": b"/v1/ingest-runs",
             "query_string": b"", "headers": [(b"content-length", b"2097152"), (b"expect", b"100-continue")],
             "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "root_path": ""}
    asyncio.run(guards.BodyLimitMiddleware(lambda *a: None)(scope, receive, send))
    assert sent[0]["status"] == 413 and (b"connection", b"close") in sent[0]["headers"]


def test_a_streamed_body_over_the_limit_is_a_413_too(client):
    def chunks():
        for _ in range(10):
            yield b" " * 10_000

    r = client.post("/v1/ingest-runs", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["type"].endswith("#content-too-large")
    assert r.headers["connection"] == "close"


def test_a_body_at_the_limit_is_read(client, configure):
    body = b'{"registers": ["gb_ukgc"]}'.ljust(guards.MAX_BODY_BYTES)
    r = client.post("/v1/ingest-runs", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 503  # past the body check: ingest is off in the hermetic settings


def test_legacy_routes_keep_their_shape_for_a_413(client):
    r = client.post("/ingest/pl_mf", content=b"x" * 70000)
    assert r.status_code == 413 and set(r.json()) == {"detail"}


# --- P10.5 CORS (T10.5.a, T10.5.b) --------------------------------------------------------

def _preflight(c, method: str, origin: str = ORIGIN):
    return c.options(ROWS, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                    "Access-Control-Request-Headers": "authorization"})


def test_no_cors_by_default(client):
    r = _preflight(client, "GET", "https://evil.test")
    assert r.status_code == 405 and "access-control-allow-origin" not in r.headers
    assert "access-control-allow-origin" not in client.get(ROWS, headers={"Origin": "https://evil.test"}).headers


def test_an_allowed_origin_may_read_with_get_only(client, configure):
    configure(CORS_ALLOW_ORIGINS=f"{ORIGIN}, https://other.test")
    ok = _preflight(client, "GET")
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
    assert ok.headers["access-control-allow-methods"] == "GET, HEAD"
    assert "authorization" in ok.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in ok.headers
    refused = _preflight(client, "POST")
    assert refused.status_code == 400 and "POST" not in refused.headers.get("access-control-allow-methods", "")
    # The read itself: allowed, with the headers a page needs to page and to back off.
    r = client.get(ROWS, headers={"Origin": ORIGIN})
    assert r.headers["access-control-allow-origin"] == ORIGIN and "Origin" in r.headers["vary"]
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"etag", "link", "ratelimit", "ratelimit-policy", "retry-after", "x-request-id"} <= exposed
    # Another origin, or another method from this one: nothing a browser would let a page read.
    assert "access-control-allow-origin" not in client.get(ROWS, headers={"Origin": "https://evil.test"}).headers
    post = client.post("/v1/ingest-runs", headers={"Origin": ORIGIN, "Content-Type": "text/plain"}, content=b"{}")
    assert "access-control-allow-origin" not in post.headers


def test_any_origin_without_credentials(client, configure):
    configure(CORS_ALLOW_ORIGINS="*")
    r = client.get(ROWS, headers={"Origin": "https://anyone.test"})
    assert r.headers["access-control-allow-origin"] == "*" and "access-control-allow-credentials" not in r.headers


@pytest.mark.parametrize("bad", ["https://app.test/", "app.test", "https://app.test/path", "ftp://app.test"])
def test_an_origin_that_could_never_match_is_refused_at_startup(configure, bad):
    with pytest.raises(ValueError, match="not an origin"):
        configure(CORS_ALLOW_ORIGINS=bad)


def test_preflights_are_not_rate_limited(client, configure):
    configure(CORS_ALLOW_ORIGINS=ORIGIN, RATE_LIMIT_READ_PER_MIN=1)
    assert all(_preflight(client, "GET").status_code == 200 for _ in range(3))
    assert client.get(ROWS).status_code == 200 and client.get(ROWS).status_code == 429


def test_transport_constants_match_the_plan():
    assert transport.GZIP_MIN_BYTES == 1024 and transport.HSTS == "max-age=31536000"
    assert guards.MAX_BODY_BYTES == 64 * 1024

"""Phase 8, P8.1: liveness, readiness and status, split (F13).

`/livez` touches nothing. `/readyz` is the database answering `SELECT 1` within
2 s. `/v1/status` is freshness: always a 200, unless `strict=true` asks for the
old 503. "Database down" is real here, not a patch: the app's own pools pointed
at a port where nothing listens (refused), or where the kernel accepts the
connection and nothing ever answers (hung, like a server that has stopped), or
[pg] through a proxy that stops passing bytes once the pool holds connections
(frozen).
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.http import deps, probes
from registerwatch.http.problems import Catalog
from registerwatch.ingest import runs
from registerwatch.registers import REGISTRY
from tests.test_contract import _FakeConn
from tests.test_postgres import DSN, db  # noqa: F401 — fixture

pg = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")

READ_TOKEN = "read-token-0123456789abcdefghijklmnopqrstuvwxyz"
SLUGS = sorted(REGISTRY)


class Down:
    """A database URL nothing answers at. `refused`: no one listens on the port.
    `hung`: a listening socket that never accepts, so the kernel completes the
    handshake and the client waits for a server that never speaks."""

    def __init__(self, kind: str) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.url = f"postgresql://rw:rw@127.0.0.1:{self.sock.getsockname()[1]}/rw"
        if kind == "hung":
            self.sock.listen(64)
        else:
            self.sock.close()

    def close(self) -> None:
        self.sock.close()


def _timed(client, path, **kw):
    started = time.monotonic()
    r = client.get(path, **kw)
    return r, time.monotonic() - started


# --- T8.1.a ------------------------------------------------------------------------------

# Refused fails fast, so the pool's own timeout ends the wait (1 s). Hung never
# fails, so with the default 3 s timeout it is /readyz's 2 s that ends it.
@pytest.mark.parametrize(("kind", "pool_timeout_s"), [("refused", 1), ("hung", 3)])
def test_with_the_database_down(kind, pool_timeout_s, configure, monkeypatch):  # T8.1.a
    down = Down(kind)
    configure(DATABASE_URL=down.url, DB_POOL_TIMEOUT_S=pool_timeout_s)
    monkeypatch.setattr(runs, "sweep_on_startup", lambda: None)  # would wait out its connect_timeout when hung
    with TestClient(api.app) as client:
        try:
            live, took = _timed(client, "/livez")
            assert live.status_code == 200 and live.json() == {"status": "ok"} and took < 0.5

            ready, took = _timed(client, "/readyz")
            assert ready.status_code == 503 and ready.headers["content-type"] == "application/problem+json"
            body = ready.json()
            assert body["type"] == Catalog.DATABASE_UNAVAILABLE.type and body["instance"] == "/readyz"
            assert body["request_id"] == ready.headers["x-request-id"] and ready.headers["retry-after"] == "5"
            assert took < min(pool_timeout_s, probes.READY_WITHIN_S) + 0.5, took

            status, took = _timed(client, "/v1/status")
            assert status.status_code == 200 and took < pool_timeout_s + 0.5
            body = status.json()
            assert body["stale"] is True and body["database"] == "unreachable"
            assert body["stale_registers"] == SLUGS
            assert all(r["stale"] and r["freshness"] is None and r["hours_since_good"] is None
                       for r in body["registers"])

            strict = client.get("/v1/status?strict=true")
            assert strict.status_code == 503 and strict.headers["content-type"] == "application/json"
            assert strict.json()["database"] == "unreachable" and strict.json()["stale"] is True
        finally:
            down.close()  # before the pool closes, so its connection attempts end


class Freezer:
    """A TCP proxy in front of the test database that can stop passing bytes
    while every connection stays open: a server that has stopped answering
    (paused, swapped out, a network gone quiet after the handshake). Unlike
    `Down`, the pool already holds connections when it happens, and a query sent
    on one waits for an answer no timeout of the database's own can end."""

    def __init__(self, dsn: str) -> None:
        upstream = psycopg.conninfo.conninfo_to_dict(dsn)
        self.upstream = (upstream.get("host") or "localhost", int(upstream.get("port") or 5432))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = psycopg.conninfo.make_conninfo(dsn, host="127.0.0.1", port=self.listener.getsockname()[1])
        self.flowing = threading.Event()
        self.flowing.set()
        self.socks: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            server = socket.create_connection(self.upstream)
            self.socks += [client, server]
            for src, dst in ((client, server), (server, client)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while data := src.recv(65536):
                self.flowing.wait()
                dst.sendall(data)
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)

    def close(self) -> None:
        self.flowing.set()
        self.listener.close()
        for sock in self.socks:
            with contextlib.suppress(OSError):
                sock.close()


@pg
def test_with_the_database_frozen(db, configure, monkeypatch):  # noqa: F811 — T8.1.a, a third way to be down
    """Up, then frozen with the pool's connections open, then back."""
    freezer = Freezer(DSN)
    configure(DATABASE_URL=freezer.url)
    with TestClient(api.app) as client:
        try:
            assert client.get("/readyz").status_code == 200
            assert client.get("/v1/status").json()["database"] == "ok"
            freezer.flowing.clear()
            unstick = threading.Timer(10, freezer.flowing.set)  # a hang fails the test instead of hanging it
            unstick.start()

            live, took = _timed(client, "/livez")
            assert live.status_code == 200 and took < 0.5, took
            for path, code in (("/readyz", 503), ("/v1/status", 200), ("/v1/status?strict=true", 503)):
                r, took = _timed(client, path)
                assert r.status_code == code, (path, r.text)
                assert probes.READY_WITHIN_S <= took < probes.READY_WITHIN_S + 0.5, (path, took)
                if path.startswith("/v1/status"):
                    assert r.json()["database"] == "unreachable" and r.json()["stale"] is True, path

            unstick.cancel()
            freezer.flowing.set()  # the stuck queries finish and give their connections back
            deadline = time.monotonic() + 5
            while (ready := client.get("/readyz")).status_code != 200 and time.monotonic() < deadline:
                pass
            assert ready.status_code == 200
            assert client.get("/v1/status").json()["database"] == "ok"
        finally:
            freezer.close()


# --- T8.1.b ------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """A database that answers: every register freshly ingested except those
    `client.ages` makes older (hours), or never (None)."""
    cfg = SimpleNamespace(ingest_token="", read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(deps, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    ages: dict[str, float | None] = {}
    now = datetime.now(timezone.utc)

    def source_health(conn):
        rows = []
        for slug in SLUGS:
            age = ages.get(slug, 3.0)
            good = None if age is None else now - timedelta(hours=age)
            rows.append({"slug": slug, "last_fetch": now - timedelta(hours=1), "last_good": good,
                         "last_reason": None if age is None or age < 26 else "PAGE_GAP:0/1 x:HTTP_503",
                         "failed_7d": 0 if age is None or age < 26 else 2})
        return rows

    monkeypatch.setattr(deps.repo, "source_health", source_health)
    with TestClient(api.app) as c:
        c.cfg, c.ages = cfg, ages
        yield c


def test_one_stale_register(client):  # T8.1.b
    client.ages["ch_esbk"] = 30.0
    assert client.get("/readyz").json() == {"status": "ok"}
    r = client.get("/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True and body["database"] == "ok" and body["stale_registers"] == ["ch_esbk"]
    entry = {e["slug"]: e for e in body["registers"]}
    assert entry["ch_esbk"]["stale"] is True and entry["ch_esbk"]["hours_since_good"] == 30.0
    assert entry["ch_esbk"]["freshness"]["last_reason"] == "PAGE_GAP:0/1 x:HTTP_503"
    assert entry["ch_esbk"]["jurisdiction"] == "CH" and entry["ch_esbk"]["url"] == "/v1/registers/ch_esbk"
    assert [e["slug"] for e in body["registers"]] == SLUGS
    assert not any(e["stale"] for s, e in entry.items() if s != "ch_esbk")
    strict = client.get("/v1/status?strict=true")
    assert strict.status_code == 503 and strict.json()["stale_registers"] == ["ch_esbk"]


def test_never_ingested_is_stale_and_fresh_is_200_even_when_strict(client):
    client.ages["pl_mf"] = None
    body = client.get("/v1/status").json()
    assert body["stale_registers"] == ["pl_mf"] and body["registers"][SLUGS.index("pl_mf")]["hours_since_good"] is None
    client.ages.clear()
    for path in ("/v1/status", "/v1/status?strict=true", "/v1/status?strict=false"):
        r = client.get(path)
        assert r.status_code == 200 and r.json()["stale"] is False and r.json()["stale_registers"] == [], path
    # Stale means older than stale_after_h, as in legacy /status.
    client.ages["ch_esbk"] = 25.9
    assert client.get("/v1/status?strict=true").status_code == 200


def test_a_parameter_status_does_not_take_is_a_400(client):
    """An uptime monitor's typo must not quietly mean "not strict"."""
    for path, field in (("/v1/status?stict=true", "stict"), ("/v1/status?strict=maybe", "strict")):
        r = client.get(path)
        assert r.status_code == 400 and r.json()["errors"][0]["field"] == field, path


def test_probes_and_status_are_open_and_never_stored(client):
    client.cfg.read_token = READ_TOKEN
    assert client.get("/v1/registers").status_code == 401  # reads now need the token
    for path in ("/livez", "/readyz", "/v1/status"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-store" and "etag" not in r.headers, path


@contextlib.contextmanager
def _stuck():
    """A transaction whose first statement does not come back for a while: a
    server that has stopped answering, or a network gone quiet. No timeout of
    the database's own can end that wait."""
    time.sleep(3)
    yield _FakeConn()


def test_readyz_and_status_answer_in_time_when_the_database_does_not(client, monkeypatch):
    """The answers come at 2 s; the stuck queries are left to finish on their own."""
    monkeypatch.setattr(deps, "tx", _stuck)
    r, took = _timed(client, "/readyz")
    assert r.status_code == 503 and r.json()["type"] == Catalog.DATABASE_UNAVAILABLE.type
    assert probes.READY_WITHIN_S <= took < probes.READY_WITHIN_S + 0.5, took
    for path, code in (("/v1/status", 200), ("/v1/status?strict=true", 503)):
        r, took = _timed(client, path)
        assert r.status_code == code and r.json()["database"] == "unreachable", path
        assert r.json()["stale_registers"] == SLUGS, path
        assert probes.READY_WITHIN_S <= took < probes.READY_WITHIN_S + 0.5, (path, took)
    assert client.get("/livez").status_code == 200


def test_the_spec_has_the_probes_and_status():
    spec = api.app.openapi()
    ops = {op["operationId"]: (path, op) for path, item in spec["paths"].items() for op in item.values()}
    assert ops["livez"][0] == "/livez" and ops["readyz"][0] == "/readyz" and ops["getStatus"][0] == "/v1/status"
    for op_id in ("livez", "readyz", "getStatus"):
        _, op = ops[op_id]
        assert op["security"] == [] and op["tags"] == ["operations"], op_id
        assert "304" not in op["responses"] and "ETag" not in op["responses"]["200"]["headers"], op_id
        assert "Cache-Control" in op["responses"]["200"]["headers"], op_id
    # Probes are never rate limited, so they document no 429 and no RateLimit fields.
    for op_id in ("livez", "readyz"):
        _, op = ops[op_id]
        assert "429" not in op["responses"] and "RateLimit" not in op["responses"]["200"]["headers"], op_id
    assert ops["readyz"][1]["responses"]["503"] == {"$ref": "#/components/responses/ServiceUnavailable"}
    strict = ops["getStatus"][1]["responses"]["503"]["content"]["application/json"]["schema"]
    assert strict == {"$ref": "#/components/schemas/Status"}
    assert [p["name"] for p in ops["getStatus"][1]["parameters"]] == ["strict"]

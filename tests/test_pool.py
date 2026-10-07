"""Phase 8, P8.2: the database pool under pressure (F14).

Reads get DB_POOL_MAX connections and wait at most DB_POOL_TIMEOUT_S for one,
then answer 503 `database-unavailable`, never 500. Ingest has two connections
of its own, so a batch cannot starve reads. Sync handlers run in
2 x DB_POOL_MAX threads, so a burst queues for a thread instead of timing out
on the pool.

The [pg] tests run the real app under uvicorn, with its real pools, threads and
run lock, against the test database. The rows query is slowed by a
`pg_sleep(1)` in front of it, so the pool is what they measure. The engine is
faked: it takes both of ingest's connections and holds them.
"""

from __future__ import annotations

import contextlib
import math
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio
import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from registerwatch import api, query
from registerwatch.db import engine as pools
from registerwatch.fetch import limiter
from registerwatch.http import deps
from registerwatch.http.problems import Catalog
from registerwatch.ingest import engine as ingest_engine
from registerwatch.ingest import runs
from registerwatch.ingest.engine import IngestResult
from tests.test_postgres import DSN, db, loaded  # noqa: F401 — fixtures

pg = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")

INGEST_TOKEN = "ingest-token-0123456789abcdefghijklmnopqrstuvwxyz"
AS_INGEST = {"Authorization": f"Bearer {INGEST_TOKEN}"}
ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"
OURS = "SELECT application_name AS name, count(*) AS n FROM pg_stat_activity " \
       "WHERE application_name LIKE 'registerwatch%' GROUP BY 1"


# --- the pools themselves -------------------------------------------------------------

class FakePool:
    """Records how it was made; slow to make, like a real pool opening."""

    made: list[FakePool] = []

    def __init__(self, conninfo, **kw):
        time.sleep(0.05)
        self.conninfo, self.kw, self.closed = conninfo, kw, False
        FakePool.made.append(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pools(configure, monkeypatch):
    FakePool.made = []
    monkeypatch.setattr(pools, "ConnectionPool", FakePool)
    return configure


def test_one_pool_however_many_threads_ask_at_once(fake_pools):
    """Requests arriving together at startup must not each open a pool: every
    extra one is DB_POOL_MAX more connections nobody counted."""
    fake_pools(DB_POOL_MAX=4)
    start = threading.Barrier(16)
    got: list[object] = []

    def ask(which):
        start.wait()
        got.append(which())

    threads = [threading.Thread(target=ask, args=(pools.pool if i % 2 else pools.ingest_pool,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(FakePool.made) == 2 and len({id(p) for p in got}) == 2
    pools.close_pools()
    assert all(p.closed for p in FakePool.made)
    assert pools.pool() is not FakePool.made[0]  # closed pools are forgotten: the next use opens a new one


def test_each_pool_takes_its_size_and_name(fake_pools, monkeypatch):
    fake_pools(DB_POOL_MAX=7, DB_POOL_TIMEOUT_S=1.5)
    read, ingest = pools.pool(), pools.ingest_pool()
    assert (read.kw["min_size"], read.kw["max_size"], read.kw["timeout"]) == (1, 7, 1.5)
    assert read.kw["kwargs"]["application_name"] == "registerwatch"
    assert (ingest.kw["min_size"], ingest.kw["max_size"]) == (0, pools.INGEST_POOL_MAX) == (0, 2)
    assert ingest.kw["kwargs"]["application_name"] == "registerwatch-ingest"
    seen = {}
    monkeypatch.setattr(runs.psycopg, "connect", lambda url, **kw: seen.update(kw))
    runs.connect()
    assert seen["application_name"] == "registerwatch-lock"


def test_ingest_goes_through_its_own_pool():
    """Everything an ingest run does to the database, and nothing a read does."""
    assert ingest_engine.tx is pools.ingest_tx and limiter.tx is pools.ingest_tx and runs.tx is pools.ingest_tx
    assert deps.tx is pools.read_tx


def test_the_thread_limit_is_twice_the_pool(configure, monkeypatch):
    configure(DB_POOL_MAX=3)
    monkeypatch.setattr(runs, "sweep_on_startup", lambda: None)
    with TestClient(api.app) as client:
        tokens = client.portal.call(lambda: anyio.to_thread.current_default_thread_limiter().total_tokens)
    assert tokens == 6


def test_a_pool_timeout_is_a_503_with_retry_after(configure, monkeypatch):
    """A real pool with every connection taken: the next read waits
    DB_POOL_TIMEOUT_S, then a 503 problem, not a 500."""
    nobody = socket.socket()
    nobody.bind(("127.0.0.1", 0))
    nobody.listen(8)  # accepts into the backlog, never answers: the pool never gets a connection
    configure(DATABASE_URL=f"postgresql://rw:rw@127.0.0.1:{nobody.getsockname()[1]}/rw", DB_POOL_TIMEOUT_S=0.5)
    monkeypatch.setattr(runs, "sweep_on_startup", lambda: None)
    with TestClient(api.app) as client:
        try:
            started = time.monotonic()
            r = client.get(ROWS)
            took = time.monotonic() - started
        finally:
            nobody.close()
    assert r.status_code == 503 and r.headers["content-type"] == "application/problem+json"
    assert r.json()["type"] == Catalog.DATABASE_UNAVAILABLE.type and r.headers["retry-after"] == "5"
    assert 0.5 <= took < 1.5, took


# --- the app under uvicorn, on the test database [pg] ------------------------------------

class Holding:
    """engine.ingest that takes both of ingest's connections and keeps them until `go`."""

    def __init__(self):
        self.held, self.go = threading.Event(), threading.Event()

    def __call__(self, register, store, **kw):
        with pools.ingest_tx() as a, pools.ingest_tx() as b:
            a.execute("SELECT 1")
            b.execute("SELECT 1")
            self.held.set()
            assert self.go.wait(60), "the test never let the engine go"
        return IngestResult(register.slug, None, True, None, 1, 1, record_count=10)


@pytest.fixture
def serve(configure, loaded, monkeypatch):  # noqa: F811
    """`with serve(slow=True) as app:` the real app under uvicorn on a free port,
    DB_POOL_MAX=2 and the default DB_POOL_TIMEOUT_S (3 s), on the test database
    with every register's rows. `slow` puts a `pg_sleep(1)` before each rows query."""
    with loaded() as c:
        c.execute("TRUNCATE ingest_runs CASCADE")
    cfg = configure(DATABASE_URL=DSN, DB_POOL_MAX=2, INGEST_TOKEN=INGEST_TOKEN)
    assert cfg.db_pool_timeout_s == 3.0
    engine = Holding()
    monkeypatch.setattr(runs.engine, "ingest", engine)
    monkeypatch.setattr(runs, "make_store", lambda: object())

    @contextlib.contextmanager
    def serving(slow: bool = False):
        if slow:
            real = query.row_page

            def slow_page(conn, *a, **kw):
                conn.execute("SELECT pg_sleep(1)")
                return real(conn, *a, **kw)

            monkeypatch.setattr(query, "row_page", slow_page)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(api.app, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.01)
        try:
            yield SimpleNamespace(url=f"http://127.0.0.1:{sock.getsockname()[1]}", engine=engine, cfg=cfg)
        finally:
            engine.go.set()
            server.should_exit = True
            thread.join(30)
            sock.close()

    yield serving
    engine.go.set()


def _get(url: str) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    r = httpx.get(url, timeout=60)
    return r, time.monotonic() - started


def _burst(base: str, path: str, n: int) -> list[tuple[httpx.Response, float]]:
    """`n` requests at once. Each client is connected beforehand: making one
    costs tens of milliseconds (its TLS context alone), which would spread the
    burst out."""
    clients = [httpx.Client(base_url=base, timeout=60) for _ in range(n)]
    for c in clients:
        c.get("/livez")
    together = threading.Barrier(n)

    def one(c: httpx.Client) -> tuple[httpx.Response, float]:
        together.wait()
        started = time.monotonic()
        r = c.get(path)
        return r, time.monotonic() - started

    try:
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(one, clients))
    finally:
        for c in clients:
            c.close()


def _start_run(app) -> str:
    r = httpx.post(app.url + "/v1/ingest-runs", headers=AS_INGEST, json={"registers": ["pl_mf"]})
    assert r.status_code == 202, r.text
    assert app.engine.held.wait(10), "the run never reached the engine"
    return r.headers["location"]


def _finish_run(app, location: str) -> dict:
    app.engine.go.set()
    deadline = time.monotonic() + 10
    while (run := httpx.get(app.url + location, headers=AS_INGEST).json())["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, run
        time.sleep(0.05)
    return run


def _check_burst(answers, cfg, n) -> dict:
    bound = 1 * math.ceil(n / cfg.db_pool_max) + cfg.db_pool_timeout_s
    took = [t for _, t in answers]
    codes = [r.status_code for r, _ in answers]
    assert set(codes) <= {200, 503}, codes
    for r, _ in answers:
        if r.status_code == 503:
            assert r.headers["content-type"] == "application/problem+json"
            assert r.json()["type"] == Catalog.DATABASE_UNAVAILABLE.type and r.headers["retry-after"] == "5"
    assert max(took) <= bound, (took, bound)
    return {"200": codes.count(200), "503": codes.count(503), "max_s": round(max(took), 2),
            "median_s": round(statistics.median(took), 2), "bound_s": bound}


@pg
def test_pool_exhaustion_is_a_503_never_a_500(serve, capsys):  # T8.2.a
    with serve(slow=True) as app:
        assert _get(app.url + ROWS)[0].status_code == 200  # warm: the pool, the data versions behind ETags
        probes: dict[str, tuple[httpx.Response, float]] = {}
        prober = httpx.Client(base_url=app.url, timeout=10)  # connected beforehand, as in _burst
        prober.get("/livez")

        def probe():
            deadline = time.monotonic() + 5
            while not pools.pool().get_stats().get("requests_waiting"):  # every connection busy, reads queued
                assert time.monotonic() < deadline, "the burst never filled the pool"
                time.sleep(0.01)
            for path in ("/livez", "/readyz"):
                started = time.monotonic()
                probes[path] = prober.get(path), time.monotonic() - started

        side = threading.Thread(target=probe)
        side.start()
        answers = _burst(app.url, ROWS, 10)
        side.join()
        prober.close()
    seen = _check_burst(answers, app.cfg, 10)
    live, live_s = probes["/livez"]
    ready, ready_s = probes["/readyz"]
    assert live.status_code == 200 and live_s < 0.1, live_s  # on the event loop: no thread, no pool
    assert ready.status_code in (200, 503) and ready_s < 2.5, (ready.status_code, ready_s)
    with capsys.disabled():
        print(f"\nT8.2.a 10 x rows with pg_sleep(1), DB_POOL_MAX=2, DB_POOL_TIMEOUT_S=3: {seen}; "
              f"/livez {live_s * 1000:.0f} ms, /readyz {ready.status_code} in {ready_s:.2f} s")


@pg
def test_a_running_ingest_cannot_starve_reads(serve, capsys):  # T8.2.b
    with serve() as app:
        location = _start_run(app)
        with psycopg.connect(DSN, application_name="rw-test") as watch:
            held = {r[0]: r[1] for r in watch.execute(OURS)}
        assert held.get("registerwatch-ingest") == 2 and held.get("registerwatch-lock") == 1, held
        answers = [_get(app.url + ROWS) for _ in range(20)]
        assert [r.status_code for r, _ in answers] == [200] * 20
        assert all(r.json()["data"] for r, _ in answers)
        assert _get(app.url + "/readyz")[0].status_code == 200
        assert httpx.get(app.url + location, headers=AS_INGEST).json()["status"] == "running"
        run = _finish_run(app, location)
    assert run["status"] == "succeeded" and [r["register"] for r in run["results"]] == ["pl_mf"]
    with capsys.disabled():
        print(f"\nT8.2.b during a run holding {held}: 20 sequential reads all 200, max "
              f"{max(t for _, t in answers) * 1000:.0f} ms")


@pg
def test_connections_stay_within_the_pools(serve, capsys):  # T8.2.c
    """While a run holds both ingest connections and its lock, a burst of slow
    reads fills the read pool: never more than DB_POOL_MAX + 2 + 1 connections."""
    peak: dict[str, int] = {}
    totals: list[int] = []
    stop = threading.Event()

    def sample():
        with psycopg.connect(DSN, application_name="rw-test", autocommit=True) as watch:
            while not stop.is_set():
                now = {r[0]: r[1] for r in watch.execute(OURS)}
                totals.append(sum(now.values()))
                for name, n in now.items():
                    peak[name] = max(peak.get(name, 0), n)
                time.sleep(0.01)

    with serve(slow=True) as app:
        assert _get(app.url + ROWS)[0].status_code == 200
        location = _start_run(app)
        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            answers = _burst(app.url, ROWS, 10)
        finally:
            stop.set()
            sampler.join()
        run = _finish_run(app, location)
    limit = app.cfg.db_pool_max + pools.INGEST_POOL_MAX + 1
    assert max(totals) <= limit == 5, (max(totals), peak)
    assert peak == {"registerwatch": 2, "registerwatch-ingest": 2, "registerwatch-lock": 1}, peak
    assert max(totals) == limit  # all three were in use at once, so the bound was really tested
    seen = _check_burst(answers, app.cfg, 10)
    assert run["status"] == "succeeded"
    with capsys.disabled():
        print(f"\nT8.2.c {len(totals)} samples of pg_stat_activity: peak {max(totals)} (limit {limit}), "
              f"by name {peak}; burst {seen}")

"""Phase 5: ingest runs as records in Postgres, one at a time across processes.

Without Postgres only the checks that never reach the database run (auth, body
validation, the worker's stop between registers). The rest are [pg]: a run that
lives in memory would prove nothing about F3. "Another replica" is a separate
Python process (`_REPLICA`), so the in-process lock cannot stand in for the
advisory one.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.db.repos import ingest_runs as repo
from registerwatch.http import deps
from registerwatch.ingest import runs
from registerwatch.ingest.engine import IngestResult
from registerwatch.registers import REGISTRY
from tests.test_postgres import DSN, ROOT, db, loaded  # noqa: F401 — fixtures

# Long enough for settings() (P6.3): the replica below reads them from its environment.
INGEST_TOKEN = "ingest-token-0123456789abcdefghijklmnopqrstuvwxyz"
READ_TOKEN = "read-token-0123456789abcdefghijklmnopqrstuvwxyz"
INGEST = {"Authorization": f"Bearer {INGEST_TOKEN}"}
READ = {"Authorization": f"Bearer {READ_TOKEN}"}
pg = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")


def _done(slug: str) -> IngestResult:
    return IngestResult(slug, None, True, None, 1, 1, record_count=10)


@pytest.fixture
def cfg(monkeypatch):
    cfg = SimpleNamespace(ingest_token=INGEST_TOKEN, read_token=READ_TOKEN, stale_after_h=26.0,
                          log_level="WARNING", database_url=DSN)
    for module in (deps, api, runs):
        monkeypatch.setattr(module, "settings", lambda: cfg)
    monkeypatch.setattr(runs, "make_store", lambda: object())
    runs.STOP.clear()
    yield cfg
    runs.STOP.clear()
    assert not runs.LOCAL.locked(), "a test left the run lock held"


@pytest.fixture
def fake_db(cfg, monkeypatch):
    """No database: anything that reaches one fails the test."""
    def no_db():
        raise AssertionError("this request should not reach the database")

    monkeypatch.setattr(deps, "tx", no_db)
    monkeypatch.setattr(runs, "sweep_on_startup", lambda: None)
    monkeypatch.setattr(runs, "acquire", lambda: pytest.fail("this request should not start a run"))
    with TestClient(api.app) as c:
        yield c


class Engine:
    """A fake engine.ingest. `hold` makes each call wait until `go` is set."""

    def __init__(self, hold: bool = False):
        self.started = threading.Event()
        self.go = threading.Event()
        self.hold = hold
        self.calls: list[str] = []

    def __call__(self, register, store, **kw):
        self.calls.append(register.slug)
        self.started.set()
        if self.hold:
            assert self.go.wait(30), "the test never released the engine"
        return _done(register.slug)


@pytest.fixture
def pg_app(cfg, loaded, monkeypatch):  # noqa: F811
    """The app on a real database, with a fake engine. The run lock is real."""
    with loaded() as c:
        c.execute("TRUNCATE ingest_runs CASCADE")
    monkeypatch.setattr(deps, "tx", loaded)
    monkeypatch.setattr(runs, "tx", loaded)
    engine = Engine()
    monkeypatch.setattr(runs.engine, "ingest", engine)

    def client():
        return TestClient(api.app)

    yield SimpleNamespace(client=client, engine=engine, db=loaded)
    engine.go.set()


def _in_thread(fn):
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    t.start()
    return t, out


# The other replica: its own interpreter, its own pool and its own LOCAL, same
# database. Starting it runs the startup sweep; then it POSTs once and prints
# the status and body. Its engine is fake too.
_REPLICA = """
import json, os, sys
from fastapi.testclient import TestClient
from registerwatch import api
from registerwatch.ingest import runs
from registerwatch.ingest.engine import IngestResult
runs.make_store = lambda: object()
runs.engine.ingest = lambda r, store, **kw: IngestResult(r.slug, None, True, None, 1, 1, record_count=10)
with TestClient(api.app) as c:
    auth = {"Authorization": "Bearer " + os.environ["INGEST_TOKEN"]}
    r = c.post("/v1/ingest-runs", json=json.loads(sys.argv[1]), headers=auth)
print(json.dumps({"status": r.status_code, "body": r.json()}))
"""


def _replica(body: dict) -> dict:
    env = {**os.environ, "DATABASE_URL": DSN, "INGEST_TOKEN": INGEST_TOKEN, "READ_TOKEN": READ_TOKEN,
           "LOG_LEVEL": "WARNING"}
    out = subprocess.run([sys.executable, "-c", _REPLICA, json.dumps(body)], env=env, cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# --- P5.1 the tables ------------------------------------------------------------


@pg
def test_the_migration_applies_twice_on_todays_head(db):  # noqa: F811
    """T5.1.a: 0008 over a database already migrated (the fixture ran every file), twice."""
    sql = (ROOT / "src" / "registerwatch" / "migrations" / "20261007000008_ingest_runs.sql").read_text()
    assert sql == (ROOT / "supabase" / "migrations" / "20261007000008_ingest_runs.sql").read_text()
    with db() as c:
        for _ in range(2):
            c.execute(sql)
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ingest_runs'")}
        assert {"id", "status", "requested", "idempotency_key", "request_hash", "created_at", "started_at",
                "finished_at", "error"} <= cols
        assert c.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'ingest_run_results'"
                         ).fetchone()["relrowsecurity"]


@pg
def test_anon_cannot_read_ingest_runs():
    """T5.1.b: on a database where anon gets every new public table by default
    (as on Supabase), the migrations leave ingest_runs unreadable to it."""
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS rw_anon")
        admin.execute("CREATE DATABASE rw_anon")
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'anon'").fetchone():
            admin.execute("CREATE ROLE anon NOLOGIN")
    try:
        with psycopg.connect(DSN.rsplit("/", 1)[0] + "/rw_anon", autocommit=True) as c:
            c.execute("GRANT USAGE ON SCHEMA public TO anon")
            c.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon")
            for path in sorted((ROOT / "src" / "registerwatch" / "migrations").glob("*.sql")):
                c.execute(path.read_text())
            c.execute("CREATE TABLE control (x int)")  # not locked down: the default grant is live
            c.execute("SET ROLE anon")
            c.execute("SELECT * FROM control")
            for table in ("ingest_runs", "ingest_run_results"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"SELECT * FROM {table}")
            c.execute("RESET ROLE")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS rw_anon")


# --- P5.2 single flight ---------------------------------------------------------------


@pg
def test_a_second_replica_is_refused_while_a_run_goes(pg_app):
    """T5.2.a: the other process has its own LOCAL, so only the advisory lock can refuse it."""
    pg_app.engine.hold = True
    with pg_app.client() as a:
        t, out = _in_thread(lambda: a.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"]}, headers=INGEST))
        assert pg_app.engine.started.wait(30)
        with pg_app.db() as c:
            first = c.execute("SELECT id FROM ingest_runs WHERE status = 'running'").fetchone()["id"]
        b = _replica({"registers": ["ch_esbk"]})
        pg_app.engine.go.set()
        t.join(30)
    assert b["status"] == 409, b
    assert b["body"]["type"].endswith("#ingest-in-progress")
    assert b["body"]["active_run"] == f"/v1/ingest-runs/{first}"
    assert out["r"].status_code == 202 and out["r"].json()["id"] == str(first)
    with pg_app.db() as c:
        assert [r["status"] for r in c.execute("SELECT status FROM ingest_runs")] == ["succeeded"]


@pg
def test_a_dead_holder_frees_the_lock_and_its_run_is_failed(pg_app):
    """T5.2.b: kill the backend holding the advisory lock; a new replica's startup
    sweep fails the orphan and its POST is accepted."""
    pg_app.engine.hold = True
    with pg_app.client() as a:
        t, out = _in_thread(lambda: a.post("/v1/ingest-runs", json={"registers": ["gb_ukgc", "ch_esbk"]},
                                           headers=INGEST))
        assert pg_app.engine.started.wait(30)
        with pg_app.db() as c:
            orphan = c.execute("SELECT id FROM ingest_runs").fetchone()["id"]
            holders = c.execute("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted").fetchall()
            assert len(holders) == 1
            assert c.execute("SELECT pg_terminate_backend(%s) AS ok", (holders[0]["pid"],)).fetchone()["ok"]
        b = _replica({"registers": ["ch_esbk"]})
        pg_app.engine.go.set()  # the dead worker wakes, sees its lease gone and stops
        t.join(30)
    assert b["status"] == 202, b
    assert out["r"].status_code == 202
    with pg_app.db() as c:
        old = repo.get(c, orphan)
        new = repo.get(c, uuid.UUID(b["body"]["id"]))
        assert c.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'").fetchone()["n"] == 0
    assert (old["status"], old["error"]) == ("failed", "worker lost") and old["finished_at"] is not None
    assert [r["slug"] for r in old["results"]] == ["gb_ukgc"]  # it never started ch_esbk
    assert new["status"] == "succeeded" and [r["slug"] for r in new["results"]] == ["ch_esbk"]


# --- P5.3 endpoints -----------------------------------------------------------------


@pg
def test_a_run_is_accepted_then_watched_to_its_end(pg_app):
    """T5.3.a: 202 + Location; while it runs the run reads `running`; then `succeeded` with results."""
    pg_app.engine.hold = True
    with pg_app.client() as c:
        t, out = _in_thread(lambda: c.post("/v1/ingest-runs", json={"jurisdiction": "ch"}, headers=INGEST))
        assert pg_app.engine.started.wait(30)
        with pg_app.db() as conn:
            run_id = conn.execute("SELECT id FROM ingest_runs").fetchone()["id"]
        during = c.get(f"/v1/ingest-runs/{run_id}", headers=INGEST)
        pg_app.engine.go.set()
        t.join(30)
        r = out["r"]
        after = c.get(r.headers["location"], headers=INGEST)
        listed = c.get("/v1/ingest-runs", headers=INGEST)
    assert r.status_code == 202 and r.headers["location"] == f"/v1/ingest-runs/{run_id}"
    assert r.json()["status"] == "queued" and r.json()["url"] == r.headers["location"]
    assert r.json()["registers"] == ["ch_esbk", "ch_gespa"]
    assert during.status_code == 200 and during.json()["status"] in ("queued", "running")
    assert during.json()["status"] == "running" and during.json()["finished_at"] is None
    body = after.json()
    assert after.status_code == 200 and body["status"] == "succeeded" and body["finished_at"]
    assert [(x["register"], x["complete"], x["record_count"]) for x in body["results"]] == [
        ("ch_esbk", True, 10), ("ch_gespa", True, 10)]
    assert listed.status_code == 200 and listed.json()["data"] == [body]


@pg
def test_the_same_key_replays_the_same_run(pg_app):
    """T5.3.b: same Idempotency-Key and body → one run; the second answer is 200 with its id."""
    with pg_app.client() as c:
        h = {**INGEST, "Idempotency-Key": "cron-2026-10-07"}
        first = c.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"]}, headers=h)
        again = c.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"], "force": False}, headers=h)
        other = c.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"]}, headers={**INGEST, "Idempotency-Key": "b"})
    assert first.status_code == 202 and again.status_code == 200 and other.status_code == 202
    assert again.json()["id"] == first.json()["id"] != other.json()["id"]
    assert "location" not in again.headers or again.headers["location"] == first.headers["location"]
    assert pg_app.engine.calls == ["gb_ukgc", "gb_ukgc"]  # the replay ran nothing
    with pg_app.db() as conn:
        assert conn.execute("SELECT count(*) AS n FROM ingest_runs").fetchone()["n"] == 2


@pg
def test_the_same_key_with_another_body_is_refused(pg_app):
    """T5.3.c: and a key older than 24 hours is forgotten, so it starts a new run."""
    with pg_app.client() as c:
        h = {**INGEST, "Idempotency-Key": "k1"}
        first = c.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"]}, headers=h)
        reused = c.post("/v1/ingest-runs", json={"registers": ["ch_esbk"]}, headers=h)
        with pg_app.db() as conn:
            conn.execute("UPDATE ingest_runs SET created_at = now() - interval '25 hours'")
        later = c.post("/v1/ingest-runs", json={"registers": ["ch_esbk"]}, headers=h)
    assert first.status_code == 202
    assert reused.status_code == 422 and reused.json()["type"].endswith("#idempotency-key-reused")
    assert reused.json()["original_run"] == first.json()["url"]
    assert later.status_code == 202 and later.json()["id"] != first.json()["id"]


def test_bad_targets_are_refused(fake_db):
    """T5.3.d: before any database work or lock."""
    c = fake_db
    nope = c.post("/v1/ingest-runs", json={"registers": ["xx_nope"]}, headers=INGEST)
    assert nope.status_code == 404 and nope.json()["type"].endswith("#register-not-found")
    for body in ({"registers": [], "jurisdiction": "gb"}, {"registers": ["gb_ukgc"], "jurisdiction": "gb"},
                 {"registers": []}, {"jurisdiction": "gb", "extra": 1}):
        r = c.post("/v1/ingest-runs", json=body, headers=INGEST)
        assert r.status_code == 400 and r.json()["type"].endswith("#invalid-parameter"), body
    assert c.post("/v1/ingest-runs", json={"jurisdiction": "zz"}, headers=INGEST).status_code == 404
    text = c.post("/v1/ingest-runs", content="registers=gb_ukgc", headers={**INGEST, "Content-Type": "text/plain"})
    assert text.status_code == 415


def test_a_read_token_is_forbidden_and_no_token_unauthenticated(fake_db):
    """T5.3.e: on every ingest operation."""
    c = fake_db
    for method, path in (("post", "/v1/ingest-runs"), ("get", "/v1/ingest-runs"),
                         ("get", f"/v1/ingest-runs/{uuid.uuid4()}")):
        read = getattr(c, method)(path, headers=READ)
        assert read.status_code == 403 and read.json()["type"].endswith("#forbidden"), path
        assert "www-authenticate" not in read.headers
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": INGEST_TOKEN}):
            r = getattr(c, method)(path, headers=headers)
            assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer"), (path, headers)


@pg
def test_a_finished_run_survives_a_restart(pg_app):
    """T5.3.f (F3): a new app (new client, new lifespan, new connections) still has it."""
    with pg_app.client() as c:
        run = c.post("/v1/ingest-runs", json={"registers": ["gb_ukgc"]}, headers=INGEST).json()
    with pg_app.client() as c:
        again = c.get(run["url"], headers=INGEST)
        missing = c.get(f"/v1/ingest-runs/{uuid.uuid4()}", headers=INGEST)
    assert again.status_code == 200 and again.json()["status"] == "succeeded"
    assert again.json()["id"] == run["id"] and again.json()["results"][0]["register"] == "gb_ukgc"
    assert missing.status_code == 404 and missing.json()["type"].endswith("#ingest-run-not-found")


# --- P5.4 stopping between registers -------------------------------------------------


class _MemoryRepo:
    def __init__(self):
        self.status, self.results, self.not_started = "queued", [], None

    def start(self, conn, run_id):
        self.status = "running"

    def record(self, conn, run_id, position, result):
        self.results.append(result.slug)

    def finish(self, conn, run_id, status, *, not_started=None, error=None):
        self.status, self.not_started = status, not_started
        return True


class _Lease:
    released = False

    def alive(self):
        return True

    def release(self):
        self.released = True


def test_a_stop_between_registers_leaves_the_run_partial(cfg, monkeypatch):
    """T5.4.a: five registers, STOP set during the second → two results, three not started."""
    memory = _MemoryRepo()
    for name in ("start", "record", "finish"):
        monkeypatch.setattr(runs.repo, name, getattr(memory, name))
    monkeypatch.setattr(runs, "tx", lambda: contextlib.nullcontext(None))
    targets = [REGISTRY[s] for s in ("gb_ukgc", "ch_esbk", "ch_gespa", "de_ggl", "fr_anj")]
    calls = []

    def ingest(register, store, **kw):
        calls.append(register.slug)
        if len(calls) == 2:
            runs.STOP.set()  # SIGTERM arrives while the second register is in hand
        return _done(register.slug)

    lease = _Lease()
    status = runs.execute(lease, uuid.uuid4(), targets, ingest=ingest)
    assert status == "partial" == memory.status and lease.released
    assert calls == memory.results == ["gb_ukgc", "ch_esbk"]
    assert memory.not_started == ["ch_gespa", "de_ggl", "fr_anj"]

    runs.STOP.clear()
    memory.__init__()
    assert runs.execute(_Lease(), uuid.uuid4(), targets, ingest=lambda r, s, **kw: _done(r.slug)) == "succeeded"
    assert memory.not_started == [] and len(memory.results) == 5


def test_a_second_sigterm_inside_the_first_does_not_deadlock(monkeypatch):
    """A signal handler runs on the main thread, between any two bytecodes, so a
    second SIGTERM can run its handler inside the first's `STOP.set()`, which
    holds the Event's lock. Setting STOP again there waits for that lock
    forever: the server never exits (seen in the Phase 8 wire check, under
    `uv run`). Here the second SIGTERM arrives exactly then."""

    class Stop(threading.Event):
        """Takes its lock as `Event.set` does, but fails instead of waiting forever."""

        def set(self):
            assert self._cond.acquire(timeout=1), "STOP.set() re-entered while it holds its lock"
            try:
                if not handed_on:
                    signal.raise_signal(signal.SIGTERM)  # the second one, right now
                self._flag = True
                self._cond.notify_all()
            finally:
                self._cond.release()

    handed_on: list[int] = []
    monkeypatch.setattr(runs, "STOP", Stop())
    before = signal.signal(signal.SIGTERM, lambda signum, frame: handed_on.append(signum))  # uvicorn's place
    try:
        runs.stop_on_sigterm()
        signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, before)
    assert runs.STOP.is_set() and handed_on == [signal.SIGTERM, signal.SIGTERM]

"""Ingest runs: a batch of registers, recorded in Postgres (F3), one at a time
across every process (single flight), stoppable between registers.

Single flight is two locks, both taken without waiting:
  LOCAL     a threading.Lock, shared with the legacy routes in this process;
  advisory  `pg_try_advisory_lock(LOCK_KEY)` on a connection of its own, held
            for the whole run. It is session-level, so a replica that dies (or
            whose connection is killed) frees it, and the next holder's sweep
            marks the run it left behind `failed` / `worker lost`.

The worker checks STOP and its lock connection between registers: on SIGTERM it
finishes the register in hand and records the rest as `not_started` (`partial`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row

from registerwatch.config import settings
# The run's bookkeeping shares ingest's pool, never the read pool (plan.md P8.2).
from registerwatch.db.engine import APP, ingest_tx as tx
from registerwatch.db.repos import ingest_runs as repo
from registerwatch.ingest import engine
from registerwatch.registers.base import Register
from registerwatch.storage.blobs import make_store

log = logging.getLogger(__name__)

LOCK_KEY = 0x5265_6757_4C6F_636B  # "RegWLock"
LOCAL = threading.Lock()
STOP = threading.Event()


def connect() -> psycopg.Connection:
    """A connection of the run's own, outside the pools, for the advisory lock."""
    return psycopg.connect(settings().database_url, autocommit=True, row_factory=dict_row, connect_timeout=5,
                           application_name=f"{APP}-lock")


class Lease:
    """Both locks, held. `release` is idempotent."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.released = False

    def alive(self) -> bool:
        try:
            self.conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        except psycopg.Error:
            pass  # a dead connection has released it already
        finally:
            self.conn.close()
            LOCAL.release()


def acquire() -> Lease | None:
    """Both locks, or None when a run is going (here or in another process)."""
    if not LOCAL.acquire(blocking=False):
        return None
    try:
        conn = connect()
    except BaseException:
        LOCAL.release()
        raise
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]
    except BaseException:
        conn.close()
        LOCAL.release()
        raise
    if not got:
        conn.close()
        LOCAL.release()
        return None
    lease = Lease(conn)
    try:
        with tx() as c:
            if n := repo.sweep(c):
                log.warning("marked %d ingest run(s) failed: their worker was lost", n)
    except BaseException:
        lease.release()
        raise
    return lease


def sweep_on_startup() -> None:
    """Fail runs a previous process left active, if no run is going now."""
    try:
        lease = acquire()
    except Exception as exc:  # noqa: BLE001 — the API still serves reads without it
        log.warning("startup sweep skipped: %s", type(exc).__name__)
        return
    if lease:
        lease.release()


def request_hash(requested: dict[str, Any]) -> bytes:
    return hashlib.sha256(json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()).digest()


def execute(lease: Lease, run_id: uuid.UUID, targets: list[Register], *, force: bool = False,
            accept_count_delta: bool = False,
            ingest: Callable[..., engine.IngestResult] | None = None) -> str:
    """Run the batch, recording each register as it finishes. Releases the lease.
    Returns the final status."""
    ingest = ingest or engine.ingest
    not_started: list[str] = []
    status, error = "failed", None
    try:
        with tx() as c:
            repo.start(c, run_id)
        store = make_store()
        results = []
        for i, register in enumerate(targets):
            if STOP.is_set() or not lease.alive():
                not_started = [r.slug for r in targets[i:]]
                log.warning("ingest run %s stopping: %d register(s) not started", run_id, len(not_started))
                break
            try:
                result = ingest(register, store, force=force, accept_count_delta=accept_count_delta)
            except Exception as exc:  # noqa: BLE001 — as ingest_many: one register never stops the rest
                log.exception("%s: ingest failed before recording a row", register.slug)
                result = engine.IngestResult(register.slug, None, False,
                                             f"UNRECORDED:{type(exc).__name__}:{exc}"[:500], 0, 0)
            results.append(result)
            with tx() as c:
                repo.record(c, run_id, i, result)
        ok = all(r.complete or r.skipped for r in results)
        status = "succeeded" if ok and not not_started else "partial"
    except Exception as exc:  # noqa: BLE001 — a background task has nobody to raise to
        log.exception("ingest run %s failed", run_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            with tx() as c:
                if not repo.finish(c, run_id, status, not_started=not_started, error=error):
                    log.warning("ingest run %s was closed by someone else (the sweep?)", run_id)
        except Exception:  # noqa: BLE001
            log.exception("ingest run %s: could not record its end", run_id)
        lease.release()
    return status


def stop_on_sigterm() -> None:
    """Set STOP on SIGTERM, then hand the signal to whoever handled it before
    (uvicorn's graceful shutdown). Only possible from the main thread.

    Only the first SIGTERM sets STOP. A second can arrive while the first's
    handler is inside `STOP.set()`, holding the Event's lock (a process-group
    kill under `uv run` delivers the group's copy and uv's forwarded one
    together). Its handler runs on that same thread, so setting STOP again would
    wait for the lock forever, and the process would never exit."""
    previous = signal.getsignal(signal.SIGTERM)
    stopping = False

    def handler(signum: int, frame: Any) -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            STOP.set()
        if callable(previous):
            previous(signum, frame)
        elif previous in (signal.SIG_DFL, None):  # nobody else: terminate as the default would
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread (a test client's portal)
        pass

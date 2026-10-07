"""Connections. Two pools, so ingest can never starve reads (plan.md P8.2):

  pool         reads: every API request, the CLI's queries. DB_POOL_MAX
               connections; waiting DB_POOL_TIMEOUT_S for one is a PoolTimeout
               (a 503 `database-unavailable` on v1).
  ingest_pool  ingest runs, the fetch limiter and the run bookkeeping, at most 2.

Plus the run lock's own connection (ingest/runs.py), so one process holds at
most DB_POOL_MAX + 2 + 1. Each says whose it is in `application_name`
(`registerwatch`, `registerwatch-ingest`, `registerwatch-lock`), which is how
pg_stat_activity tells them apart.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from registerwatch.config import settings

APP = "registerwatch"
INGEST_POOL_MAX = 2
# A batch writes a few times per register; waiting on its own two connections
# is never a client's wait, so it may be patient.
INGEST_POOL_TIMEOUT_S = 30.0

_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


def pool() -> ConnectionPool:
    """The read pool."""
    s = settings()
    return _pool(APP, min_size=1, max_size=s.db_pool_max, timeout=s.db_pool_timeout_s)


def ingest_pool() -> ConnectionPool:
    """Ingest's pool. Opens no connection until a run needs one, and lets them go when idle."""
    return _pool(f"{APP}-ingest", min_size=0, max_size=INGEST_POOL_MAX, timeout=INGEST_POOL_TIMEOUT_S)


def _pool(name: str, *, min_size: int, max_size: int, timeout: float) -> ConnectionPool:
    """One pool per name for the life of the process, opened on first use. Under
    a lock: requests arriving together at startup must not each open one."""
    with _POOLS_LOCK:
        if name not in _POOLS:
            _POOLS[name] = ConnectionPool(
                settings().database_url,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                name=name,
                kwargs={"row_factory": dict_row, "autocommit": False, "application_name": name},
                open=True,
            )
        return _POOLS[name]


def close_pools() -> None:
    """Close the pools opened so far and forget them; the next use opens new ones."""
    with _POOLS_LOCK:
        opened = list(_POOLS.values())
        _POOLS.clear()
    for p in opened:
        p.close()


def thread_limit() -> int:
    """Worker threads for sync request handlers. Nearly every one waits on the
    read pool: twice its size keeps every connection busy with a request ready
    behind it, and a burst beyond that queues for a thread, where no pool
    timeout runs, instead of timing out on the pool."""
    return settings().db_pool_max * 2


@contextmanager
def _tx(p: ConnectionPool) -> Iterator[Connection]:
    with p.connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def tx() -> Iterator[Connection]:
    """One transaction. Commits on clean exit, rolls back on exception.

    Keep these short. In particular: never hold one open across a network fetch
    or a sleep - the limiter reserves its slot and commits *before* sleeping.
    """
    with _tx(pool()) as conn:
        yield conn


@contextmanager
def ingest_tx() -> Iterator[Connection]:
    """`tx` on the ingest pool. Everything an ingest run does goes through this."""
    with _tx(ingest_pool()) as conn:
        yield conn


@contextmanager
def read_tx() -> Iterator[Connection]:
    """`tx` for an API request: every statement in it is cancelled after
    READ_STATEMENT_TIMEOUT_MS (QueryCanceled, a 504), so a slow query frees its
    connection instead of holding it. SET LOCAL ends with the transaction, which
    is also all a transaction-mode pooler (Supabase's) lets a client set."""
    with tx() as conn:
        ms = settings().read_statement_timeout_ms
        if ms:
            conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(ms)))
        yield conn

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from registerwatch.config import settings


@lru_cache
def pool() -> ConnectionPool:
    return ConnectionPool(
        settings().database_url,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=True,
    )


@contextmanager
def tx() -> Iterator[Connection]:
    """One transaction. Commits on clean exit, rolls back on exception.

    Keep these short. In particular: never hold one open across a network fetch
    or a sleep - the limiter reserves its slot and commits *before* sleeping.
    """
    with pool().connection() as conn:
        with conn.transaction():
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

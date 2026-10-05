from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from psycopg import Connection
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

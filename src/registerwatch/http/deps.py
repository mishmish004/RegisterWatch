"""What every v1 route shares: a database transaction, the read check, and
turning identifiers from the path into registers and tables (or a 404).

The legacy routes in `registerwatch.api` keep their own copies of the token
checks until they are removed, so tests that patch that module keep meaning
what they meant. Both read the same settings.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import AbstractContextManager
from typing import Any

from fastapi import Header, HTTPException, Query
from psycopg import Connection

from registerwatch import jurisdictions, registers
from registerwatch.config import settings
from registerwatch.db.engine import tx
from registerwatch.db.repos import snapshots as repo
from registerwatch.registers.base import Register, Table

log = logging.getLogger(__name__)

JURISDICTION: Any = Query(None, description="Only these jurisdictions; repeat the parameter for several "
                                            "(`?jurisdiction=gb&jurisdiction=de`)")


def connection() -> AbstractContextManager[Connection]:
    """One transaction. `tx` is looked up per call, so tests can patch it here."""
    return tx()


def require_read(authorization: str | None = Header(default=None)) -> None:
    token = settings().read_token
    if not token:
        return
    ok = [f"Bearer {t}" for t in (token, settings().ingest_token) if t]
    if not authorization or not any(secrets.compare_digest(authorization, o) for o in ok):
        raise HTTPException(401, "bad or missing bearer token")


def jurisdiction_or_404(code: str) -> list[Register]:
    try:
        return jurisdictions.resolve(code)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None


def registers_for(codes: list[str] | None) -> list[Register]:
    """Every register, or those of the given jurisdictions, each once."""
    if not codes:
        return registers.all_registers()
    seen: dict[str, Register] = {}
    for code in codes:
        for r in jurisdiction_or_404(code):
            seen.setdefault(r.slug, r)
    return list(seen.values())


def register_or_404(slug: str) -> Register:
    try:
        return registers.get(slug)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None


def table_or_404(register: Register, name: str) -> Table:
    for t in register.tables:
        if t.name == name:
            return t
    raise HTTPException(404, f"unknown table {name!r} in {register.slug}; "
                             f"tables: {', '.join(t.name for t in register.tables)}")


def table_of(slug: str, name: str) -> Table:
    """A table known to exist (it came back from a query over declared tables)."""
    return next(t for t in registers.get(slug).tables if t.name == name)


def freshness() -> dict[str, dict[str, Any]] | None:
    """Per-register health rows, or None when the database cannot be reached.
    Descriptions of registers are still useful without it."""
    try:
        with connection() as conn:
            return {r["slug"]: r for r in repo.source_health(conn)}
    except Exception:  # noqa: BLE001 — degrade to "freshness unknown", never fail the description
        log.warning("freshness unavailable", exc_info=True)
        return None

"""Probes for whatever runs the container, unversioned like probes everywhere
(plan.md 2.2, P8.1). Part of the v1 surface all the same: their errors are problems.

  /livez   the process answers. Touches nothing, and runs on the event loop, so
           it answers even while every request thread waits on the database.
  /readyz  the database answers `SELECT 1` within READY_WITHIN_S, else a 503
           `database-unavailable`. The check runs on threads of its own, so busy
           request threads cannot delay it, and one that has not answered in
           time is left to finish on its own: the probe still answers on time.

Both are open, never rate limited (http/ratelimit.py) and never stored
(http/caching.py). `/v1/status` says how fresh the data is; neither probe does,
since stale data is no reason to restart or unroute a healthy process (F13).
"""

from __future__ import annotations

import logging

import anyio
import psycopg
from fastapi import APIRouter

from registerwatch.http import deps
from registerwatch.http.models import Probe
from registerwatch.http.problems import Catalog, ProblemError, responses

log = logging.getLogger(__name__)

READY_WITHIN_S = 2.0
# Readiness checks in flight at once. An abandoned one gives its place back.
_CHECKS = anyio.CapacityLimiter(2)

router = APIRouter(tags=["operations"])
_OPEN = {"security": []}


@router.get("/livez", operation_id="livez", summary="The process is up",
            description="Touches nothing. For a liveness probe: failing it means restart the process.",
            responses=responses(400, 500), openapi_extra=_OPEN)
async def livez() -> Probe:
    return Probe(status="ok")


@router.get("/readyz", operation_id="readyz", summary="The database answers",
            description=f"`SELECT 1` through the read pool within {READY_WITHIN_S:g} s. For a readiness probe: "
                        "failing it means send no traffic here for now.",
            responses=responses(400, 500, 503), openapi_extra=_OPEN)
async def readyz() -> Probe:
    try:
        with anyio.fail_after(READY_WITHIN_S):
            await anyio.to_thread.run_sync(_select_1, abandon_on_cancel=True, limiter=_CHECKS)
    except (TimeoutError, psycopg.Error) as exc:
        log.warning("not ready: %s", type(exc).__name__)
        raise ProblemError(Catalog.DATABASE_UNAVAILABLE,
                           f"the database did not answer within {READY_WITHIN_S:g} s; retry shortly") from None
    return Probe(status="ok")


def _select_1() -> None:
    with deps.connection() as conn:
        conn.execute("SELECT 1")

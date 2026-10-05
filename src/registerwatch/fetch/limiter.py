"""Host-keyed rate limiting, backed by Postgres. No Redis.

The pattern is *reservation*, not sleep-under-lock:

    BEGIN
      advisory-lock the host
      slot = greatest(now(), next_slot_at)
      next_slot_at = slot + interval
    COMMIT                      <- lock released here
    sleep until slot            <- outside the transaction

Holding a lock across a sleep would serialise every worker behind one sleeper.
Reserving means N workers can each take a distinct future slot instantly and
then wait in parallel, and the host still sees one request per interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from registerwatch.db.engine import tx


@dataclass(frozen=True)
class RateBudget:
    """Declared per source, applied per host.

    Defaults are deliberately slow. You have a 24h window and ~50 pages; there is
    no reason to be fast, and slow is free defence.
    """

    min_interval_ms: int = 2000
    max_interval_ms: int = 60_000
    respect_crawl_delay: bool = True


@dataclass(frozen=True)
class Reservation:
    host: str
    slot_at: datetime
    waited_ms: int


def _ensure_host(conn, host: str, budget: RateBudget, crawl_delay_ms: int) -> None:
    floor_ms = max(budget.min_interval_ms, crawl_delay_ms)
    conn.execute(
        """
        INSERT INTO host_state (host, next_slot_at, interval_ms, floor_interval_ms)
        VALUES (%s, now(), %s, %s)
        ON CONFLICT (host) DO UPDATE
          SET floor_interval_ms = GREATEST(host_state.floor_interval_ms, EXCLUDED.floor_interval_ms),
              interval_ms       = GREATEST(host_state.interval_ms, EXCLUDED.floor_interval_ms)
        """,
        (host, floor_ms, floor_ms),
    )


def reserve(host: str, budget: RateBudget, crawl_delay_ms: int = 0) -> Reservation:
    """Claim the next slot for `host`, then block until it arrives."""
    with tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (host,))
        _ensure_host(conn, host, budget, crawl_delay_ms)
        row = conn.execute(
            """
            UPDATE host_state
               SET next_slot_at = GREATEST(now(), next_slot_at)
                                  + (interval_ms || ' milliseconds')::interval,
                   updated_at   = now()
             WHERE host = %s
            RETURNING GREATEST(now(), next_slot_at
                     - (interval_ms || ' milliseconds')::interval) AS slot_at
            """,
            (host,),
        ).fetchone()

    slot_at: datetime = row["slot_at"]
    delay = (slot_at - datetime.now(timezone.utc)).total_seconds()
    waited_ms = 0
    if delay > 0:
        time.sleep(delay)
        waited_ms = int(delay * 1000)
    return Reservation(host=host, slot_at=slot_at, waited_ms=waited_ms)


def penalise(host: str, budget: RateBudget, retry_after_s: int | None) -> None:
    """A 429 is a defect, not a retry. Back off hard AND make noise upstream."""
    with tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (host,))
        conn.execute(
            """
            UPDATE host_state
               SET interval_ms     = LEAST(%s, GREATEST(interval_ms * 2, %s)),
                   next_slot_at    = GREATEST(next_slot_at,
                                       now() + (%s || ' seconds')::interval),
                   consecutive_429 = consecutive_429 + 1,
                   last_429_at     = now(),
                   updated_at      = now()
             WHERE host = %s
            """,
            (
                budget.max_interval_ms,
                (retry_after_s or 0) * 1000,
                retry_after_s or 0,
                host,
            ),
        )


def relax(host: str) -> None:
    """Decay back toward the floor after a clean run. Slowly — 10% per run."""
    with tx() as conn:
        conn.execute(
            """
            UPDATE host_state
               SET interval_ms = GREATEST(floor_interval_ms, (interval_ms * 9) / 10),
                   consecutive_429 = 0,
                   updated_at = now()
             WHERE host = %s AND consecutive_429 = 0
            """,
            (host,),
        )

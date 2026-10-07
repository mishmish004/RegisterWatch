"""ingest_runs and ingest_run_results: the SQL, nothing else."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from psycopg import Connection

ACTIVE = ("queued", "running")


def create(conn: Connection, run_id: uuid.UUID, requested: dict[str, Any], registers: list[str],
           key: str | None, request_hash: bytes | None) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO ingest_runs (id, requested, registers, idempotency_key, request_hash) VALUES (%s, %s, %s, %s, %s)",
        (run_id, json.dumps(requested), registers, key, request_hash))
    return get(conn, run_id)  # type: ignore[return-value]


def get(conn: Connection, run_id: uuid.UUID) -> dict[str, Any] | None:
    run = conn.execute("SELECT * FROM ingest_runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        return None
    run["results"] = conn.execute("SELECT * FROM ingest_run_results WHERE run_id = %s ORDER BY position",
                                  (run_id,)).fetchall()
    return run


def by_key(conn: Connection, key: str) -> dict[str, Any] | None:
    """The run that used this key in the last 24 hours. Older uses are forgotten."""
    conn.execute("UPDATE ingest_runs SET idempotency_key = NULL, request_hash = NULL "
                 "WHERE idempotency_key = %s AND created_at < now() - interval '24 hours'", (key,))
    row = conn.execute("SELECT id FROM ingest_runs WHERE idempotency_key = %s", (key,)).fetchone()
    return get(conn, row["id"]) if row else None


def active(conn: Connection) -> dict[str, Any] | None:
    return conn.execute("SELECT id FROM ingest_runs WHERE status IN ('queued', 'running') "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()


def page(conn: Connection, *, before: tuple[datetime, uuid.UUID] | None, limit: int) -> list[dict[str, Any]]:
    """Newest first: up to limit + 1, strictly after `before` in (created_at, id) descending order."""
    if before is None:
        rows = conn.execute("SELECT id FROM ingest_runs ORDER BY created_at DESC, id DESC LIMIT %s",
                            (limit + 1,)).fetchall()
    else:
        rows = conn.execute("SELECT id FROM ingest_runs WHERE (created_at, id) < (%s, %s) "
                            "ORDER BY created_at DESC, id DESC LIMIT %s", (*before, limit + 1)).fetchall()
    return [get(conn, r["id"]) for r in rows]  # type: ignore[misc]


def start(conn: Connection, run_id: uuid.UUID) -> None:
    conn.execute("UPDATE ingest_runs SET status = 'running', started_at = now() WHERE id = %s AND status = 'queued'",
                 (run_id,))


def record(conn: Connection, run_id: uuid.UUID, position: int, result: Any) -> None:
    conn.execute(
        "INSERT INTO ingest_run_results (run_id, slug, position, snapshot_id, complete, skipped, unchanged, reason, "
        "record_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, result.slug, position, result.snapshot_id, result.complete, result.skipped, result.unchanged,
         result.reason, result.record_count))


def finish(conn: Connection, run_id: uuid.UUID, status: str, *, not_started: list[str] | None = None,
           error: str | None = None) -> bool:
    """Close a run the worker still owns; False when something else (the sweep) already did."""
    return conn.execute(
        "UPDATE ingest_runs SET status = %s, not_started = %s, error = %s, finished_at = now() "
        "WHERE id = %s AND status IN ('queued', 'running')",
        (status, not_started or [], error, run_id)).rowcount == 1


def sweep(conn: Connection) -> int:
    """Fail every run still marked active. Only the holder of the run lock calls
    this, so whatever it finds lost its worker."""
    return conn.execute("UPDATE ingest_runs SET status = 'failed', error = 'worker lost', finished_at = now() "
                        "WHERE status IN ('queued', 'running')").rowcount

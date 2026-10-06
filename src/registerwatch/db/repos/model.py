"""Read the register schemas into the model, and write the model back.

  rebuild   one transaction: read every register's rows (current and removed,
            with their history columns), build (model/build.py), replace the
            model schema's tables, record a model.builds row
  latest    the newest build, for /status and the API's freshness notes

The read runs under REPEATABLE READ so a build sees every register as of one
moment: an ingest committing halfway through the read cannot leave the model
with half of one register's change.

Writes go through TRUNCATE + COPY. The model is derived: replacing it whole is
simpler and safer than diffing it, and at ~100k rows it takes seconds. Readers
wait out those seconds (TRUNCATE locks the tables until the build commits) and
never see a half-written model.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from registerwatch.model import MODEL_VERSION
from registerwatch.model.build import Model, RegisterInput, build
from registerwatch.model.facts import RowRef
from registerwatch.registers.base import Register

log = logging.getLogger(__name__)

# COPY column order per model table: exactly the keys build.py produces.
COLUMNS: dict[str, tuple[str, ...]] = {
    "parties": ("party_id", "register", "jurisdiction", "regulator", "name", "name_key", "name_core", "cluster_id",
                "identifiers", "aliases"),
    "licences": ("licence_id", "register", "jurisdiction", "party_id", "regulator", "authority", "reference", "type",
                 "products", "channel", "area", "site", "status", "status_raw", "status_since", "valid_from",
                 "valid_to", "notes"),
    "brands": ("brand_id", "register", "jurisdiction", "party_id", "name", "name_key", "status", "status_raw",
               "products"),
    "domains": ("listing_id", "register", "jurisdiction", "host", "registrable", "label", "published", "party_id",
                "brand_id", "licence_id", "status", "status_raw", "products", "since"),
    "blocks": ("block_id", "register", "jurisdiction", "regulator", "host", "registrable", "label", "published",
               "listed_on"),
    "events": ("id", "at", "snapshot_id", "register", "jurisdiction", "kind", "type", "subject_id", "party_id", "host",
               "before", "after", "summary"),
}
LIFETIME = ("current", "first_seen_at", "last_seen_at", "removed_at", "versions", "evidence")
JSON_COLUMNS = frozenset({"identifiers", "evidence", "before", "after"})

def _default(o: Any) -> str:
    return o.isoformat() if hasattr(o, "isoformat") else str(o)


_dumps = partial(json.dumps, default=_default, ensure_ascii=False)


def columns(table: str) -> tuple[str, ...]:
    return COLUMNS[table] + (() if table == "events" else LIFETIME)


def read_register(conn: Connection, register: Register) -> RegisterInput:
    rows: dict[str, list[tuple[RowRef, dict[str, Any]]]] = {}
    for t in register.tables:
        q = sql.SQL(
            "SELECT id AS _row_id, first_seen_snapshot_id AS _first, removed_snapshot_id AS _removed, "
            "first_seen_at AS _first_at, last_seen_at AS _last_at, removed_at AS _removed_at, {cols} "
            "FROM {t} ORDER BY id"
        ).format(cols=sql.SQL(", ").join(sql.Identifier(c) for c in t.column_names),
                 t=sql.Identifier(register.slug, t.name))
        rows[t.name] = [
            (RowRef(t.name, r["_row_id"], r["_first"], r["_removed"], r["_first_at"], r["_last_at"], r["_removed_at"]),
             {c: r[c] for c in t.column_names})
            for r in conn.execute(q)
        ]
    times = {r["id"]: r["fetched_at"] for r in conn.execute(
        "SELECT r.id, r.fetched_at FROM raw_snapshots r JOIN sources s ON s.id = r.source_id "
        "WHERE s.slug = %s AND r.complete", (register.slug,))}
    return RegisterInput(register, rows, times)


def rebuild(conn: Connection, registers: list[Register]) -> dict[str, Any]:
    """Build the model from `registers` (normally all of them) and replace it.
    Must be the first statement of its transaction (REPEATABLE READ)."""
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    inputs = [read_register(conn, r) for r in registers]
    read_s = time.monotonic() - t0
    model = build(inputs)
    build_s = time.monotonic() - t0 - read_s
    write(conn, model)
    meta = {i.register.slug: {"rows": sum(len(v) for v in i.rows.values()),
                              "last_snapshot": max(i.snapshot_times, default=None)} for i in inputs}
    counts = {**model.counts, "_events": len(model.events)}
    build_id = conn.execute(
        "INSERT INTO model.builds (started_at, model_version, inputs, counts) VALUES (%s, %s, %s, %s) RETURNING id",
        (started, MODEL_VERSION, Jsonb(meta), Jsonb(counts))).fetchone()["id"]
    took = time.monotonic() - t0
    log.info("model build %s: %s rows, %s events in %.1fs (read %.1fs, build %.1fs)", build_id,
             sum(len(v) for k, v in model.tables().items() if k != "events"), len(model.events), took, read_s, build_s)
    return {"build_id": build_id, "started_at": started, "seconds": round(took, 1),
            "rows": {k: len(v) for k, v in model.tables().items()}}


def write(conn: Connection, model: Model) -> None:
    tables = model.tables()
    conn.execute(sql.SQL("TRUNCATE {}").format(
        sql.SQL(", ").join(sql.Identifier("model", t) for t in tables)))
    for name, rows in tables.items():
        cols = columns(name)
        copy = sql.SQL("COPY {t} ({cols}) FROM STDIN").format(
            t=sql.Identifier("model", name), cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols))
        with conn.cursor().copy(copy) as cp:
            for row in rows:
                cp.write_row([_cell(c, row.get(c)) for c in cols])


def _cell(col: str, value: Any) -> Any:
    if col in JSON_COLUMNS:
        return None if value is None else Jsonb(value, dumps=_dumps)
    return value


def latest(conn: Connection) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, started_at, finished_at, model_version, counts FROM model.builds ORDER BY id DESC LIMIT 1"
    ).fetchone()


def freshness(conn: Connection) -> dict[str, Any]:
    """The newest build, and whether a complete snapshot has been recorded
    since it started — the model then answers from older data than the
    registers hold."""
    b = latest(conn)
    newest = conn.execute("SELECT max(fetched_at) AS at FROM raw_snapshots WHERE complete").fetchone()["at"]
    return {
        "build_id": b["id"] if b else None,
        "built_at": b["finished_at"] if b else None,
        "newest_snapshot_at": newest,
        "behind": bool(newest and (not b or newest > b["started_at"])),
    }

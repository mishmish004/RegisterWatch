"""Read the registers back. Shared by the API and the CLI.

  rows          current rows of one table, filtered and paged
  search        a substring across every text column of every register
  check_domain  is this domain licensed, or blocked, anywhere?
  changes       rows that appeared or disappeared since a date

Every identifier in the SQL comes from a register declaration (already held to
`^[a-z][a-z0-9_]*$`) and goes through psycopg.sql.Identifier; every value is a
bound parameter. Filters on a column the table does not declare are refused,
not passed through.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection, sql

from registerwatch import jurisdictions
from registerwatch.registers.base import Register, Table
from registerwatch.registers.extract import host

MAX_LIMIT = 1000
DOMAIN_COLUMNS = ("host", "domain")      # scalar hostname columns
DOMAIN_ARRAY_COLUMNS = ("hosts",)        # text[] of hostnames


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def find_table(registers: list[Register], name: str) -> tuple[Register, Table]:
    """`table` when it is unique across `registers`, else `slug.table`."""
    slug, _, tname = name.rpartition(".")
    hits = [(r, t) for r in registers for t in r.tables
            if t.name == tname and (not slug or r.slug == slug)]
    if not hits:
        known = ", ".join(f"{r.slug}.{t.name}" for r in registers for t in r.tables)
        raise KeyError(f"unknown table {name!r}; known: {known}")
    if len(hits) > 1:
        raise KeyError(f"{name!r} is in several registers; use one of: "
                       + ", ".join(f"{r.slug}.{t.name}" for r, t in hits))
    return hits[0]


def _text_match(table: Table, q: str) -> tuple[sql.Composable, list[str]]:
    parts: list[sql.Composable] = []
    for c in table.columns:
        if c.type == "text":
            parts.append(sql.SQL("{} ILIKE %s").format(sql.Identifier(c.name)))
        elif c.type == "text[]":
            parts.append(sql.SQL("array_to_string({}, ' ') ILIKE %s").format(sql.Identifier(c.name)))
    if not parts:
        return sql.SQL("false"), []
    return sql.SQL("(") + sql.SQL(" OR ").join(parts) + sql.SQL(")"), [_like(q)] * len(parts)


def rows(conn: Connection, register: Register, table: Table, *, q: str | None = None,
         filters: dict[str, str] | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    where: list[sql.Composable] = []
    params: list[Any] = []
    for col, val in (filters or {}).items():
        if col not in table.column_names:
            raise ValueError(f"unknown column {col!r} for {register.slug}.{table.name}; "
                             f"columns: {', '.join(table.column_names)}")
        where.append(sql.SQL("{}::text = %s").format(sql.Identifier(col)))
        params.append(val)
    if q:
        clause, ps = _text_match(table, q)
        where.append(clause)
        params += ps
    w = (sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where)) if where else sql.SQL("")
    view = sql.Identifier(register.slug, f"current_{table.name}")
    total = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(view) + w, params).fetchone()["n"]
    limit = max(0, min(limit, MAX_LIMIT))
    data = conn.execute(
        sql.SQL("SELECT * FROM {}").format(view) + w + sql.SQL(" ORDER BY 1, 2 LIMIT %s OFFSET %s"),
        [*params, limit, offset],
    ).fetchall()
    return {"jurisdiction": jurisdictions.normalise(register.country), "register": register.slug,
            "table": table.name, "total": total, "limit": limit, "offset": offset, "rows": data}


def search(conn: Connection, q: str, registers: list[Register], *, limit: int = 20) -> list[dict[str, Any]]:
    """Every table with at least one match, most matches first."""
    hits = []
    for r in registers:
        for t in r.tables:
            res = rows(conn, r, t, q=q, limit=limit)
            if res["total"]:
                hits.append({**res, "regulator": r.regulator, "kind": r.kind})
    return sorted(hits, key=lambda h: -h["total"])


def check_domain(conn: Connection, domain: str, registers: list[Register]) -> dict[str, Any]:
    """Where is this domain licensed, and where is it blocked?

    Matches ignore a leading "www." and count a subdomain as a match of its
    parent (`nj.betmgm.com` is reported for `betmgm.com`, marked `subdomain`):
    regulators list whichever form the operator registered.
    """
    target = host(domain)
    if not target:
        raise ValueError(f"{domain!r} is not a hostname")
    target = target.removeprefix("www.")
    norm = "regexp_replace(lower({c}), '^www\\.', '')"
    matches = []
    for r in registers:
        for t in r.tables:
            conds, params = [], []
            for c in t.columns:
                if c.name in DOMAIN_COLUMNS:
                    conds.append(sql.SQL(f"({norm} = %s OR lower({{c}}) LIKE %s)").format(c=sql.Identifier(c.name)))
                    params += [target, "%." + target]
                elif c.name in DOMAIN_ARRAY_COLUMNS:
                    conds.append(sql.SQL(
                        "EXISTS (SELECT 1 FROM unnest({c}) h WHERE regexp_replace(lower(h), '^www\\.', '') = %s "
                        "OR lower(h) LIKE %s)").format(c=sql.Identifier(c.name)))
                    params += [target, "%." + target]
            if not conds:
                continue
            view = sql.Identifier(r.slug, f"current_{t.name}")
            for row in conn.execute(sql.SQL("SELECT * FROM {} WHERE ").format(view) + sql.SQL(" OR ").join(conds),
                                    params).fetchall():
                found = [str(row.get(c)) for c in (*DOMAIN_COLUMNS, *DOMAIN_ARRAY_COLUMNS) if row.get(c)]
                exact = any(f.lower().removeprefix("www.") == target or f"'{target}'" in f for f in found)
                matches.append({"jurisdiction": jurisdictions.normalise(r.country), "register": r.slug,
                                "regulator": r.regulator, "kind": r.kind, "table": t.name,
                                "match": "exact" if exact else "subdomain", "row": row})
    return {
        "domain": target,
        "licensed_in": sorted({m["jurisdiction"] for m in matches if m["kind"] == "licensees"}),
        "blocked_in": sorted({m["jurisdiction"] for m in matches if m["kind"] == "blocklist"}),
        "matches": matches,
    }


def changes(conn: Connection, register: Register, since: datetime, *, limit: int = 200) -> dict[str, Any]:
    """Rows added or removed since `since`, per table.

    A register's first complete snapshot is its baseline, not a change: rows
    first seen there are not reported as added.
    """
    src = conn.execute("SELECT id FROM sources WHERE slug = %s", (register.slug,)).fetchone()
    if not src:
        return {"register": register.slug, "since": since, "tables": {}}
    first = conn.execute("SELECT min(id) AS id FROM raw_snapshots WHERE source_id = %s AND complete",
                         (src["id"],)).fetchone()["id"]
    out: dict[str, Any] = {}
    for t in register.tables:
        tbl = sql.Identifier(register.slug, t.name)
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in t.column_names)
        added = conn.execute(
            sql.SQL("SELECT {cols}, first_seen_at AS at FROM {t} WHERE first_seen_at >= %s "
                    "AND first_seen_snapshot_id <> %s ORDER BY first_seen_at DESC LIMIT %s").format(cols=cols, t=tbl),
            (since, first or -1, limit)).fetchall()
        removed = conn.execute(
            sql.SQL("SELECT {cols}, removed_at AS at FROM {t} WHERE removed_at >= %s "
                    "ORDER BY removed_at DESC LIMIT %s").format(cols=cols, t=tbl),
            (since, limit)).fetchall()
        if added or removed:
            out[t.name] = {"added": added, "removed": removed}
    return {"register": register.slug, "since": since, "tables": out}


# --- API v1: keyed pages, the change feed, snapshot history ---------------------
#
# v1 pages are keyed on each row's `id` (insertion order, never reused) and read
# as of one snapshot, so a walk that spans an ingest neither repeats nor skips a
# row: the cursor carries the snapshot the first page was read at.

HISTORY = ("id", "first_seen_snapshot_id", "last_seen_snapshot_id", "removed_snapshot_id",
           "first_seen_at", "last_seen_at", "removed_at")


def latest_snapshot_id(conn: Connection, register: Register) -> int:
    """The register's newest complete snapshot, or 0 when it has none."""
    row = conn.execute("SELECT max(r.id) AS id FROM raw_snapshots r JOIN sources s ON s.id = r.source_id "
                       "WHERE s.slug = %s AND r.complete", (register.slug,)).fetchone()
    return (row or {}).get("id") or 0


def _as_of(snapshot_id: int, latest: int) -> tuple[sql.Composable, list[Any]]:
    """Rows of the table as it was at `snapshot_id`. At the latest snapshot that
    is just the current rows, which the partial index on `id` serves."""
    if snapshot_id >= latest:
        return sql.SQL("removed_snapshot_id IS NULL"), []
    return (sql.SQL("first_seen_snapshot_id <= %s AND (removed_snapshot_id IS NULL OR removed_snapshot_id > %s)"),
            [snapshot_id, snapshot_id])


def _where(register: Register, table: Table, q: str | None, filters: dict[str, str] | None,
           snapshot_id: int, latest: int) -> tuple[sql.Composable, list[Any]]:
    clause, params = _as_of(snapshot_id, latest)
    where: list[sql.Composable] = [clause]
    for col, val in (filters or {}).items():
        if col not in table.column_names:
            raise ValueError(f"unknown column {col!r} for {register.slug}.{table.name}; "
                             f"columns: {', '.join(table.column_names)}")
        where.append(sql.SQL("{}::text = %s").format(sql.Identifier(col)))
        params.append(val)
    if q:
        match, ps = _text_match(table, q)
        where.append(match)
        params += ps
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where), params


def row_page(conn: Connection, register: Register, table: Table, *, snapshot_id: int, latest: int,
             after: int = 0, limit: int = 100, q: str | None = None,
             filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Up to `limit` + 1 rows with `id > after`, in id order (the extra one says
    whether there is another page)."""
    where, params = _where(register, table, q, filters, snapshot_id, latest)
    return conn.execute(
        sql.SQL("SELECT * FROM {}").format(sql.Identifier(register.slug, table.name)) + where
        + sql.SQL(" AND id > %s ORDER BY id LIMIT %s"), [*params, after, limit + 1]).fetchall()


def count_rows(conn: Connection, register: Register, table: Table, *, snapshot_id: int, latest: int,
               q: str | None = None, filters: dict[str, str] | None = None) -> int:
    where, params = _where(register, table, q, filters, snapshot_id, latest)
    return conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(register.slug, table.name))
                        + where, params).fetchone()["n"]


def get_row(conn: Connection, register: Register, table: Table, row_id: int) -> dict[str, Any] | None:
    """One row by id, current or not, with its history."""
    return conn.execute(sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(register.slug, table.name)),
                        (row_id,)).fetchone()


# A change feed position: (snapshot_id, register, table, id, change). Snapshot ids
# are global, so the order interleaves registers by when their snapshots landed.
FeedKey = tuple[int, str, str, int, str]


def change_feed(conn: Connection, registers: list[Register], *, since: datetime | None = None,
                until: datetime | None = None, after: FeedKey | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
    """Up to `limit` + 1 row additions and removals across `registers`, oldest
    first. A register's first complete snapshot is its baseline, not a change."""
    import heapq

    baselines = {r["slug"]: r["id"] for r in conn.execute(
        "SELECT s.slug, min(r.id) AS id FROM sources s JOIN raw_snapshots r ON r.source_id = s.id AND r.complete "
        "WHERE s.slug = ANY(%s) GROUP BY s.slug", ([r.slug for r in registers],)).fetchall()}
    streams = []
    for reg in registers:
        if reg.slug not in baselines:
            continue
        for t in reg.tables:
            streams.append(_table_changes(conn, reg, t, baselines[reg.slug], since, until, after, limit))
    out = []
    for event in heapq.merge(*streams, key=lambda e: e["key"]):
        out.append(event)
        if len(out) > limit:
            break
    return out


def _table_changes(conn: Connection, register: Register, table: Table, baseline: int,
                   since: datetime | None, until: datetime | None, after: FeedKey | None,
                   limit: int) -> list[dict[str, Any]]:
    tbl = sql.Identifier(register.slug, table.name)
    branches, params = [], []
    sides = (("added", "first_seen_snapshot_id", "first_seen_at", "first_seen_snapshot_id <> %s"),
             ("removed", "removed_snapshot_id", "removed_at", "removed_snapshot_id IS NOT NULL"))
    for change, snap, at, extra in sides:
        conds = [sql.SQL(extra)]
        ps: list[Any] = [baseline] if change == "added" else []
        if since:
            conds.append(sql.SQL("{} >= %s").format(sql.Identifier(at)))
            ps.append(since)
        if until:
            conds.append(sql.SQL("{} < %s").format(sql.Identifier(at)))
            ps.append(until)
        if after:
            # Everything at or past the cursor's snapshot; the exact cut is made below.
            conds.append(sql.SQL("{} >= %s").format(sql.Identifier(snap)))
            ps.append(after[0])
        branches.append(sql.SQL("SELECT %s::text AS change, {snap} AS snapshot_id, {at} AS at, t.* FROM {t} t WHERE ")
                        .format(snap=sql.Identifier(snap), at=sql.Identifier(at), t=tbl)
                        + sql.SQL(" AND ").join(conds))
        params += [change, *ps]
    query = sql.SQL("SELECT * FROM (") + sql.SQL(" UNION ALL ").join(branches) + sql.SQL(") e")
    if after:
        # Past the cursor in (snapshot_id, register, table, id, change) order. This
        # stream's register and table are fixed, so compare them here, in Python's
        # order (the merge's), rather than in a collation-dependent SQL comparison.
        here, there = (register.slug, table.name), (after[1], after[2])
        same = sql.SQL("TRUE") if here > there else sql.SQL("FALSE") if here < there else \
            sql.SQL("(id, change) > (%s, %s)")
        query += sql.SQL(" WHERE snapshot_id > %s OR (snapshot_id = %s AND ") + same + sql.SQL(")")
        params += [after[0], after[0], *([after[3], after[4]] if here == there else [])]
    query += sql.SQL(" ORDER BY snapshot_id, id, change LIMIT %s")
    params.append(limit + 1)
    rows = conn.execute(query, params).fetchall()
    return [{"key": (r["snapshot_id"], register.slug, table.name, r["id"], r["change"]), "register": register,
             "table": table, **r} for r in rows]


def search_tables(conn: Connection, q: str, registers: list[Register], *, limit: int = 20) -> list[dict[str, Any]]:
    """Every table with at least one current row matching `q`: its total and its
    first `limit` rows by id. One query per register, most matches first."""
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for r in registers:
        branches, params = [], []
        for t in r.tables:
            match, ps = _text_match(t, q)
            if not ps:
                continue
            values = sql.SQL(", ").join(sql.SQL("{}, t.{}").format(sql.Literal(c), sql.Identifier(c))
                                        for c in t.column_names)
            branches.append(
                sql.SQL("SELECT {name}::text AS tbl, t.id, t.first_seen_snapshot_id, t.first_seen_at, t.last_seen_at, "
                        "jsonb_build_object({values}) AS vals FROM {t} t WHERE removed_snapshot_id IS NULL AND ")
                .format(name=sql.Literal(t.name), values=values, t=sql.Identifier(r.slug, t.name)) + match)
            params += ps
        if not branches:
            continue
        query = (sql.SQL("SELECT * FROM (SELECT m.*, count(*) OVER w AS total, row_number() OVER (w ORDER BY id) AS rn "
                         "FROM (") + sql.SQL(" UNION ALL ").join(branches)
                 + sql.SQL(") m WINDOW w AS (PARTITION BY tbl)) x WHERE rn <= %s ORDER BY tbl, id"))
        for row in conn.execute(query, [*params, limit]).fetchall():
            table = next(t for t in r.tables if t.name == row["tbl"])
            hit = hits.setdefault((r.slug, table.name), {
                "jurisdiction": jurisdictions.normalise(r.country), "register": r.slug, "regulator": r.regulator,
                "kind": r.kind, "table": table.name, "total": row["total"], "rows": []})
            hit["rows"].append({**_typed(table, row["vals"]), **{k: row[k] for k in HISTORY if k in row}})
    return sorted(hits.values(), key=lambda h: (-h["total"], h["register"], h["table"]))


def _typed(table: Table, values: dict[str, Any]) -> dict[str, Any]:
    """jsonb gives dates and timestamps back as text; restore what a plain SELECT gives."""
    from datetime import date

    out = dict(values)
    for c in table.columns:
        v = out.get(c.name)
        if isinstance(v, str) and c.type == "date":
            out[c.name] = date.fromisoformat(v)
        elif isinstance(v, str) and c.type == "timestamptz":
            out[c.name] = datetime.fromisoformat(v)
    return out


def snapshot_page(conn: Connection, register: Register, *, before: int | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    """Every recorded run of the register, complete or not, newest first:
    up to `limit` + 1 with `id < before`."""
    return conn.execute(
        "SELECT r.id, r.fetched_at, r.run_started_at, r.complete, r.incomplete_reason, r.record_count, "
        "r.http_status, r.pages_expected, r.pages_ok FROM raw_snapshots r JOIN sources s ON s.id = r.source_id "
        "WHERE s.slug = %s AND r.id < %s ORDER BY r.id DESC LIMIT %s",
        (register.slug, before or 2**63 - 1, limit + 1)).fetchall()

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

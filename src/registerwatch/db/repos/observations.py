"""Write one complete snapshot's rows into its register's schema.

Called only for complete snapshots, inside the transaction that inserts the
raw_snapshots row. Three statements per table:

  close   current rows whose hash is not in this snapshot -> removed here
  touch   current rows whose hash is                      -> last seen here
  insert  hashes not current before this snapshot         -> first seen here

Incoming hashes go through a temp table with COPY, so a 60,000-row blocklist is
three set operations, not 60,000 round trips.
"""

from __future__ import annotations

from psycopg import Connection, sql

from registerwatch.registers.base import Register, Row


def apply(conn: Connection, register: Register, snapshot_id: int,
          rows_by_table: dict[str, list[tuple[bytes, Row]]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for table in register.tables:
        pairs = rows_by_table.get(table.name, [])
        target = sql.Identifier(register.slug, table.name)
        cols = table.column_names

        conn.execute("CREATE TEMP TABLE _incoming (row_hash bytea PRIMARY KEY) ON COMMIT DROP")
        with conn.cursor().copy("COPY _incoming (row_hash) FROM STDIN") as cp:
            for digest, _ in pairs:
                cp.write_row((digest,))

        closed = conn.execute(
            sql.SQL("""
                UPDATE {t} SET removed_snapshot_id = %s, removed_at = now()
                 WHERE removed_snapshot_id IS NULL
                   AND NOT EXISTS (SELECT 1 FROM _incoming i WHERE i.row_hash = {t}.row_hash)
            """).format(t=target),
            (snapshot_id,),
        ).rowcount

        touched = conn.execute(
            sql.SQL("""
                UPDATE {t} SET last_seen_snapshot_id = %s, last_seen_at = now()
                 WHERE removed_snapshot_id IS NULL
                   AND EXISTS (SELECT 1 FROM _incoming i WHERE i.row_hash = {t}.row_hash)
            """).format(t=target),
            (snapshot_id,),
        ).rowcount

        current = {
            bytes(r["row_hash"]) for r in conn.execute(
                sql.SQL("SELECT row_hash FROM {t} WHERE removed_snapshot_id IS NULL").format(t=target)
            )
        }
        new = [(d, r) for d, r in pairs if d not in current]
        if new:
            copy = sql.SQL("COPY {t} ({cols}) FROM STDIN").format(
                t=target,
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in
                                        ["row_hash", "first_seen_snapshot_id", "last_seen_snapshot_id", *cols]),
            )
            with conn.cursor().copy(copy) as cp:
                for digest, row in new:
                    cp.write_row((digest, snapshot_id, snapshot_id, *(row.get(c) for c in cols)))

        conn.execute("DROP TABLE _incoming")
        stats[table.name] = {"inserted": len(new), "removed": closed, "unchanged": touched}
    return stats

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import Connection


def get_source(conn: Connection, slug: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM sources WHERE slug = %s", (slug,))
    return cur.fetchone()


def upsert_source(
    conn: Connection,
    slug: str,
    display_name: str,
    jurisdiction: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO sources (slug, display_name, jurisdiction, config)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (slug) DO UPDATE
          SET display_name = EXCLUDED.display_name,
              jurisdiction = EXCLUDED.jurisdiction,
              config       = EXCLUDED.config
        RETURNING *
        """,
        (slug, display_name, jurisdiction, json.dumps(config)),
    )
    return cur.fetchone()  # type: ignore[return-value]


def latest_snapshot(
    conn: Connection, source_id: int, *, complete_only: bool = False
) -> dict[str, Any] | None:
    sql = """
        SELECT * FROM raw_snapshots
        WHERE source_id = %s {filt}
        ORDER BY fetched_at DESC, id DESC
        LIMIT 1
    """.format(filt="AND complete" if complete_only else "")
    return conn.execute(sql, (source_id,)).fetchone()


# A run that produced a whole snapshot. Complete runs are parsed in the same run
# now; AWAITING_PARSE is the verdict older, fetch-only runs recorded.
GOOD = "(r.complete OR r.incomplete_reason = 'AWAITING_PARSE')"


def fetched_recently(conn: Connection, source_id: int, within_h: float) -> bool:
    """Has a *good* snapshot landed within `within_h` hours?

    Failed runs do not count. They used to, so a 06:00 failure turned every
    retry for the next 20 hours into a silent, successful-looking skip.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_h)
    row = conn.execute(
        f"SELECT 1 FROM raw_snapshots r WHERE r.source_id = %s AND r.fetched_at > %s AND {GOOD} LIMIT 1",
        (source_id, cutoff),
    ).fetchone()
    return row is not None


def recent_snapshots(conn: Connection, source_id: int, limit: int = 14) -> list[dict[str, Any]]:
    """Newest first, good or not: the ingest reads their manifests for 304
    carry-forward (the newest) and row-count baselines (the newest that has one)."""
    return conn.execute(
        """
        SELECT id, blob_ref, raw_hash, canonical_hash, fetched_at, complete, incomplete_reason
          FROM raw_snapshots
         WHERE source_id = %s
         ORDER BY fetched_at DESC, id DESC
         LIMIT %s
        """,
        (source_id, limit),
    ).fetchall()


def source_health(conn: Connection) -> list[dict[str, Any]]:
    """Per enabled source: last attempt, last good snapshot, last verdict."""
    return conn.execute(
        f"""
        SELECT s.slug,
               max(r.fetched_at)                                      AS last_fetch,
               max(r.fetched_at) FILTER (WHERE {GOOD})                AS last_good,
               (array_agg(r.incomplete_reason ORDER BY r.fetched_at DESC, r.id DESC)
                  FILTER (WHERE r.id IS NOT NULL))[1]                 AS last_reason,
               count(*) FILTER (WHERE r.fetched_at > now() - interval '7 days'
                                  AND NOT {GOOD})                     AS failed_7d
          FROM sources s LEFT JOIN raw_snapshots r ON r.source_id = s.id
         WHERE s.enabled
         GROUP BY s.slug ORDER BY s.slug
        """
    ).fetchall()


def insert_snapshot(
    conn: Connection,
    *,
    source_id: int,
    run_started_at: datetime,
    raw_hash: bytes,
    blob_ref: str,
    http_status: int,
    pages_expected: int,
    pages_ok: int,
    fetch_log: list[dict[str, Any]],
    complete: bool,
    incomplete_reason: str | None,
    record_count: int | None = None,
    canonical_hash: bytes | None = None,
    parsed: bool = False,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO raw_snapshots (
            source_id, run_started_at, raw_hash, blob_ref, http_status,
            pages_expected, pages_ok, fetch_log, complete, incomplete_reason,
            record_count, canonical_hash, parsed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  CASE WHEN %s THEN now() END)
        RETURNING id
        """,
        (
            source_id,
            run_started_at,
            raw_hash,
            blob_ref,
            http_status,
            pages_expected,
            pages_ok,
            json.dumps(fetch_log),
            complete,
            incomplete_reason,
            record_count,
            canonical_hash,
            parsed,
        ),
    )
    return cur.fetchone()["id"]  # type: ignore[index]


def etag_map(conn: Connection, source_id: int) -> dict[str, dict[str, str]]:
    """url -> {etag, last_modified} from the most recent snapshot's fetch log.

    No separate cache table: the evidence log already holds this, and reusing it
    means the conditional-request state can never drift from what we actually saw.
    """
    row = latest_snapshot(conn, source_id)
    if not row:
        return {}
    out: dict[str, dict[str, str]] = {}
    for entry in row["fetch_log"] or []:
        url = entry.get("url")
        if not url or entry.get("status_code") not in (200, 304):
            continue
        validators = {
            k: v for k, v in (("etag", entry.get("etag")),
                              ("last_modified", entry.get("last_modified"))) if v
        }
        if validators:
            out[url] = validators
    return out


def previous_pages_ok(conn: Connection, source_id: int) -> int | None:
    # GOOD, not `complete`: complete is false until step 2 exists, so filtering
    # on it made this always None and paginated truncation undetectable.
    row = conn.execute(
        f"SELECT r.pages_ok FROM raw_snapshots r WHERE r.source_id = %s AND {GOOD} "
        "ORDER BY r.fetched_at DESC, r.id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["pages_ok"] if row else None

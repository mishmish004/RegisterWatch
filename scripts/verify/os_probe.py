"""Helpers for scripts/verify/os.sh (plan.md Phase 9). os.sh runs this inside
the image under test, so it needs nothing on the host but Docker.

  os_probe.py seed --rows 5000
      gb_ukgc gets a complete snapshot with N current synthetic licences (if it
      has fewer), so a 1000-row page is a real 1000 rows. Needs DATABASE_URL.

  os_probe.py burst URL --n 20
      N GETs at once, each on a connection of its own, released together.
      One JSON line per request (status, ms, content type), then a summary
      line: {"n", "statuses", "max_ms", "errors"}.

  os_probe.py get URL [--token T] [--method POST --body JSON]
      One request; prints {"status", "ms", "body"} (body parsed when JSON).

  os_probe.py sql "SELECT ..."
      The rows as one JSON list. Needs DATABASE_URL.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from collections import Counter
from urllib.parse import urlsplit

FAKE_LICENCES = {"account_number": "(200000 + g)::text", "licence_number": "'OS-' || g",
                 "status": "CASE WHEN mod(g, 7) = 0 THEN 'Revoked' ELSE 'Active' END",
                 "type": "CASE WHEN mod(g, 2) = 0 THEN 'Remote' ELSE 'Non-Remote' END",
                 "activity": "'Casino ' || md5(g::text)",
                 "start_date": "date '2020-01-01' + mod(g, 1500)", "end_date": "NULL::date"}


def seed(rows: int) -> None:
    from datetime import datetime, timezone

    from registerwatch.db.engine import tx
    from registerwatch.db.repos import snapshots as repo
    from registerwatch.registers import REGISTRY

    gb = REGISTRY["gb_ukgc"]
    with tx() as c:
        have = c.execute("SELECT count(*) AS n FROM gb_ukgc.licences WHERE removed_snapshot_id IS NULL").fetchone()["n"]
        if have >= rows:
            print(json.dumps({"seeded": 0, "current": have}))
            return
        src = repo.upsert_source(c, gb.slug, gb.name, gb.country, gb.config())
        sid = repo.insert_snapshot(
            c, source_id=src["id"], run_started_at=datetime.now(timezone.utc), raw_hash=b"os.sh",
            blob_ref="os.sh", http_status=200, pages_expected=1, pages_ok=1, fetch_log=[], complete=True,
            incomplete_reason=None, record_count=rows, canonical_hash=None, parsed=True)
        n = rows - have
        c.execute(f"INSERT INTO gb_ukgc.licences (row_hash, first_seen_snapshot_id, last_seen_snapshot_id, "
                  f"{', '.join(FAKE_LICENCES)}) SELECT sha256(('os.sh licence ' || g)::bytea), %s, %s, "
                  f"{', '.join(FAKE_LICENCES.values())} FROM generate_series(%s::int, %s::int) g",
                  (sid, sid, have + 1, have + n))
    print(json.dumps({"seeded": n, "current": rows, "snapshot_id": sid}))


def request(url: str, *, method: str = "GET", token: str | None = None, body: str | None = None,
            timeout: float = 60.0, ready: threading.Barrier | None = None) -> dict:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    headers = {"Connection": "close"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    if ready is not None:
        ready.wait()
    start = time.perf_counter()
    try:
        conn.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        out = {"status": resp.status, "ms": round((time.perf_counter() - start) * 1000, 1),
               "type": resp.getheader("content-type"), "raw": raw}
    except OSError as exc:
        out = {"status": None, "ms": round((time.perf_counter() - start) * 1000, 1),
               "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()
    return out


def burst(url: str, n: int, timeout: float) -> int:
    ready = threading.Barrier(n)
    results: list[dict] = [{}] * n

    def one(i: int) -> None:
        results[i] = request(url, timeout=timeout, ready=ready)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results:
        r.pop("raw", None)
        print(json.dumps(r))
    print(json.dumps({"n": n, "statuses": dict(Counter(str(r["status"]) for r in results)),
                      "max_ms": max(r["ms"] for r in results), "errors": sum("error" in r for r in results)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="os_probe.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--rows", type=int, default=5000)
    b = sub.add_parser("burst")
    b.add_argument("url")
    b.add_argument("--n", type=int, default=20)
    b.add_argument("--timeout", type=float, default=60.0)
    g = sub.add_parser("get")
    g.add_argument("url")
    g.add_argument("--method", default="GET")
    g.add_argument("--token")
    g.add_argument("--body")
    q = sub.add_parser("sql")
    q.add_argument("statement")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        seed(args.rows)
        return 0
    if args.cmd == "sql":
        import os

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as c:
            print(json.dumps(c.execute(args.statement).fetchall(), default=str))
        return 0
    if args.cmd == "burst":
        return burst(args.url, args.n, args.timeout)
    r = request(args.url, method=args.method, token=args.token, body=args.body)
    raw = r.pop("raw", b"")
    try:
        r["body"] = json.loads(raw) if raw else None
    except ValueError:
        r["body"] = raw.decode("utf-8", "replace")[:500]
    print(json.dumps(r))
    return 0 if r["status"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())

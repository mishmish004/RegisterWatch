"""Phase 7, P7.3 [pg]: what a read may cost. The `q` and domain queries use
their indexes, a slow statement is cancelled at READ_STATEMENT_TIMEOUT_MS (a
504), and search stays fast on the fixture-loaded database (plan.md P7.3).

Plans are read from the statements the query layer itself sends (a spy on the
connection), on a table grown with synthetic current rows in a transaction that
is rolled back: on a handful of fixture rows any plan is a sequential scan, and
rightly so.
"""

from __future__ import annotations

import contextlib
import statistics
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import sql
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from registerwatch import api, query
from registerwatch.db import engine
from registerwatch.registers import REGISTRY, all_registers
from tests.test_postgres import DSN, db, loaded  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")

GB = REGISTRY["gb_ukgc"]
SCANS = ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Index Scan")


class Spy:
    """A connection that records what it is asked to run."""

    def __init__(self, conn):
        self.conn, self.sent = conn, []

    def execute(self, q, params=None, **kw):
        self.sent.append((q, params))
        return self.conn.execute(q, params, **kw)


def scans(conn, statement, params) -> list[tuple[str, str]]:
    """(node type, index or relation) of every scan in the statement's plan."""
    text = statement.as_bytes(conn) if hasattr(statement, "as_bytes") else statement.encode()
    plan = conn.execute(b"EXPLAIN (FORMAT JSON) " + text, params).fetchone()["QUERY PLAN"][0]["Plan"]
    out: list[tuple[str, str]] = []

    def walk(node: dict[str, Any]) -> None:
        if node["Node Type"] in SCANS:
            out.append((node["Node Type"], node.get("Index Name") or node.get("Relation Name")))
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    return out


def _settle(c, schema: str, table: str) -> None:
    """What autovacuum does after a load: GIN pending lists merged (until then a
    trigram scan reads the whole list, and the planner prices it so) and
    statistics fresh."""
    for r in c.execute("SELECT x.oid::regclass::text AS i FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
                       "JOIN pg_am a ON a.oid = x.relam WHERE i.indrelid = %s::regclass AND a.amname = 'gin'",
                       (f"{schema}.{table}",)).fetchall():
        c.execute("SELECT gin_clean_pending_list(%s::regclass)", (r["i"],))
    c.execute(f"ANALYZE {schema}.{table}")


@contextlib.contextmanager
def grown(loaded, table: str, columns: dict[str, str], n: int = 5000):  # noqa: F811
    """A connection on which gb_ukgc.`table` has `n` more current rows, each
    column given as an SQL expression of g in 1..n; rolled back afterwards."""
    with loaded():
        pass  # every register's fixture rows are in
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        sid = c.execute("SELECT max(r.id) AS id FROM raw_snapshots r JOIN sources s ON s.id = r.source_id "
                        "WHERE s.slug = 'gb_ukgc' AND r.complete").fetchone()["id"]
        c.execute(f"INSERT INTO gb_ukgc.{table} (row_hash, first_seen_snapshot_id, last_seen_snapshot_id, "
                  f"{', '.join(columns)}) SELECT sha256(('{table}' || g)::bytea), %s, %s, "
                  f"{', '.join(columns.values())} FROM generate_series(1, %s) g", (sid, sid, n))
        _settle(c, "gb_ukgc", table)
        try:
            yield c
        finally:
            c.rollback()


TRADING_NAMES = {"account_number": "(100000 + g)::text", "trading_name": "'Operator ' || md5(g::text)",
                 "status": "'Active'"}
DOMAIN_NAMES = {"account_number": "(100000 + g)::text", "domain_name": "'https://site' || g || '.example'",
                "status": "'Active'", "host": "'site' || g || '.example'"}


# --- T7.3.a ------------------------------------------------------------------------------

def test_q_on_trading_names_uses_the_trigram_index(loaded):  # noqa: F811 — T7.3.a
    table = next(t for t in GB.tables if t.name == "trading_names")
    with grown(loaded, "trading_names", TRADING_NAMES) as c:
        latest = query.latest_snapshot_id(c, GB)
        rows, search = Spy(c), Spy(c)
        query.row_page(rows, GB, table, snapshot_id=latest, latest=latest, q="bet")
        query.search_tables(search, "bet", [GB])
        for name, (statement, params) in (("rows", rows.sent[0]), ("search", search.sent[0])):
            plan = scans(c, statement, params)
            assert ("Bitmap Index Scan", "trading_names_trading_name_trgm") in plan, (name, plan)
            assert ("Seq Scan", "trading_names") not in plan, (name, plan)


def test_every_searched_column_has_an_index_its_query_can_use(loaded):  # noqa: F811
    """Each column's `q` term is its trigram index's expression (text[] columns
    go through registerwatch_private.array_text on both sides), so the index can
    answer it. On a few rows the planner would rather read a whole small index
    than search a trigram one, so each column's term is planned on its own, with
    the other partial indexes dropped (rolled back), full scans priced out and
    the table settled as autovacuum leaves it."""
    with loaded():
        pass
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        for scan in ("seqscan", "indexscan", "indexonlyscan"):
            c.execute(f"SET LOCAL enable_{scan} = off")
        try:
            checked = 0
            for r in all_registers():
                for t in r.tables:
                    for row in c.execute(
                            "SELECT x.relname FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
                            "WHERE i.indrelid = %s::regclass AND i.indpred IS NOT NULL AND x.relname NOT LIKE %s",
                            (f"{r.slug}.{t.name}", r"%\_trgm")).fetchall():
                        c.execute(f'DROP INDEX {r.slug}."{row["relname"]}"')
                    _settle(c, r.slug, t.name)
                    for col in t.columns:
                        if not col.searched:
                            continue
                        match, params = query._text_match(SimpleNamespace(columns=(col,)), "bet")
                        statement = (sql.SQL("SELECT id FROM {} WHERE removed_snapshot_id IS NULL AND ")
                                     .format(sql.Identifier(r.slug, t.name)) + match)
                        assert f"{t.name}_{col.name}_trgm" in searched_by(c, statement, params), (r.slug, t.name)
                        checked += 1
            assert checked == sum(col.searched for r in all_registers() for t in r.tables for col in t.columns)
        finally:
            c.rollback()


def searched_by(conn, statement, params) -> set[str]:
    """The indexes a plan searches with a condition (not merely reads whole)."""
    plan = conn.execute(b"EXPLAIN (FORMAT JSON) " + statement.as_bytes(conn), params).fetchone()["QUERY PLAN"]
    found: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        if node["Node Type"] == "Bitmap Index Scan" and node.get("Index Cond"):
            found.add(node["Index Name"])
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return found


# --- T7.3.b ------------------------------------------------------------------------------

def test_the_domain_query_uses_the_reverse_index(loaded):  # noqa: F811 — T7.3.b
    with grown(loaded, "domain_names", DOMAIN_NAMES) as c:
        spy = Spy(c)
        found = query.check_domain(spy, "site42.example", [GB])
        statement, params = next(s for s in spy.sent if "domain_names" in s[0].as_string(c))
        plan = scans(c, statement, params)
        assert ("Bitmap Index Scan", "domain_names_host_reverse") in plan, plan
        assert ("Bitmap Index Scan", "domain_names_host_lower") in plan, plan
        assert not any(kind == "Seq Scan" for kind, _ in plan), plan
        assert [m["row"]["host"] for m in found["matches"]] == ["site42.example"]
        # A subdomain is found through the same index; a lookalike suffix is not.
        c.execute("INSERT INTO gb_ukgc.domain_names (row_hash, first_seen_snapshot_id, last_seen_snapshot_id, "
                  "account_number, domain_name, status, host) SELECT sha256(h::bytea), s, s, '1', h, 'Active', h "
                  "FROM unnest(ARRAY['nj.site42.example', 'notsite42.example', 'site42_example']) h, "
                  "(SELECT max(first_seen_snapshot_id) AS s FROM gb_ukgc.domain_names) x")
        hosts = {m["row"]["host"]: m["match"] for m in query.check_domain(c, "www.site42.example", [GB])["matches"]}
        assert hosts == {"site42.example": "exact", "nj.site42.example": "subdomain"}


# --- T7.3.c ------------------------------------------------------------------------------

@pytest.fixture
def real_pool(loaded, monkeypatch):  # noqa: F811
    """The app's own read path (pool, read_tx, statement timeout), on the test database."""
    pool = ConnectionPool(DSN, min_size=1, max_size=2, kwargs={"row_factory": dict_row, "autocommit": False},
                          open=True)
    monkeypatch.setattr(engine, "pool", lambda: pool)
    try:
        yield pool
    finally:
        pool.close()


def test_a_slow_statement_is_a_504_after_the_timeout(real_pool, monkeypatch):  # T7.3.c
    assert engine.settings().read_statement_timeout_ms == 5000
    real = query.row_page
    monkeypatch.setattr(query, "row_page", lambda conn, *a, **kw: conn.execute("SELECT pg_sleep(10)").fetchall())
    with TestClient(api.app) as client:
        started = time.monotonic()
        r = client.get("/v1/registers/gb_ukgc/tables/licences/rows")
        elapsed = time.monotonic() - started
        assert r.status_code == 504 and r.headers["content-type"] == "application/problem+json"
        assert r.json()["type"].endswith("#query-timeout") and r.headers["retry-after"] == "5"
        assert 4.5 < elapsed < 6, elapsed
        # The connection went back to the pool fit for the next request.
        monkeypatch.setattr(query, "row_page", real)
        for _ in range(3):
            assert client.get("/v1/registers/gb_ukgc/tables/licences/rows").status_code == 200
    with real_pool.connection() as c:  # SET LOCAL ended with its transaction
        assert c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"


# --- T7.3.d ------------------------------------------------------------------------------

def test_search_p95_is_under_300_ms(real_pool, capsys):  # T7.3.d
    with TestClient(api.app) as client:
        assert client.get("/v1/search?q=bet").status_code == 200  # warm: pool, data versions
        took = []
        for _ in range(50):
            started = time.perf_counter()
            r = client.get("/v1/search?q=bet")
            took.append((time.perf_counter() - started) * 1000)
            assert r.status_code == 200 and r.json()["data"]
    p95 = statistics.quantiles(took, n=20)[-1]
    with capsys.disabled():
        print(f"\nT7.3.d /v1/search?q=bet x50: p50 {statistics.median(took):.1f} ms, p95 {p95:.1f} ms, "
              f"max {max(took):.1f} ms")
    assert p95 < 300

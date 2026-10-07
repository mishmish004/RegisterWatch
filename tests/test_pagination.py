"""Phase 4 against Postgres: keyed pages that survive an ingest, one row by id,
the change feed, search in one query per register (plan.md P4.1-P4.4).

Skipped unless REGISTERWATCH_TEST_DATABASE_URL is set; every register's fixture
rows are loaded once as a complete baseline snapshot (`loaded`).
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api, query
from registerwatch.http import deps
from registerwatch.registers import REGISTRY
from tests.test_postgres import DSN, _snapshot, db, loaded  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")

ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"


class _Counting:
    """A connection that counts and remembers what it executes."""

    def __init__(self, conn):
        self.conn, self.executed = conn, []

    def execute(self, q, params=None, **kw):
        self.executed.append((q, params))
        return self.conn.execute(q, params, **kw)


@pytest.fixture(scope="module")
def client(loaded):  # noqa: F811
    seen: list[_Counting] = []

    @contextlib.contextmanager
    def counted():
        with loaded() as conn:
            seen.append(_Counting(conn))
            yield seen[-1]

    cfg = SimpleNamespace(ingest_token="ingest-token", read_token="", stale_after_h=26.0, log_level="WARNING")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(deps, "settings", lambda: cfg)
        mp.setattr(api, "settings", lambda: cfg)
        mp.setattr(deps, "tx", counted)
        mp.setattr(api, "tx", loaded)
        with TestClient(api.app) as c:
            c.connections = seen
            c.db = loaded
            yield c


def _walk(client, url, *, stop_after=None):
    """Follow next_cursor; returns (rows, responses). Stops early after `stop_after` pages."""
    rows, responses, r = [], [], client.get(url)
    while True:
        assert r.status_code == 200, r.text
        responses.append(r)
        page = r.json()
        rows += page["data"]
        assert ("link" in r.headers) == page["pagination"]["has_more"]  # T4.2.e
        if not page["pagination"]["has_more"] or (stop_after and len(responses) == stop_after):
            return rows, responses
        r = client.get(r.headers["link"].split(">")[0][1:])  # the Link target is the next page


def _current_ids(client, table="licences"):
    with client.db() as c:
        return [r["id"] for r in c.execute(f"SELECT id FROM gb_ukgc.current_{table} ORDER BY id")]


# --- P4.1 ----------------------------------------------------------------------------

@pytest.mark.parametrize("as_of_older", [False, True], ids=["current", "as-of-older-snapshot"])
def test_a_keyed_page_is_an_index_scan(client, as_of_older):  # T4.1.c
    reg = REGISTRY["gb_ukgc"]
    tbl = next(t for t in reg.tables if t.name == "licences")
    with client.db() as c:
        c.execute("ANALYZE gb_ukgc.licences")
        latest = query.latest_snapshot_id(c, reg)
        spy = _Counting(c)
        # As of an older snapshot: the same rows, read through the history predicate
        # a walk that began before the latest snapshot uses.
        query.row_page(spy, reg, tbl, snapshot_id=latest, latest=latest + 1 if as_of_older else latest,
                       after=37, limit=100)
        (q, params), = spy.executed
        plan = c.execute(b"EXPLAIN (FORMAT JSON) " + q.as_bytes(c), params).fetchone()["QUERY PLAN"]
    nodes = []

    def walk(node):
        nodes.append(node)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    kinds = [n["Node Type"] for n in nodes]
    assert "Sort" not in kinds and "Seq Scan" not in kinds, kinds
    scan = next(n for n in nodes if n["Node Type"] in ("Index Scan", "Index Only Scan"))
    assert scan["Relation Name"] == "licences"
    assert scan["Index Name"] == ("licences_pkey" if as_of_older else "licences_current_id"), scan["Index Name"]


# --- P4.2 ----------------------------------------------------------------------------

def test_a_full_walk_returns_every_current_row_once(client):  # T4.2.a
    rows, responses = _walk(client, ROWS + "?limit=37")
    ids = [r["id"] for r in rows]
    assert ids == sorted(set(ids)), "duplicates or out of order"
    assert ids == _current_ids(client) and len(ids) == 399
    assert len(responses) == 11 and all(len(r.json()["data"]) == 37 for r in responses[:-1])
    assert responses[-1].json()["pagination"] == {"next_cursor": None, "has_more": False, "limit": 37, "total": None}


def test_a_filtered_walk_with_a_total(client):
    rows, responses = _walk(client, ROWS + "?limit=50&filter[status]=Active&include_total=true")
    with client.db() as c:
        want = [r["id"] for r in c.execute("SELECT id FROM gb_ukgc.current_licences WHERE status = 'Active' "
                                           "ORDER BY id")]
    assert [r["id"] for r in rows] == want
    assert {r.json()["pagination"]["total"] for r in responses} == {len(want)}


def test_get_row_returns_one_row_with_its_history(client):  # T4.2.f
    first = client.get(ROWS + "?limit=1").json()["data"][0]
    r = client.get(f"{ROWS}/{first['id']}")
    body = r.json()
    assert r.status_code == 200 and body["values"] == first["values"] and body["id"] == first["id"]
    assert body["current"] is True and body["removed_snapshot_id"] is None and body["removed_at"] is None
    assert body["first_seen_snapshot_id"] == first["first_seen_snapshot_id"] <= body["last_seen_snapshot_id"]
    assert body["url"] == f"{ROWS}/{first['id']}"
    missing = client.get(f"{ROWS}/999999")
    assert missing.status_code == 404 and missing.json()["type"].endswith("#row-not-found")
    assert client.get(f"{ROWS}/0").status_code == 400


def test_a_walk_across_an_ingest_neither_repeats_nor_skips(client):  # T4.2.b
    """Half a walk, then a complete snapshot that adds 3 rows and removes 2, then
    the rest: the walk is exactly the snapshot it started at."""
    from registerwatch.db.repos import observations
    from registerwatch.ingest.engine import row_hashes

    reg = REGISTRY["gb_ukgc"]
    tbl = next(t for t in reg.tables if t.name == "licences")
    before = _current_ids(client)
    half, responses = _walk(client, ROWS + "?limit=37", stop_after=5)
    with client.db() as c:
        current = c.execute("SELECT * FROM gb_ukgc.current_licences ORDER BY id").fetchall()
        gone = [current[10], current[300]]  # one already returned, one not yet
        keep = [{k: r[k] for k in tbl.column_names} for r in current if r not in gone]
        new = [{**keep[0], "licence_number": f"9999{i}-X-000000-000"} for i in range(3)]
        src = c.execute("SELECT id FROM sources WHERE slug = 'gb_ukgc'").fetchone()["id"]
        sid = _snapshot(c, src)
        # Every other table keeps its rows, so only licences changes.
        others = {t.name: row_hashes([{k: r[k] for k in t.column_names} for r in
                                      c.execute(f"SELECT * FROM gb_ukgc.current_{t.name} ORDER BY id")])
                  for t in reg.tables if t.name != "licences"}
        stats = observations.apply(c, reg, sid, {**others, "licences": row_hashes(keep + new)})
    assert stats["licences"] == {"inserted": 3, "removed": 2, "unchanged": 397}
    nxt = responses[-1].headers["link"].split(">")[0][1:]
    rest, _ = _walk(client, nxt)
    ids = [r["id"] for r in half + rest]
    assert ids == sorted(set(ids)) and ids == before  # the snapshot the walk began at, exactly
    after = [r["id"] for r in _walk(client, ROWS + "?limit=37")[0]]
    assert after == _current_ids(client) and len(after) == 400
    assert {g["id"] for g in gone} & set(after) == set() and len(set(after) - set(before)) == 3
    removed = client.get(f"{ROWS}/{gone[0]['id']}").json()  # T4.2.f: a row that is no longer current
    assert removed["current"] is False and removed["removed_snapshot_id"] == sid


# --- P4.3 ----------------------------------------------------------------------------

def test_a_large_feed_pages_without_truncation(client):  # T4.3.b
    from registerwatch.db.repos import observations
    from registerwatch.ingest.engine import row_hashes

    reg = REGISTRY["pl_mf"]
    with client.db() as c:
        current = [{k: r[k] for k in reg.tables[0].column_names}
                   for r in c.execute("SELECT * FROM pl_mf.current_blocked_domains ORDER BY id")]
        synthetic = [{"entry_no": str(100000 + i), "domain": f"synthetic-{i}.example", "added_at_local": None,
                      "added_on": None} for i in range(1250)]
        src = c.execute("SELECT id FROM sources WHERE slug = 'pl_mf'").fetchone()["id"]
        add = _snapshot(c, src)
        observations.apply(c, reg, add, {"blocked_domains": row_hashes(current + synthetic)})
        remove = _snapshot(c, src)
        observations.apply(c, reg, remove, {"blocked_domains": row_hashes(current)})
    events, responses = _walk(client, "/v1/registers/pl_mf/changes?limit=1000")
    assert [r.json()["pagination"]["has_more"] for r in responses] == [True, True, False]
    assert len(events) == 2500 and [len(r.json()["data"]) for r in responses] == [1000, 1000, 500]
    assert [e["change"] for e in events] == ["added"] * 1250 + ["removed"] * 1250
    assert {e["snapshot_id"] for e in events[:1250]} == {add} and {e["snapshot_id"] for e in events[1250:]} == {remove}
    keys = [(e["snapshot_id"], e["row"]["id"], e["change"]) for e in events]
    assert keys == sorted(keys) and len(set(keys)) == 2500
    assert {e["row"]["values"]["domain"] for e in events} == {s["domain"] for s in synthetic}


def test_the_jurisdiction_feed_interleaves_its_registers(client):  # T4.3.c
    from registerwatch.db.repos import observations
    from registerwatch.ingest.engine import row_hashes

    def change(slug):
        reg = REGISTRY[slug]
        t = reg.tables[0]
        with client.db() as c:
            rows = [{k: r[k] for k in t.column_names}
                    for r in c.execute(f"SELECT * FROM {slug}.current_{t.name} ORDER BY id")]
            src = c.execute("SELECT id FROM sources WHERE slug = %s", (slug,)).fetchone()["id"]
            sid = _snapshot(c, src)
            observations.apply(c, reg, sid, {t.name: row_hashes(rows[1:] + [{**rows[0], "domain": f"new-{sid}.ch"}])})
        return sid

    snaps = [change("ch_esbk"), change("ch_gespa"), change("ch_esbk")]
    events, responses = _walk(client, "/v1/jurisdictions/ch/changes?limit=1")
    assert len(responses) == len(events) == 6  # one row out, one in, per snapshot; paged one at a time
    # Within a snapshot, by row id: the old row (removed) has the lower id.
    assert [(e["snapshot_id"], e["register"], e["change"]) for e in events] == [
        (snaps[0], "ch_esbk", "removed"), (snaps[0], "ch_esbk", "added"),
        (snaps[1], "ch_gespa", "removed"), (snaps[1], "ch_gespa", "added"),
        (snaps[2], "ch_esbk", "removed"), (snaps[2], "ch_esbk", "added")]
    whole = client.get("/v1/jurisdictions/ch/changes").json()["data"]
    assert [(e["snapshot_id"], e["row"]["id"]) for e in whole] == [(e["snapshot_id"], e["row"]["id"]) for e in events]


def test_the_feed_takes_only_offset_timestamps(client):  # T4.3.d
    url = "/v1/registers/gb_ukgc/changes?since="
    naive = client.get(url + "2026-10-01T00:00:00")
    assert naive.status_code == 400 and naive.json()["errors"][0]["field"] == "since"
    assert client.get(url + "2026-10-01T00:00:00Z").status_code == 200
    assert client.get(url + "2026-10-01T02:00:00%2B02:00").status_code == 200
    r = client.get(url + "2026-10-02T00:00:00Z&until=2026-10-01T00:00:00Z")
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "until"


def test_a_feed_cursor_is_bound_to_its_window(client):
    url = "/v1/jurisdictions/ch/changes?limit=1"
    cur = client.get(url).json()["pagination"]["next_cursor"]
    r = client.get(url + "&since=2026-01-01T00:00:00Z&cursor=" + cur)
    assert r.status_code == 400 and r.json()["type"].endswith("#invalid-cursor")
    assert client.get("/v1/registers/ch_esbk/changes?limit=1&cursor=" + cur).status_code == 400


# --- P4.4 ----------------------------------------------------------------------------

def test_search_is_one_query_per_register(client):  # T4.4.a
    for params, registers in [("q=betway", 21), ("q=betway&jurisdiction=gb", 1), ("q=bet&jurisdiction=ch", 2)]:
        client.connections.clear()
        assert client.get("/v1/search?" + params).status_code == 200
        (conn,) = client.connections
        assert len(conn.executed) <= registers, (params, len(conn.executed))


def test_every_search_rows_url_gives_the_same_total(client):  # T4.4.b
    hits = client.get("/v1/search?q=bet&limit=3").json()["data"]
    assert len(hits) >= 5
    for hit in hits:
        r = client.get(hit["rows_url"])
        assert r.status_code == 200 and r.json()["pagination"]["total"] == hit["total"], hit["rows_url"]
        assert [x["id"] for x in r.json()["data"][:3]] == [x["id"] for x in hit["rows"]]
        assert json.dumps(r.json()["data"][:3], sort_keys=True) == json.dumps(hit["rows"], sort_keys=True)

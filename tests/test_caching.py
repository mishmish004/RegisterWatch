"""Phase 7, P7.1: conditional reads. Every v1 read is tagged with the data
versions it depends on and answers `If-None-Match` with a 304; reads are
cacheable for 5 minutes, ingest and status never (plan.md P7.1).

The database is faked as in test_contract.py (every query answers nothing) with
the data versions under the test's control; the [pg] test runs the real engine.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api, query
from registerwatch.http import caching, deps
from registerwatch.ingest import runs
from tests.test_contract import _FakeConn, _Lease, _MemoryRuns
from tests.test_postgres import DSN, db, engine_on_fake_http, revoke_103  # noqa: F401 — fixtures

ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"
READ_TOKEN = "read-token-0123456789abcdefghijklmnopqrstuvwxyz"
INGEST_TOKEN = "ingest-token-0123456789abcdefghijklmnopqrstuvwxyz"
NOW = datetime(2026, 10, 7, 6, 0, 41, tzinfo=timezone.utc)

# Every v1 read operation, with what it depends on.
READS = {
    "listJurisdictions": "/v1/jurisdictions",
    "getJurisdiction": "/v1/jurisdictions/ch",
    "listJurisdictionChanges": "/v1/jurisdictions/ch/changes",
    "listRegisters": "/v1/registers",
    "getRegister": "/v1/registers/gb_ukgc",
    "getTable": "/v1/registers/gb_ukgc/tables/licences",
    "listRows": ROWS,
    "getRow": ROWS + "/10",
    "listRegisterChanges": "/v1/registers/gb_ukgc/changes",
    "listSnapshots": "/v1/registers/gb_ukgc/snapshots",
    "search": "/v1/search?q=bet",
    "getDomainStatus": "/v1/domains/bet365.com",
}


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token=INGEST_TOKEN, read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(deps, "settings", lambda: cfg)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    versions = {"gb_ukgc": [7, 9], "ch_esbk": [3, 3], "ch_gespa": [4, 5]}
    lookups = []

    def data_versions(conn):
        lookups.append(1)
        return [{"slug": s, "complete": c, "latest": n} for s, (c, n) in versions.items()]

    monkeypatch.setattr(deps.repo, "data_versions", data_versions)
    monkeypatch.setattr(query, "get_row", lambda conn, reg, tbl, row_id: {
        "id": row_id, "first_seen_snapshot_id": 7, "last_seen_snapshot_id": 7, "removed_snapshot_id": None,
        "first_seen_at": NOW, "last_seen_at": NOW, "removed_at": None, "account_number": "1"})
    clock = Clock()
    monkeypatch.setattr(caching.VERSIONS, "clock", clock)
    with TestClient(api.app) as c:
        c.cfg, c.versions, c.lookups, c.clock = cfg, versions, lookups, clock
        yield c


def _later(client, seconds: float = caching.TTL_S + 1) -> None:
    client.clock.t += seconds


# --- T7.1.a ----------------------------------------------------------------------------

def test_a_matching_etag_is_a_304_with_no_body(client, monkeypatch):  # T7.1.a
    pages = []
    real = query.row_page
    monkeypatch.setattr(query, "row_page", lambda *a, **kw: pages.append(1) or real(*a, **kw))
    first = client.get(ROWS)
    tag = first.headers["etag"]
    assert first.status_code == 200 and tag.startswith('W/"') and len(tag) == 2 + 1 + 64 + 1
    again = client.get(ROWS, headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == tag
    assert again.headers["cache-control"] == first.headers["cache-control"] == "public, max-age=300"
    assert again.headers["vary"] == first.headers["vary"] == "Authorization, Accept-Encoding"
    assert "content-type" not in again.headers and "content-length" not in again.headers
    # The 304 read no rows, and the versions were read once for both requests.
    assert len(pages) == 1 and len(client.lookups) == 1


@pytest.mark.parametrize("if_none_match", [
    "{tag}", "{strong}", 'W/"0000", {tag}', "*", '  {tag}  ',
])
def test_if_none_match_compares_weakly_and_takes_a_list(client, if_none_match):
    tag = client.get(ROWS).headers["etag"]
    header = if_none_match.format(tag=tag, strong=tag.removeprefix("W/"))
    assert client.get(ROWS, headers={"If-None-Match": header}).status_code == 304


def test_another_etag_or_another_query_is_a_200(client):
    tag = client.get(ROWS).headers["etag"]
    assert client.get(ROWS, headers={"If-None-Match": 'W/"0000"'}).status_code == 200
    other = client.get(ROWS + "?limit=5", headers={"If-None-Match": tag})
    assert other.status_code == 200 and other.headers["etag"] != tag


@pytest.mark.parametrize("op", READS)
def test_every_read_is_tagged_and_revalidates(client, op):
    first = client.get(READS[op])
    assert first.status_code == 200, first.text
    again = client.get(READS[op], headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.headers["etag"] == first.headers["etag"]


def test_an_invalid_request_is_answered_not_revalidated(client):
    """The 304 comes after the request's own checks: `*` on a bad request is still its 400 or 404."""
    assert client.get(ROWS + "?limit=0", headers={"If-None-Match": "*"}).status_code == 400
    r = client.get("/v1/registers/xx_nope", headers={"If-None-Match": "*"})
    assert r.status_code == 404 and "etag" not in r.headers and "cache-control" not in r.headers


def test_without_data_versions_a_read_is_answered_untagged(client, monkeypatch):
    def down(conn):
        raise ConnectionError("tenant not found")

    tries = []
    monkeypatch.setattr(deps.repo, "data_versions", lambda conn: tries.append(1) or down(conn))
    r = client.get(ROWS, headers={"If-None-Match": "*"})
    assert r.status_code == 200 and "etag" not in r.headers and r.headers["cache-control"] == "public, max-age=300"
    # What needs no data version (the register catalogue) is still tagged.
    assert "etag" in client.get("/v1/registers").headers
    # The lookup is not retried by every read while the database is down, only every RETRY_S.
    assert "etag" not in client.get(ROWS).headers and len(tries) == 1
    client.clock.t += caching.RETRY_S
    monkeypatch.setattr(deps.repo, "data_versions", lambda conn: tries.append(1) or [])
    assert "etag" in client.get(ROWS).headers and len(tries) == 2


# --- what an ETag depends on -------------------------------------------------------------

def test_rows_change_tag_with_the_newest_complete_snapshot_only(client):
    tag = client.get(ROWS).headers["etag"]
    client.versions["gb_ukgc"][1] = 10  # an incomplete run: nothing current changed
    _later(client)
    assert client.get(ROWS).headers["etag"] == tag
    client.versions["gb_ukgc"][0] = 10  # a complete one
    assert client.get(ROWS).headers["etag"] == tag  # within the 30 s the versions are kept
    _later(client)
    assert client.get(ROWS).headers["etag"] != tag


def test_history_and_freshness_change_tag_with_any_snapshot(client):
    snapshots, register = (client.get(p).headers["etag"] for p in (READS["listSnapshots"], READS["getRegister"]))
    client.versions["gb_ukgc"][1] = 10
    _later(client)
    assert client.get(READS["listSnapshots"]).headers["etag"] != snapshots
    assert client.get(READS["getRegister"]).headers["etag"] != register


def test_freshness_turns_over_with_max_age(client, monkeypatch):
    """failed_7d is a rolling window: it changes with no new snapshot."""
    monkeypatch.setattr(caching, "wall", lambda: 3000.0)
    tag, rows = client.get(READS["getRegister"]).headers["etag"], client.get(ROWS).headers["etag"]
    monkeypatch.setattr(caching, "wall", lambda: 3000.0 + caching.MAX_AGE)
    assert client.get(READS["getRegister"]).headers["etag"] != tag
    assert client.get(ROWS).headers["etag"] == rows


def test_a_jurisdiction_depends_on_its_own_registers_only(client):
    ch, gb = client.get("/v1/search?q=bet&jurisdiction=ch").headers["etag"], client.get(ROWS).headers["etag"]
    client.versions["ch_esbk"][0] = 99
    _later(client)
    assert client.get("/v1/search?q=bet&jurisdiction=ch").headers["etag"] != ch
    assert client.get(ROWS).headers["etag"] == gb


def test_a_new_build_never_revalidates_an_old_rendering(client, monkeypatch):
    tag = client.get("/v1/registers").headers["etag"]
    monkeypatch.setattr(caching, "salt", lambda: "another build")
    assert client.get("/v1/registers", headers={"If-None-Match": tag}).status_code == 200


# --- T7.1.c ------------------------------------------------------------------------------

@pytest.mark.parametrize("op", READS)
def test_reads_are_public_when_open_and_private_behind_a_token(client, op):  # T7.1.c
    open_ = client.get(READS[op])
    assert open_.headers["cache-control"] == "public, max-age=300"
    assert open_.headers["vary"] == "Authorization, Accept-Encoding"
    client.cfg.read_token = READ_TOKEN
    auth = {"Authorization": f"Bearer {READ_TOKEN}"}
    closed = client.get(READS[op], headers=auth)
    assert closed.headers["cache-control"] == "private, max-age=300"
    assert closed.headers["vary"] == "Authorization, Accept-Encoding"
    not_modified = client.get(READS[op], headers={**auth, "If-None-Match": closed.headers["etag"]})
    assert not_modified.status_code == 304 and not_modified.headers["cache-control"] == "private, max-age=300"


# --- T7.1.d ------------------------------------------------------------------------------

@pytest.fixture
def ingest(client, monkeypatch):
    """Ingest without a database or an engine: the runs repo answers from memory."""
    memory = _MemoryRuns()
    for name in ("create", "by_key", "get", "active", "page"):
        monkeypatch.setattr(runs.repo, name, getattr(memory, name))
    monkeypatch.setattr(runs, "acquire", lambda: _Lease())
    monkeypatch.setattr(runs, "execute", lambda lease, run_id, targets, **kw: None)
    monkeypatch.setattr(api, "make_store", lambda: object())
    monkeypatch.setattr(api.engine, "ingest_many", lambda regs, store, **kw: [])
    return client


def test_ingest_and_status_are_never_stored(ingest, monkeypatch):  # T7.1.d
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    token = {"Authorization": f"Bearer {INGEST_TOKEN}"}
    created = ingest.post("/v1/ingest-runs", headers=token, json={"registers": ["pl_mf"]})
    answers = {
        "POST /v1/ingest-runs": created,
        "GET /v1/ingest-runs": ingest.get("/v1/ingest-runs", headers=token),
        "GET /v1/ingest-runs/{id}": ingest.get(created.headers["location"], headers=token),
        "GET /v1/ingest-runs/{id} 404": ingest.get("/v1/ingest-runs/0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11",
                                                   headers=token),
        "POST /v1/ingest-runs 401": ingest.post("/v1/ingest-runs"),
        "POST /ingest/{slug}": ingest.post("/ingest/pl_mf", headers=token),
        "GET /ingest/last": ingest.get("/ingest/last", headers=token),
        "POST /jurisdictions/{code}/ingest": ingest.post("/jurisdictions/pl/ingest", headers=token),
        "GET /status": ingest.get("/status"),
        "GET /health": ingest.get("/health"),
    }
    assert {k: r.status_code for k, r in answers.items()} == {
        "POST /v1/ingest-runs": 202, "GET /v1/ingest-runs": 200, "GET /v1/ingest-runs/{id}": 200,
        "GET /v1/ingest-runs/{id} 404": 404, "POST /v1/ingest-runs 401": 401, "POST /ingest/{slug}": 202,
        "GET /ingest/last": 200, "POST /jurisdictions/{code}/ingest": 202, "GET /status": 503,
        "GET /health": 200}
    for name, r in answers.items():
        assert r.headers.get("cache-control") == "no-store", name
        assert "etag" not in r.headers, name
    # Reads are not caught by the pattern.
    assert ingest.get("/v1/registers").headers["cache-control"] == "public, max-age=300"
    assert "cache-control" not in ingest.get("/jurisdictions/pl").headers


def test_the_spec_documents_etags_and_304s():
    spec = api.app.openapi()
    ops = {op["operationId"]: op for item in spec["paths"].values() for op in item.values()}
    assert {"ETag", "Cache-Control"} <= set(spec["components"]["headers"])
    for op_id in READS:
        responses = ops[op_id]["responses"]
        assert responses["304"] == {"$ref": "#/components/responses/NotModified"}, op_id
        assert {"ETag", "Cache-Control"} <= set(responses["200"]["headers"]), op_id
    for op_id in ("createIngestRun", "listIngestRuns", "getIngestRun"):
        assert "304" not in ops[op_id]["responses"]
        assert "ETag" not in next(r for s, r in ops[op_id]["responses"].items() if s.startswith("2"))["headers"]


# --- T7.1.b [pg] -------------------------------------------------------------------------

@pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")
def test_the_tag_moves_with_a_complete_ingest_only(db, tmp_path, monkeypatch):  # noqa: F811 — T7.1.b
    """The whole engine, real SQL, fake HTTP: a complete run that changes the
    licences gives a new ETag (once the 30 s are up); a failed run does not,
    though the snapshot history, which lists failed runs, does move."""
    from tests import test_engine as te

    cfg = SimpleNamespace(ingest_token="", read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(deps, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", db)
    clock = Clock()
    monkeypatch.setattr(caching.VERSIONS, "clock", clock)
    run, responses = engine_on_fake_http(db, tmp_path, monkeypatch)
    snapshots = "/v1/registers/gb_ukgc/snapshots"

    assert run().complete
    with TestClient(api.app) as c:
        first = c.get(ROWS)
        tag = first.headers["etag"]
        assert first.status_code == 200 and c.get(ROWS, headers={"If-None-Match": tag}).status_code == 304

        responses[te.LIC] = (200, revoke_103(te.LICENCES))
        second = run()
        assert second.complete and not second.unchanged
        assert c.get(ROWS, headers={"If-None-Match": tag}).status_code == 304  # versions kept 30 s
        clock.t += caching.TTL_S + 1
        changed = c.get(ROWS, headers={"If-None-Match": tag})
        assert changed.status_code == 200 and changed.headers["etag"] != tag
        assert changed.json() != first.json()
        assert "Revoked" in {r["values"]["status"] for r in changed.json()["data"]}
        tag, history = changed.headers["etag"], c.get(snapshots).headers["etag"]

        responses[te.LIC] = (503, None)
        assert not run().complete
        clock.t += caching.TTL_S + 1
        assert c.get(ROWS, headers={"If-None-Match": tag}).status_code == 304
        assert c.get(snapshots, headers={"If-None-Match": history}).status_code == 200

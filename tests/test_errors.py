"""Phase 3: every error from /v1 is an RFC 9457 problem from the one catalog,
parameters are bounded, and every request has an id (plan.md P3.1-P3.3).

Each catalog entry is triggered through a real v1 route. An entry whose feature
lands later is raised from a real route by a patch until then; the phase that
adds the feature adds its real trigger (`LATER`, empty since P7).
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import time
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from pydantic import TypeAdapter, ValidationError

from registerwatch import api
from registerwatch.http import deps, problems, ratelimit, request_id
from registerwatch.http.problems import Catalog, ProblemError
from registerwatch.http.v1 import registers as v1_registers
from registerwatch.ingest import runs
from tests.test_postgres import DSN, db, loaded  # noqa: F401 — fixtures for the [pg] test

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"

# Raised by a patch until the named phase adds the feature that raises it.
LATER: dict[Catalog, str] = {}
INGEST = {"Authorization": "Bearer ingest-token"}
RUNS = "/v1/ingest-runs"
RUN_ID = "0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11"


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token="ingest-token", read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(deps, "settings", lambda: cfg)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(deps.repo, "source_health", lambda conn: [])
    monkeypatch.setattr(api.query, "rows", lambda conn, reg, tbl, **kw: {"total": 0, "rows": []})  # legacy
    monkeypatch.setattr(api.query, "latest_snapshot_id", lambda conn, reg: 0)
    monkeypatch.setattr(api.query, "row_page", lambda conn, reg, tbl, **kw: [])
    monkeypatch.setattr(api.query, "get_row", lambda conn, reg, tbl, row_id: None)
    with TestClient(api.app) as c:
        c.cfg = cfg
        yield c


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc
    return raiser


def _trigger(entry: Catalog, client, monkeypatch) -> tuple[str, str, dict]:
    """Make the app answer with `entry`; returns (method, path, request kwargs)."""
    if entry in LATER:
        monkeypatch.setattr(v1_registers, "freshness", _raise(ProblemError(entry, f"forced {entry.slug}")))
        return "GET", "/v1/registers/gb_ukgc", {}
    match entry:
        case Catalog.FORBIDDEN:
            client.cfg.read_token = "read-token"
            return "GET", RUNS, {"headers": {"Authorization": "Bearer read-token"}}
        case Catalog.INGEST_DISABLED:
            client.cfg.ingest_token = ""
            return "GET", RUNS, {"headers": INGEST}
        case Catalog.INGEST_RUN_NOT_FOUND:
            monkeypatch.setattr(runs.repo, "get", lambda conn, run_id: None)
            return "GET", f"{RUNS}/{RUN_ID}", {"headers": INGEST}
        case Catalog.INGEST_IN_PROGRESS:
            monkeypatch.setattr(runs, "acquire", lambda: None)
            monkeypatch.setattr(runs.repo, "active", lambda conn: {"id": RUN_ID})
            return "POST", RUNS, {"headers": INGEST}
        case Catalog.CONTENT_TOO_LARGE:
            return "POST", RUNS, {"headers": INGEST, "json": {"registers": ["gb_ukgc"] * 8000}}
        case Catalog.UNSUPPORTED_MEDIA_TYPE:
            return "POST", RUNS, {"headers": {**INGEST, "Content-Type": "text/plain"}, "content": "gb_ukgc"}
        case Catalog.IDEMPOTENCY_KEY_REUSED:
            monkeypatch.setattr(runs.repo, "by_key", lambda conn, key: {"id": RUN_ID, "request_hash": b"other"})
            return "POST", RUNS, {"headers": {**INGEST, "Idempotency-Key": "k"}, "json": {"jurisdiction": "ch"}}
        case Catalog.INVALID_PARAMETER:
            return "GET", ROWS + "?limit=0", {}
        case Catalog.UNKNOWN_FILTER_COLUMN:
            return "GET", ROWS + "?filter[nope]=1", {}
        case Catalog.INVALID_CURSOR:
            return "GET", ROWS + "?cursor=not-a-cursor", {}
        case Catalog.UNAUTHENTICATED:
            client.cfg.read_token = "read-token"
            return "GET", "/v1/registers", {}
        case Catalog.NOT_FOUND:
            return "GET", "/v1/nope", {}
        case Catalog.JURISDICTION_NOT_FOUND:
            return "GET", "/v1/jurisdictions/zz", {}
        case Catalog.REGISTER_NOT_FOUND:
            return "GET", "/v1/registers/xx_nope", {}
        case Catalog.TABLE_NOT_FOUND:
            return "GET", "/v1/registers/gb_ukgc/tables/nope", {}
        case Catalog.ROW_NOT_FOUND:
            return "GET", ROWS + "/999", {}
        case Catalog.METHOD_NOT_ALLOWED:
            return "POST", "/v1/registers", {}
        case Catalog.INTERNAL:
            monkeypatch.setattr(api.query, "row_page", _raise(RuntimeError("secret internals in /srv/app.py")))
            return "GET", ROWS, {}
        case Catalog.DATABASE_UNAVAILABLE:
            monkeypatch.setattr(deps, "tx", _raise(PoolTimeout("couldn't get a connection after 30.00 sec")))
            return "GET", ROWS, {}
        case Catalog.QUERY_TIMEOUT:
            monkeypatch.setattr(api.query, "row_page", _raise(psycopg.errors.QueryCanceled("canceling statement")))
            return "GET", ROWS, {}
        case Catalog.RATE_LIMITED:
            client.cfg.rate_limit_read_per_min, client.cfg.rate_limit_search_per_min = 1, 0
            client.cfg.rate_limit_ingest_per_min = 0
            monkeypatch.setattr(ratelimit, "settings", lambda: client.cfg)
            assert client.get("/v1/registers").status_code == 200  # the minute's one read
            return "GET", "/v1/registers", {}
    raise AssertionError(f"no trigger for {entry}")


# --- T3.1.a ------------------------------------------------------------------------

@pytest.mark.parametrize("entry", list(Catalog), ids=lambda e: e.slug)
def test_every_catalog_entry_is_a_problem(entry, client, monkeypatch):
    method, path, kw = _trigger(entry, client, monkeypatch)
    kw["headers"] = {**kw.get("headers", {}), "X-Request-Id": f"t31a-{entry.slug}"}
    r = client.request(method, path, **kw)
    body = r.json()
    assert r.status_code == entry.status == body["status"]
    assert r.headers["content-type"] == "application/problem+json"
    assert body["type"] == entry.type == f"{problems.DOCS}#{entry.slug}"
    assert body["title"] == entry.title and body["detail"]
    assert body["instance"] == path.split("?")[0]
    assert body["request_id"] == r.headers["x-request-id"] == f"t31a-{entry.slug}"
    if entry is Catalog.UNAUTHENTICATED:
        assert r.headers["www-authenticate"] == 'Bearer realm="registerwatch"'
    if entry in (Catalog.DATABASE_UNAVAILABLE, Catalog.QUERY_TIMEOUT):
        assert r.headers["retry-after"] == "5"
    if entry is Catalog.INGEST_IN_PROGRESS:
        assert r.headers["retry-after"] == "60" and body["active_run"] == f"{RUNS}/{RUN_ID}"
    if entry is Catalog.CONTENT_TOO_LARGE:
        assert r.headers["connection"] == "close"
    if entry is Catalog.RATE_LIMITED:
        assert int(r.headers["retry-after"]) >= 1 and r.headers["ratelimit"].startswith('"read";r=0;')
    if entry.status == 400:
        assert body["errors"] and {"field", "location", "message"} <= set(body["errors"][0])


def test_every_problem_type_has_a_section_in_the_docs():
    headings = set(re.findall(r"^## (\S+)$", (ROOT / "docs" / "problems.md").read_text(), re.M))
    assert {e.slug for e in Catalog} <= headings, "docs/problems.md is missing a section"


def test_slugs_and_types_are_unique():
    assert len({e.slug for e in Catalog}) == len({e.type for e in Catalog}) == len(Catalog)


def test_legacy_routes_keep_their_error_shape(client):
    r = client.get("/jurisdictions/zz")
    assert r.status_code == 404 and r.headers["content-type"] == "application/json"
    assert set(r.json()) == {"detail"}
    r = client.get("/jurisdictions/gb/changes")  # since is required
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


# --- T3.1.b ------------------------------------------------------------------------

def test_an_unhandled_error_leaks_nothing_and_is_logged_with_its_request_id(client, monkeypatch, caplog):
    monkeypatch.setattr(api.query, "row_page", _raise(RuntimeError("secret internals in /srv/app.py")))
    with caplog.at_level(logging.ERROR, logger="registerwatch.http.problems"):
        r = client.get(ROWS, headers={"X-Request-Id": "t31b"})
    assert r.status_code == 500 and r.json()["type"].endswith("#internal")
    assert "Traceback" not in r.text and ".py" not in r.text and "secret" not in r.text
    assert r.headers["x-request-id"] == "t31b"
    (record,) = [rec for rec in caplog.records if rec.getMessage() == "unhandled error"]
    assert record.request_id == "t31b" and record.exc_info is not None


def test_a_legacy_unhandled_error_is_still_plain_text(client, monkeypatch):
    monkeypatch.setattr(api.query, "rows", _raise(RuntimeError("boom")))
    r = client.get("/jurisdictions/gb/gb_ukgc/licences")
    assert r.status_code == 500 and r.text == "Internal Server Error" and "x-request-id" in r.headers


# --- T3.2 --------------------------------------------------------------------------

@pytest.mark.parametrize("limit", ["0", "1001", "-1", "ten"])
def test_limit_is_bounded(client, limit):  # T3.2.a
    r = client.get(f"{ROWS}?limit={limit}")
    assert r.status_code == 400 and r.json()["type"].endswith("#invalid-parameter")
    assert r.json()["errors"][0]["field"] == "limit" and r.json()["errors"][0]["location"] == "query"


def test_timestamps_must_carry_an_offset():  # T3.2.b; the route-level check is T4.3.d
    ts = TypeAdapter(deps.Timestamp)
    assert ts.validate_python("2026-10-01T00:00:00Z").utcoffset().total_seconds() == 0
    assert ts.validate_python("2026-10-01T02:00:00+02:00").utcoffset().total_seconds() == 7200
    with pytest.raises(ValidationError, match="timezone"):
        ts.validate_python("2026-10-01T00:00:00")


@pytest.mark.parametrize("domain", ["not_a_host!!", "a" * 300 + ".com", "nodot"])
def test_domain_is_validated(client, domain):  # T3.2.c
    r = client.get(f"/v1/domains/{domain}")
    assert r.status_code == 400 and r.json()["errors"][0] == {**r.json()["errors"][0], "field": "domain",
                                                              "location": "path"}


@pytest.mark.parametrize("q", ["a", "x" * 201])
def test_search_q_length_is_validated(client, q):  # T3.2.d
    r = client.get(f"/v1/search?q={q}")
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "q"


def test_a_misspelt_parameter_is_refused_not_ignored(client):
    r = client.get("/v1/search?q=bet&jurisdictions=gb")
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "jurisdictions"
    assert "jurisdiction" in r.json()["detail"]  # says what it does take
    assert client.get("/v1/registers?page=2").status_code == 400


@pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")
@pytest.mark.parametrize("path", ["/v1/search?q=a%00b", ROWS + "?filter[status]=a%00b", ROWS + "?q=a%00b",
                                  "/search?q=a%00b", "/jurisdictions/gb/gb_ukgc/licences?status=a%00b"])
def test_nul_bytes_are_a_400_against_postgres(path, loaded, monkeypatch):  # noqa: F811 — T3.2.f
    monkeypatch.setattr(api, "tx", loaded)
    monkeypatch.setattr(deps, "tx", loaded)
    with TestClient(api.app) as c:
        assert c.get(path.replace("%00", "")).status_code == 200  # the same request without it works
        r = c.get(path)
    assert r.status_code == 400
    if path.startswith("/v1/"):
        assert r.json()["type"].endswith("#invalid-parameter") and r.json()["errors"][0]["field"] in (
            "q", "filter[status]")
    else:
        assert "NUL" in r.json()["detail"]


# --- T3.3 --------------------------------------------------------------------------

def test_a_request_without_an_id_gets_a_uuid7(client):  # T3.3.a
    before = time.time_ns() // 1_000_000
    r = client.get("/v1/registers")
    rid = uuid.UUID(r.headers["x-request-id"])
    assert rid.version == 7 and rid.variant == uuid.RFC_4122
    assert before - 1 <= rid.int >> 80 <= time.time_ns() // 1_000_000 + 1  # its first 48 bits are now, in ms
    assert client.get("/v1/registers").headers["x-request-id"] != str(rid)
    assert uuid.UUID(client.get("/health").headers["x-request-id"]).version == 7  # legacy too


def test_a_supplied_id_is_echoed_and_an_unsafe_one_replaced(client):  # T3.3.b
    assert client.get("/v1/registers", headers={"X-Request-Id": "abc-123"}).headers["x-request-id"] == "abc-123"
    for bad in ["x" * 1024, "two words", "a\tb", ""]:
        rid = client.get("/v1/registers", headers={"X-Request-Id": bad}).headers["x-request-id"]
        assert rid != bad and uuid.UUID(rid).version == 7


def test_log_records_carry_the_request_id(client, monkeypatch, caplog):
    request_id.install_log_field()  # what the lifespan does; installing twice is a no-op
    request_id.install_log_field()
    monkeypatch.setattr(deps.repo, "source_health", _raise(PoolTimeout("no connection")))
    with caplog.at_level(logging.WARNING, logger="registerwatch.http.deps"):
        assert client.get("/v1/registers/gb_ukgc", headers={"X-Request-Id": "logged-1"}).status_code == 200
        logging.getLogger("registerwatch.http.deps").warning("outside any request")
    inside, outside = [r for r in caplog.records if r.name == "registerwatch.http.deps"]
    assert inside.getMessage() == "freshness unavailable" and inside.request_id == "logged-1"
    assert outside.request_id == "-"

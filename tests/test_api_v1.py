"""API v1: shapes, routing, paging and the read check. The database is faked
the way test_api.py fakes it; test_contract.py and test_postgres.py cover the
real queries."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.http import deps
from registerwatch.registers import REGISTRY

NOW = datetime(2026, 10, 7, 6, 0, 41, tzinfo=timezone.utc)


def _row(**values):
    return {**values, "first_seen_at": NOW - timedelta(days=2), "last_seen_at": NOW}


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token="ingest-token", read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(deps, "settings", lambda: cfg)
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(deps.repo, "source_health", lambda conn: [
        {"slug": "ch_esbk", "last_fetch": NOW, "last_good": NOW, "last_reason": None, "failed_7d": 0},
        {"slug": "ch_gespa", "last_fetch": NOW, "last_good": NOW - timedelta(days=3),
         "last_reason": "INVALID blocked_domains:HEADER_MISMATCH", "failed_7d": 3},
    ])
    with TestClient(api.app) as c:
        c.cfg = cfg
        yield c


def test_jurisdictions_are_a_page_of_every_code(client):
    body = client.get("/v1/jurisdictions").json()
    codes = [j["code"] for j in body["data"]]
    assert {"GB", "CH", "US-NJ"} <= set(codes) and len(codes) == 20
    assert body["pagination"] == {"next_cursor": None, "has_more": False, "limit": 20, "total": 20}
    ch = next(j for j in body["data"] if j["code"] == "CH")
    assert ch["url"] == "/v1/jurisdictions/ch"
    assert [r["slug"] for r in ch["registers"]] == ["ch_esbk", "ch_gespa"]
    assert ch["registers"][0]["url"] == "/v1/registers/ch_esbk"


def test_a_jurisdiction_carries_its_registers_freshness(client):
    ch = client.get("/v1/jurisdictions/CH").json()
    assert ch["code"] == "CH" and ch["name"] == "Switzerland"
    esbk, gespa = ch["registers"]
    assert esbk["freshness"] == {"last_attempt_at": "2026-10-07T06:00:41Z", "last_good_at": "2026-10-07T06:00:41Z",
                                 "last_reason": None, "failed_7d": 0}
    assert gespa["freshness"]["last_reason"] == "INVALID blocked_domains:HEADER_MISMATCH"
    assert esbk["tables"][0]["rows_url"] == "/v1/registers/ch_esbk/tables/blocked_domains/rows"
    assert client.get("/v1/jurisdictions/us_nj").json()["code"] == "US-NJ"
    assert client.get("/v1/jurisdictions/zz").status_code == 404


def test_freshness_is_null_when_the_database_is_down(client, monkeypatch):
    def down():
        raise ConnectionError("tenant not found")

    monkeypatch.setattr(deps, "tx", down)
    ch = client.get("/v1/jurisdictions/ch")
    assert ch.status_code == 200 and all(r["freshness"] is None for r in ch.json()["registers"])
    assert client.get("/v1/registers/gb_ukgc").json()["freshness"] is None


def test_a_register_never_ingested_has_empty_freshness_not_null(client):
    gb = client.get("/v1/registers/gb_ukgc").json()
    assert gb["freshness"] == {"last_attempt_at": None, "last_good_at": None, "last_reason": None, "failed_7d": 0}


def test_registers_list_and_detail(client):
    body = client.get("/v1/registers").json()
    assert [r["slug"] for r in body["data"]] == sorted(REGISTRY)
    gb = client.get("/v1/registers/gb_ukgc").json()
    assert gb["jurisdiction"] == "GB" and gb["kind"] == "licensees" and gb["url"] == "/v1/registers/gb_ukgc"
    assert [t["name"] for t in gb["tables"]] == ["businesses", "licences", "trading_names", "domain_names"]
    assert client.get("/v1/registers/xx_nope").status_code == 404


def test_a_table_lists_its_typed_columns(client):
    t = client.get("/v1/registers/gb_ukgc/tables/licences").json()
    assert t["register"] == "gb_ukgc" and t["name"] == "licences"
    assert {"name": "start_date", "type": "date", "required": False} in t["columns"]
    assert t["rows_url"] == "/v1/registers/gb_ukgc/tables/licences/rows"
    assert client.get("/v1/registers/gb_ukgc/tables/nope").status_code == 404


@pytest.fixture
def rows(client, monkeypatch):
    """A fake table of 5 rows behind query.rows, honouring limit and offset."""
    calls = []
    table = [_row(account_number=str(i), licence_number=f"00010{i}-N-000000-000", status="Active", type=None,
                  activity="Bingo", start_date=None, end_date=None) for i in range(5)]

    def fake_rows(conn, reg, tbl, *, q=None, filters=None, limit=100, offset=0):
        if filters and set(filters) - set(tbl.column_names):
            raise ValueError("unknown column")
        calls.append({"q": q, "filters": filters, "limit": limit, "offset": offset})
        return {"total": len(table), "rows": table[offset:offset + limit]}

    monkeypatch.setattr(api.query, "rows", fake_rows)
    client.calls = calls
    return client


def test_rows_come_in_pages_linked_by_an_opaque_cursor(rows):
    url = "/v1/registers/gb_ukgc/tables/licences/rows?limit=2&filter[status]=Active"
    seen, page, n = [], rows.get(url).json(), 1
    while True:
        seen += [r["values"]["account_number"] for r in page["data"]]
        assert page["pagination"]["limit"] == 2 and page["pagination"]["total"] is None
        if not page["pagination"]["has_more"]:
            assert page["pagination"]["next_cursor"] is None
            break
        page, n = rows.get(url + "&cursor=" + page["pagination"]["next_cursor"]).json(), n + 1
    assert seen == ["0", "1", "2", "3", "4"] and n == 3
    assert all(c["filters"] == {"status": "Active"} for c in rows.calls)
    first = rows.get(url).json()["data"][0]
    assert set(first) == {"first_seen_at", "last_seen_at", "values"}
    assert set(first["values"]) == {c.name for c in REGISTRY["gb_ukgc"].tables[1].columns}


def test_rows_total_only_when_asked(rows):
    assert rows.get("/v1/registers/gb_ukgc/tables/licences/rows?include_total=true").json()["pagination"]["total"] == 5


def test_a_cursor_only_pages_the_query_that_issued_it(rows):
    base = "/v1/registers/gb_ukgc/tables/licences/rows?limit=2"
    cur = rows.get(base + "&filter[status]=Active").json()["pagination"]["next_cursor"]
    assert rows.get(base + "&filter[status]=Revoked&cursor=" + cur).status_code == 400
    assert rows.get(base + "&cursor=" + cur[:-2] + "xx").status_code == 400
    assert rows.get(base + "&cursor=not-a-cursor").status_code == 400


def test_rows_refuse_unknown_parameters_and_columns(rows):
    base = "/v1/registers/gb_ukgc/tables/licences/rows"
    r = rows.get(base + "?status=Active")
    assert r.status_code == 400 and "filter[status]" in r.json()["detail"]
    assert rows.get(base + "?filter[nope]=1").status_code == 400
    assert rows.get(base + "?filter[status]=A&filter[status]=B").status_code == 400
    assert rows.get(base + "?limit=0").status_code == 422 and rows.get(base + "?limit=1001").status_code == 422
    assert rows.get("/v1/registers/gb_ukgc/tables/nope/rows").status_code == 404


def test_search_returns_one_hit_per_table_with_a_link_to_all_of_it(client, monkeypatch):
    def fake_search(conn, q, regs, *, limit=20):
        assert {r.slug for r in regs} == {"ch_esbk", "ch_gespa", "gb_ukgc"}
        return [{"jurisdiction": "CH", "register": "ch_esbk", "table": "blocked_domains", "total": 3,
                 "regulator": "ESBK", "kind": "blocklist",
                 "rows": [_row(domain="0101b00merang-bet.com", listed_on=None)]}]

    monkeypatch.setattr(api.query, "search", fake_search)
    body = client.get("/v1/search?q=b00merang&jurisdiction=ch&jurisdiction=gb&jurisdiction=CH").json()
    hit = body["data"][0]
    assert hit["register"] == "ch_esbk" and hit["total"] == 3
    assert hit["rows"][0]["values"] == {"domain": "0101b00merang-bet.com", "listed_on": None}
    assert hit["rows_url"] == "/v1/registers/ch_esbk/tables/blocked_domains/rows?q=b00merang"
    assert client.get("/v1/search?q=b").status_code == 422
    assert client.get("/v1/search?q=bet&jurisdiction=zz").status_code == 404


def test_a_domain_reports_where_it_is_licensed_and_blocked(client, monkeypatch):
    def fake_check(conn, domain, regs):
        if "!" in domain:
            raise ValueError(f"{domain!r} is not a hostname")
        return {"domain": "bet365.com", "licensed_in": ["US-NJ"], "blocked_in": ["CH"], "matches": [
            {"jurisdiction": "US-NJ", "register": "us_nj_dge", "regulator": "DGE", "kind": "licensees",
             "table": "internet_gaming_sites", "match": "subdomain",
             "row": _row(licensee="HARD ROCK HOTEL AND CASINO", site="nj.bet365.com", host="nj.bet365.com",
                         status="authorized")}]}

    monkeypatch.setattr(api.query, "check_domain", fake_check)
    body = client.get("/v1/domains/www.bet365.com").json()
    assert body["licensed_in"] == ["US-NJ"] and body["blocked_in"] == ["CH"]
    assert body["matches"][0]["match"] == "subdomain"
    assert body["matches"][0]["row"]["values"]["site"] == "nj.bet365.com"
    assert client.get("/v1/domains/not!a!host").status_code == 400


def test_v1_reads_need_the_read_token_when_one_is_set(client):
    client.cfg.read_token = "r34d"
    assert client.get("/v1/registers").status_code == 401
    assert client.get("/v1/registers", headers={"Authorization": "Bearer r34d"}).status_code == 200
    assert client.get("/v1/registers", headers={"Authorization": "Bearer ingest-token"}).status_code == 200

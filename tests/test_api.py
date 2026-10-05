"""The HTTP layer: auth, register routing, the one-at-a-time guard, and /status
as a stale alarm. The engine is faked — test_engine.py owns its behaviour."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.ingest.engine import IngestResult
from registerwatch.registers import REGISTRY

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token=TOKEN, read_token="", stale_after_h=26.0, log_level="WARNING")
    monkeypatch.setattr(api, "settings", lambda: cfg)
    monkeypatch.setattr(api, "make_store", lambda: object())
    runs: list[dict] = []

    def fake_many(registers, store, *, force=False, accept_count_delta=False):
        runs.append({"slugs": [r.slug for r in registers], "force": force,
                     "accept_count_delta": accept_count_delta})
        return [IngestResult(r.slug, 7, True, None, 1, 1, record_count=10, raw_hash=b"\x01") for r in registers]

    monkeypatch.setattr(api.engine, "ingest_many", fake_many)
    api._last.clear()
    with TestClient(api.app) as c:
        c.cfg, c.runs = cfg, runs
        yield c
    if api._running.locked():
        api._running.release()


def test_health_needs_nothing(client):
    assert client.get("/health").json() == {"ok": True}


def test_registers_lists_every_schema(client):
    body = client.get("/registers").json()
    assert {r["slug"] for r in body} == set(REGISTRY)
    gb = next(r for r in body if r["slug"] == "gb_ukgc")
    assert gb["schema"] == "gb_ukgc" and "licences" in gb["tables"]


def test_ingest_without_a_configured_token_is_disabled(client):
    client.cfg.ingest_token = ""
    assert client.post("/ingest/all", headers=AUTH).status_code == 503
    assert client.runs == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}])
def test_ingest_rejects_a_bad_token(client, headers):
    assert client.post("/ingest/all", headers=headers).status_code == 401
    assert client.runs == []


def test_ingest_all_accepts_then_runs_every_register_in_the_background(client):
    r = client.post("/ingest/all?force=true", headers=AUTH)
    assert r.status_code == 202 and r.json()["accepted"] and len(r.json()["registers"]) == 21
    assert client.runs == [{"slugs": sorted(REGISTRY), "force": True, "accept_count_delta": False}]
    last = client.get("/ingest/last", headers=AUTH).json()
    assert last["ok"] and last["complete"] == 21 and last["results"][0]["raw_hash"] == "01"
    assert not api._running.locked()


def test_ingest_one_register_inline(client):
    r = client.post("/ingest/pl_mf?wait=true&accept_count_delta=true", headers=AUTH)
    assert r.status_code == 200 and r.json()["results"][0]["slug"] == "pl_mf"
    assert client.runs == [{"slugs": ["pl_mf"], "force": False, "accept_count_delta": True}]


def test_an_unknown_register_is_404(client):
    assert client.post("/ingest/xx_nope", headers=AUTH).status_code == 404
    assert not api._running.locked()


def test_a_second_ingest_while_one_runs_is_refused(client):
    assert api._running.acquire(blocking=False)
    try:
        assert client.post("/ingest/all", headers=AUTH).status_code == 409
    finally:
        api._running.release()
    assert client.runs == []


def test_a_crashing_batch_releases_the_lock_and_reports(client, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(api.engine, "ingest_many", boom)
    r = client.post("/ingest/all?wait=true", headers=AUTH).json()
    assert r["ok"] is False and r["error"] == "ConnectionError: database unreachable"
    assert not api._running.locked()


def _status_with(client, monkeypatch, last_good):
    now = datetime.now(timezone.utc)
    rows = [{"slug": s, "last_fetch": now, "last_good": last_good(s), "last_reason": None, "failed_7d": 0}
            for s in REGISTRY]
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(api.repo, "source_health", lambda conn: rows)
    return client.get("/status")


def test_status_is_200_when_every_register_is_fresh(client, monkeypatch):
    r = _status_with(client, monkeypatch, lambda s: datetime.now(timezone.utc) - timedelta(hours=3))
    assert r.status_code == 200 and r.json()["stale"] is False


def test_status_names_the_stale_registers(client, monkeypatch):
    fresh = datetime.now(timezone.utc) - timedelta(hours=3)
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    r = _status_with(client, monkeypatch, lambda s: old if s == "ch_esbk" else (None if s == "pl_mf" else fresh))
    assert r.status_code == 503 and r.json()["stale_registers"] == ["ch_esbk", "pl_mf"]


def test_status_is_503_when_the_database_is_down(client, monkeypatch):
    def down():
        raise ConnectionError("tenant not found")

    monkeypatch.setattr(api, "tx", down)
    r = client.get("/status")
    assert r.status_code == 503 and r.json()["stale"] is True


# --- reading ---------------------------------------------------------------------

def test_jurisdictions_list_and_detail(client, monkeypatch):
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(api.repo, "source_health", lambda conn: [])
    codes = {j["code"] for j in client.get("/jurisdictions").json()}
    assert {"GB", "CH", "US-NJ"} <= codes
    ch = client.get("/jurisdictions/ch").json()
    assert [r["slug"] for r in ch["registers"]] == ["ch_esbk", "ch_gespa"]
    assert client.get("/jurisdictions/zz").status_code == 404


def test_rows_endpoint_routes_filters_and_refuses_unknown_columns(client, monkeypatch):
    seen = {}

    def fake_rows(conn, reg, table, *, q=None, filters=None, limit=100, offset=0):
        if filters and "nope" in filters:
            raise ValueError("unknown column 'nope'")
        seen.update(reg=reg.slug, table=table.name, q=q, filters=filters, limit=limit)
        return {"total": 0, "rows": []}

    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(api.query, "rows", fake_rows)
    r = client.get("/jurisdictions/gb/gb_ukgc/licences?status=Active&q=bet&limit=5")
    assert r.status_code == 200
    assert seen == {"reg": "gb_ukgc", "table": "licences", "q": "bet", "filters": {"status": "Active"}, "limit": 5}
    assert client.get("/jurisdictions/gb/gb_ukgc/licences?nope=1").status_code == 400
    assert client.get("/jurisdictions/gb/gb_ukgc/nosuchtable").status_code == 404
    assert client.get("/jurisdictions/gb/pl_mf/blocked_domains").status_code == 404  # register of another jurisdiction


def test_read_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(api.query, "check_domain", lambda conn, d, regs: {"domain": d, "matches": []})
    assert client.get("/check/domain/bet365.com").status_code == 200  # open by default
    client.cfg.read_token = "r34d"
    assert client.get("/check/domain/bet365.com").status_code == 401
    assert client.get("/check/domain/bet365.com", headers={"Authorization": "Bearer r34d"}).status_code == 200
    assert client.get("/check/domain/bet365.com", headers=AUTH).status_code == 200  # ingest token also reads
    assert client.get("/health").status_code == 200


def test_jurisdiction_ingest_runs_only_its_registers(client):
    r = client.post("/jurisdictions/ch/ingest?wait=true", headers=AUTH)
    assert r.status_code == 200 and client.runs[-1]["slugs"] == ["ch_esbk", "ch_gespa"]
